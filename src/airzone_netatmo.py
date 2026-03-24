"""
Airzone Netatmo Weather Station Integration
=============================================
Fetches indoor climate data (temperature, humidity, CO2) from Netatmo
weather station modules and stores it alongside zone_readings.

No UI dependencies — usable by both macOS app and Pi daemon.

Setup:
    1. Create an app at https://dev.netatmo.com/
    2. Run: python3 airzone_netatmo.py --auth
       (opens browser for OAuth2 flow, saves tokens)
    3. Add to config:
       "netatmo_enabled": true,
       "netatmo_client_id": "YOUR_CLIENT_ID",
       "netatmo_client_secret": "YOUR_CLIENT_SECRET"
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import webbrowser
from datetime import datetime, timedelta, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger("airzone")

# ── Netatmo API Constants ────────────────────────────────────────────────────

AUTH_URL = "https://api.netatmo.com/oauth2/authorize"
TOKEN_URL = "https://api.netatmo.com/oauth2/token"
API_BASE = "https://api.netatmo.com/api"
SCOPE = "read_station"
REDIRECT_PORT = 8042
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

import sys
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path.home() / "Library" / "Application Support" / "Airzone"
else:
    SCRIPT_DIR = Path(__file__).parent.parent
TOKEN_FILE = SCRIPT_DIR / "netatmo_tokens.json"

MODULE_TYPES = {
    "NAMain": "Base Station (indoor)",
    "NAModule1": "Outdoor",
    "NAModule4": "Additional Indoor",
    "NAModule3": "Rain Gauge",
    "NAModule2": "Wind Gauge",
}


# ── DB Schema ────────────────────────────────────────────────────────────────

def create_netatmo_tables(conn: sqlite3.Connection):
    """Create Netatmo storage tables if they don't exist."""
    conn.execute("""CREATE TABLE IF NOT EXISTS netatmo_readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            module_mac  TEXT    NOT NULL,
            module_name TEXT,
            temperature REAL,
            humidity    INTEGER,
            co2         INTEGER,
            noise       INTEGER,
            pressure    REAL,
            UNIQUE(timestamp, module_mac)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_netatmo_ts ON netatmo_readings(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_netatmo_module_ts ON netatmo_readings(module_mac, timestamp)")
    conn.commit()
    conn.commit()


# ── Token Management ─────────────────────────────────────────────────────────

def _load_tokens() -> dict | None:
    """Load saved tokens from secure storage or legacy file."""
    # Try secure storage first
    try:
        from airzone_secrets import secrets as sec
        access = sec.get("netatmo_access_token")
        refresh = sec.get("netatmo_refresh_token")
        if access and refresh:
            return {
                "access_token": access,
                "refresh_token": refresh,
                "obtained_at": int(sec.get("netatmo_token_obtained", "0")),
                "expires_in": int(sec.get("netatmo_token_expires_in", "10800")),
            }
    except Exception:
        pass
    # Fallback: legacy plaintext file
    if TOKEN_FILE.exists():
        try:
            tokens = json.loads(TOKEN_FILE.read_text())
            # Migrate to secure storage
            _save_tokens(tokens)
            # Remove legacy file
            try:
                TOKEN_FILE.unlink()
                log.info("Migrated Netatmo tokens to secure storage, "
                         "removed %s", TOKEN_FILE.name)
            except Exception:
                pass
            return tokens
        except Exception:
            return None
    return None


def _save_tokens(tokens: dict):
    """Persist tokens to .env or fallback file."""
    try:
        from airzone_secrets import secrets as sec
        sec.set("netatmo_access_token", tokens.get("access_token", ""))
        sec.set("netatmo_refresh_token", tokens.get("refresh_token", ""))
        sec.set("netatmo_token_obtained", str(tokens.get("obtained_at", 0)))
        sec.set("netatmo_token_expires_in", str(tokens.get("expires_in", 10800)))
        return
    except Exception:
        pass
    # Fallback: write to disk
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))


def _refresh_access_token(client_id: str, client_secret: str,
                           refresh_token: str) -> dict | None:
    """Use refresh_token to get a new access_token."""
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }, timeout=30)
    if resp.status_code != 200:
        log.error("Netatmo token refresh failed (%d): %s",
                  resp.status_code, resp.text[:200])
        return None
    tokens = resp.json()
    tokens["obtained_at"] = int(time.time())
    _save_tokens(tokens)
    return tokens


def get_access_token(client_id: str, client_secret: str) -> str | None:
    """
    Return a valid access token, refreshing if necessary.
    Returns None if no tokens are available (need to run --auth).
    """
    tokens = _load_tokens()
    if not tokens:
        log.error("Netatmo: no tokens found. Run --auth first.")
        return None

    # Check if token is expired (with 5-minute buffer)
    obtained = tokens.get("obtained_at", 0)
    expires_in = tokens.get("expires_in", 10800)
    if time.time() > obtained + expires_in - 300:
        log.info("Netatmo: access token expired, refreshing...")
        tokens = _refresh_access_token(
            client_id, client_secret, tokens["refresh_token"])
        if not tokens:
            return None

    return tokens.get("access_token")


# ── OAuth2 Authorization Flow ────────────────────────────────────────────────

def run_auth_flow(client_id: str, client_secret: str) -> bool:
    """
    Run the full OAuth2 authorization code flow.

    Opens the user's browser, starts a local HTTP server to catch the
    callback, exchanges the code for tokens, and saves them.
    """
    import secrets
    state = secrets.token_urlsafe(16)

    auth_params = urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "response_type": "code",
    })
    auth_url = f"{AUTH_URL}?{auth_params}"

    # Store the received code
    received = {"code": None, "state": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            received["code"] = qs.get("code", [None])[0]
            received["state"] = qs.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Netatmo authorization successful!</h2>"
                b"<p>You can close this tab.</p></body></html>"
            )

        def log_message(self, format, *args):
            pass  # Suppress HTTP server logs

    print(f"Opening browser for Netatmo authorization...")
    print(f"If the browser doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Wait for callback
    server = HTTPServer(("localhost", REDIRECT_PORT), CallbackHandler)
    server.timeout = 120  # 2 minutes to complete auth
    server.handle_request()
    server.server_close()

    if not received["code"]:
        log.error("Netatmo: no authorization code received")
        return False
    if received["state"] != state:
        log.error("Netatmo: state mismatch (possible CSRF)")
        return False

    # Exchange code for tokens
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": received["code"],
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
    }, timeout=30)

    if resp.status_code != 200:
        log.error("Netatmo token exchange failed (%d): %s",
                  resp.status_code, resp.text[:200])
        return False

    tokens = resp.json()
    tokens["obtained_at"] = int(time.time())
    _save_tokens(tokens)
    print(f"Netatmo tokens saved to {TOKEN_FILE}")
    return True


# ── API Client ───────────────────────────────────────────────────────────────

def _api_get(endpoint: str, token: str, **params) -> dict | None:
    """Make an authenticated GET request to the Netatmo API."""
    resp = requests.get(
        f"{API_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    if resp.status_code == 403:
        body = (resp.json() if resp.headers.get(
            "content-type", "").startswith("application/json") else {})
        code = body.get("error", {}).get("code")
        if code == 26:
            log.warning("Netatmo: rate limit reached, wait and retry")
            return None
        log.error("Netatmo: forbidden (%s)", body)
        return None
    if resp.status_code == 401:
        log.error("Netatmo: unauthorized (token expired?)")
        return None
    if resp.status_code != 200:
        log.error("Netatmo API error %d: %s",
                  resp.status_code, resp.text[:200])
        return None
    return resp.json()


def get_stations(token: str) -> list[dict]:
    """
    Fetch all weather stations and their modules.

    Returns a flat list of dicts, one per module:
    [
        {
            "device_id": "70:ee:50:xx",  # base station MAC
            "module_id": None,            # None for base station
            "module_name": "Indoor",
            "module_type": "NAMain",
            "data_types": ["Temperature", "CO2", "Humidity", ...],
            "dashboard": { ... current readings ... },
        },
        ...
    ]
    """
    data = _api_get("getstationsdata", token)
    if not data or "body" not in data:
        return []

    result = []
    for device in data["body"].get("devices", []):
        # Base station itself
        result.append({
            "device_id": device["_id"],
            "module_id": None,
            "module_name": device.get("module_name",
                                      device.get("station_name", "Base")),
            "module_type": device.get("type", "NAMain"),
            "data_types": device.get("data_type", []),
            "dashboard": device.get("dashboard_data", {}),
        })
        # Its modules
        for mod in device.get("modules", []):
            result.append({
                "device_id": device["_id"],
                "module_id": mod["_id"],
                "module_name": mod.get("module_name", mod["_id"]),
                "module_type": mod.get("type", "unknown"),
                "data_types": mod.get("data_type", []),
                "dashboard": mod.get("dashboard_data", {}),
            })

    return result


def get_measure(token: str, device_id: str,
                module_id: str | None,
                measure_types: list[str],
                date_begin: int, date_end: int,
                scale: str = "30min") -> list[dict]:
    """
    Fetch historical measurements for a single module.

    Returns list of dicts:
    [
        {"timestamp": "2024-03-08T10:00:00Z",
         "Temperature": 21.3, "Humidity": 52},
        ...
    ]
    """
    params = {
        "device_id": device_id,
        "scale": scale,
        "type": ",".join(measure_types),
        "date_begin": str(date_begin),
        "date_end": str(date_end),
        "optimize": "false",
    }
    if module_id:
        params["module_id"] = module_id

    data = _api_get("getmeasure", token, **params)
    if not data or "body" not in data:
        return []

    body = data["body"]
    results = []

    # Body can be a dict {timestamp: [values]} or a list [{beg_time, step_time, value}]
    if isinstance(body, dict):
        for ts_str, values in body.items():
            ts = int(ts_str)
            row = {
                "timestamp": datetime.utcfromtimestamp(ts).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
            }
            for i, mtype in enumerate(measure_types):
                if i < len(values) and values[i] is not None:
                    row[mtype] = values[i]
            results.append(row)
    elif isinstance(body, list):
        for chunk in body:
            beg = chunk.get("beg_time", 0)
            step = chunk.get("step_time", 1800)
            for j, values in enumerate(chunk.get("value", [])):
                ts = beg + j * step
                row = {
                    "timestamp": datetime.utcfromtimestamp(ts).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"),
                }
                for i, mtype in enumerate(measure_types):
                    if i < len(values) and values[i] is not None:
                        row[mtype] = values[i]
                results.append(row)

    results.sort(key=lambda r: r["timestamp"])
    return results


def fetch_module_history(token: str, device_id: str,
                         module_id: str | None,
                         module_name: str,
                         data_types: list[str],
                         start_date: date,
                         end_date: date,
                         scale: str = "30min") -> list[dict]:
    """
    Fetch historical data for a module over an arbitrary date range.

    Automatically paginates (max 1024 points per request at 30min ~21 days).
    Returns a flat list of measurement dicts.
    """
    supported = {"Temperature", "Humidity", "CO2", "Noise", "Pressure"}
    measure_types = [t for t in data_types if t in supported]
    if not measure_types:
        return []

    all_readings = []
    current_start = int(datetime.combine(
        start_date, datetime.min.time()).timestamp())
    final_end = int(datetime.combine(
        end_date, datetime.min.time()).timestamp())

    chunk_seconds = 21 * 86400  # 21 days per chunk

    while current_start < final_end:
        chunk_end = min(current_start + chunk_seconds, final_end)

        readings = get_measure(
            token, device_id, module_id,
            measure_types, current_start, chunk_end, scale,
        )

        if readings:
            for r in readings:
                r["module_name"] = module_name
                r["module_mac"] = module_id or device_id
            all_readings.extend(readings)
            log.info("Netatmo: fetched %d readings for %s (%s to %s)",
                     len(readings), module_name,
                     datetime.utcfromtimestamp(current_start).date(),
                     datetime.utcfromtimestamp(chunk_end).date())

        current_start = chunk_end
        time.sleep(0.3)  # Rate limit courtesy

    return all_readings


# ── Storage ──────────────────────────────────────────────────────────────────

def store_readings(conn: sqlite3.Connection, readings: list[dict]) -> int:
    """Store Netatmo readings in the database (ignores duplicates)."""
    if not readings:
        return 0
    create_netatmo_tables(conn)

    rows = []
    for r in readings:
        rows.append((
            r["timestamp"],
            r.get("module_mac", ""),
            r.get("module_name", ""),
            r.get("Temperature"),
            r.get("Humidity"),
            r.get("CO2"),
            r.get("Noise"),
            r.get("Pressure"),
        ))

    conn.executemany(
        "INSERT OR IGNORE INTO netatmo_readings "
        "(timestamp, module_mac, module_name, temperature, humidity, "
        " co2, noise, pressure) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def get_netatmo_readings(conn: sqlite3.Connection,
                         module_name: str | None = None,
                         hours: int = 168) -> list[dict]:
    """Get Netatmo readings for chart display."""
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    if module_name:
        rows = conn.execute(
            "SELECT timestamp, module_name, temperature, humidity, co2 "
            "FROM netatmo_readings "
            "WHERE module_name = ? AND timestamp >= ? "
            "ORDER BY timestamp",
            (module_name, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT timestamp, module_name, temperature, humidity, co2 "
            "FROM netatmo_readings "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (cutoff,),
        ).fetchall()
    return [dict(zip(
        ["timestamp", "module_name", "temperature", "humidity", "co2"], r))
        for r in rows]


def get_netatmo_module_names(conn: sqlite3.Connection) -> list[str]:
    """Return distinct module names from stored data."""
    rows = conn.execute(
        "SELECT DISTINCT module_name FROM netatmo_readings "
        "WHERE module_name IS NOT NULL AND module_name != '' "
        "ORDER BY module_name"
    ).fetchall()
    return [r[0] for r in rows]


# ── Backfill (Historical Data) ───────────────────────────────────────────────

BACKFILL_TABLE_SQL = """CREATE TABLE IF NOT EXISTS netatmo_sync_status (
        module_mac   TEXT PRIMARY KEY,
        module_name  TEXT,
        oldest_date  TEXT,
        newest_date  TEXT,
        total_readings INTEGER DEFAULT 0,
        last_sync    TEXT
    )"""


def backfill_history(client_id: str, client_secret: str,
                     conn: sqlite3.Connection,
                     days: int = 365,
                     scale: str = "30min") -> dict:
    """
    Fetch historical Netatmo data going back up to `days` days.

    Tracks sync status per module to avoid re-fetching already-synced ranges.
    Called on first Netatmo connection, then incrementally on each poll.

    Returns summary: {modules: [...], total_new_readings: int}
    """
    token = get_access_token(client_id, client_secret)
    if not token:
        return {"error": "No valid token", "modules": []}

    stations = get_stations(token)
    if not stations:
        return {"error": "No stations found", "modules": []}

    create_netatmo_tables(conn)
    conn.execute(BACKFILL_TABLE_SQL)
    conn.commit()

    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    summary = []
    total_new = 0

    for mod in stations:
        mac = mod["module_id"] or mod["device_id"]
        mod_name = mod["module_name"]

        # Check existing sync status
        sync_row = conn.execute(
            "SELECT oldest_date, newest_date, total_readings "
            "FROM netatmo_sync_status WHERE module_mac = ?",
            (mac,)
        ).fetchone()

        if sync_row:
            # Already have some data — fetch only missing ranges
            oldest = date.fromisoformat(sync_row[0]) if sync_row[0] else end_date
            newest = date.fromisoformat(sync_row[1]) if sync_row[1] else start_date

            ranges_to_fetch = []
            # Fetch older data if needed
            if start_date < oldest:
                ranges_to_fetch.append((start_date, oldest))
            # Fetch newer data (incremental)
            if newest < end_date:
                ranges_to_fetch.append((newest, end_date))
        else:
            # No data yet — fetch full range
            ranges_to_fetch = [(start_date, end_date)]

        module_new = 0
        for fetch_start, fetch_end in ranges_to_fetch:
            readings = fetch_module_history(
                token,
                device_id=mod["device_id"],
                module_id=mod["module_id"],
                module_name=mod_name,
                data_types=mod["data_types"],
                start_date=fetch_start,
                end_date=fetch_end,
                scale=scale,
            )
            if readings:
                stored = store_readings(conn, readings)
                module_new += stored

        # Update sync status
        total_existing = (sync_row[2] if sync_row else 0) + module_new
        conn.execute(
            "INSERT OR REPLACE INTO netatmo_sync_status "
            "(module_mac, module_name, oldest_date, newest_date, "
            " total_readings, last_sync) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mac, mod_name, str(start_date), str(end_date),
             total_existing, datetime.utcnow().isoformat() + "Z"))

        total_new += module_new
        summary.append({
            "module_name": mod_name,
            "module_type": mod["module_type"],
            "new_readings": module_new,
            "total_readings": total_existing,
        })

    conn.commit()
    log.info("Netatmo backfill: %d new readings across %d modules",
             total_new, len(summary))

    return {"modules": summary, "total_new_readings": total_new}


def get_sync_status(conn: sqlite3.Connection) -> list[dict]:
    """Return backfill sync status for all modules."""
    try:
        conn.execute(BACKFILL_TABLE_SQL)
        rows = conn.execute(
            "SELECT module_mac, module_name, oldest_date, newest_date, "
            "       total_readings, last_sync "
            "FROM netatmo_sync_status ORDER BY module_name"
        ).fetchall()
        return [dict(zip(
            ["module_mac", "module_name", "oldest_date", "newest_date",
             "total_readings", "last_sync"], r))
            for r in rows]
    except Exception:
        return []


# ── High-Level Orchestrators ─────────────────────────────────────────────────

def fetch_all_modules(client_id: str, client_secret: str,
                      conn: sqlite3.Connection,
                      start_date: date, end_date: date,
                      scale: str = "30min") -> dict:
    """
    Fetch historical data for ALL modules and store in DB.

    Returns summary: {modules: [...], total_readings: int}
    """
    token = get_access_token(client_id, client_secret)
    if not token:
        return {"error": "No valid token. Run --auth first.", "modules": []}

    stations = get_stations(token)
    if not stations:
        return {"error": "No stations found", "modules": []}

    create_netatmo_tables(conn)
    summary = []
    total = 0

    for mod in stations:
        readings = fetch_module_history(
            token,
            device_id=mod["device_id"],
            module_id=mod["module_id"],
            module_name=mod["module_name"],
            data_types=mod["data_types"],
            start_date=start_date,
            end_date=end_date,
            scale=scale,
        )

        stored = store_readings(conn, readings)
        total += stored
        summary.append({
            "module_name": mod["module_name"],
            "module_type": mod["module_type"],
            "readings_fetched": len(readings),
        })
        log.info("Netatmo: %s (%s): %d readings stored",
                 mod["module_name"], mod["module_type"], stored)

    return {"modules": summary, "total_readings": total}


# ── CLI Entry Point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Netatmo Weather Station Client")
    parser.add_argument("--auth", action="store_true",
                        help="Run OAuth2 authorization flow")
    parser.add_argument("--list", action="store_true",
                        help="List all stations and modules with MAC addresses")
    parser.add_argument("--fetch", action="store_true",
                        help="Fetch historical data for all modules")
    parser.add_argument("--days", type=int, default=7,
                        help="Number of days to fetch (default: 7)")
    parser.add_argument("--client-id", type=str,
                        help="Netatmo client_id")
    parser.add_argument("--client-secret", type=str,
                        help="Netatmo client_secret")
    args = parser.parse_args()

    # Load config for credentials
    config_path = SCRIPT_DIR / "airzone_config.json"
    if not config_path.exists():
        config_path = SCRIPT_DIR / "data" / "airzone_config.json"
    if config_path.exists():
        cfg = json.loads(config_path.read_text())
    else:
        cfg = {}

    client_id = args.client_id or cfg.get("netatmo_client_id", "")
    client_secret = args.client_secret or cfg.get("netatmo_client_secret", "")

    if not client_id or not client_secret:
        print("Error: netatmo_client_id and netatmo_client_secret required.")
        print("Add them to airzone_config.json or pass "
              "--client-id / --client-secret")
        exit(1)

    if args.auth:
        ok = run_auth_flow(client_id, client_secret)
        print("Authorization", "successful!" if ok else "FAILED")
        exit(0 if ok else 1)

    token = get_access_token(client_id, client_secret)
    if not token:
        print("No valid token. Run with --auth first.")
        exit(1)

    if args.list:
        stations = get_stations(token)
        for s in stations:
            mid = s["module_id"] or "(base station)"
            mtype = MODULE_TYPES.get(s["module_type"], s["module_type"])
            print(f"  {s['module_name']:20s}  {mid:20s}  {mtype}")
            dash = s.get("dashboard", {})
            if "Temperature" in dash:
                print(f"    Temperature: {dash['Temperature']}C  "
                      f"Humidity: {dash.get('Humidity', '?')}%  "
                      f"CO2: {dash.get('CO2', 'n/a')} ppm")

    if args.fetch:
        db_path = SCRIPT_DIR / "airzone_history.db"
        conn = sqlite3.connect(str(db_path))
        end = date.today()
        start = end - timedelta(days=args.days)
        result = fetch_all_modules(client_id, client_secret, conn,
                                   start, end)
        print(f"\nFetched {result['total_readings']} total readings:")
        for m in result.get("modules", []):
            print(f"  {m['module_name']}: {m['readings_fetched']} readings")
        conn.close()

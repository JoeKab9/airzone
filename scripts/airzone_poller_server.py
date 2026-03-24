#!/usr/bin/env python3
"""
Airzone Data Collector — Headless Server
==========================================
Polls Airzone Cloud every 5 minutes and stores all raw data in SQLite.
Designed to run as a systemd service on Linux.

Usage:
    python3 airzone_poller_server.py              # run forever
    python3 airzone_poller_server.py --once        # single poll then exit
    python3 airzone_poller_server.py --stats       # show DB stats then exit
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency:  pip install requests")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "airzone_raw.db"
ENV_PATH = BASE_DIR / ".env"
LOG_PATH = BASE_DIR / "logs" / "poller.log"
DEFAULT_INTERVAL = 300  # 5 minutes

CLOUD_BASE = "https://m.airzonecloud.com"
LOGIN_PATH = "/api/v1/auth/login"
REFRESH_PATH = "/api/v1/auth/refreshToken"
INSTALLATIONS_PATH = "/api/v1/installations"

MODE_NAMES = {1: "stop", 2: "cool", 3: "heat", 4: "fan", 5: "dry", 7: "auto"}

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_PATH), encoding="utf-8"),
    ],
)
log = logging.getLogger("airzone-poller")


# ── .env loader ───────────────────────────────────────────────────────────────

def load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
        if m:
            key, val = m.group(1), m.group(2).strip().strip("\"'")
            if val and key not in os.environ:
                os.environ[key] = val


load_dotenv(ENV_PATH)


# ── Airzone Cloud API (self-contained, no external imports) ───────────────────

class AirzoneCloudAPI:
    def __init__(self, timeout: int = 15):
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.timeout = timeout
        self.token: str = ""
        self.refresh_token: str = ""
        self.token_expiry: datetime = datetime.min

    def login(self, email: str, password: str):
        resp = self.session.post(
            CLOUD_BASE + LOGIN_PATH,
            json={"email": email, "password": password},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._store_tokens(data.get("token", ""), data.get("refreshToken", ""))

    def _store_tokens(self, token: str, refresh: str):
        self.token = token
        self.refresh_token = refresh
        self.token_expiry = datetime.now() + timedelta(hours=12)
        self.session.headers["Authorization"] = f"Bearer {self.token}"

    def ensure_token(self, email: str, password: str):
        if self.token and datetime.now() < self.token_expiry:
            return
        if self.refresh_token:
            try:
                resp = self.session.post(
                    CLOUD_BASE + REFRESH_PATH,
                    json={"token": self.token, "refreshToken": self.refresh_token},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                self._store_tokens(data.get("token", ""), data.get("refreshToken", ""))
                return
            except Exception:
                pass
        self.login(email, password)

    def get_installations(self) -> list[dict]:
        resp = self.session.get(CLOUD_BASE + INSTALLATIONS_PATH, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("installations", [])

    def get_installation_detail(self, installation_id: str) -> dict:
        resp = self.session.get(
            f"{CLOUD_BASE}{INSTALLATIONS_PATH}/{installation_id}",
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("installation", data)

    def get_device_status(self, device_id: str, installation_id: str) -> dict:
        url_id = urllib.parse.quote(device_id)
        resp = self.session.get(
            f"{CLOUD_BASE}/api/v1/devices/{url_id}/status",
            params={"installation_id": installation_id},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


# ── Database ──────────────────────────────────────────────────────────────────

def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS poll_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            poll_type   TEXT NOT NULL,
            raw_json    TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_ts ON poll_snapshots(timestamp)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS zone_readings (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL,
            installation_id     TEXT,
            installation_name   TEXT,
            device_id           TEXT NOT NULL,
            zone_name           TEXT,
            device_type         TEXT,
            temperature         REAL,
            humidity            INTEGER,
            setpoint_heat       REAL,
            setpoint_cool       REAL,
            power               INTEGER,
            mode                INTEGER,
            mode_name           TEXT,
            is_connected        INTEGER,
            air_active          INTEGER,
            aq_quality          REAL,
            raw_json            TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zr_ts ON zone_readings(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zr_device ON zone_readings(device_id, timestamp)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS installation_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL,
            installation_id     TEXT NOT NULL,
            installation_name   TEXT,
            raw_json            TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inst_ts ON installation_snapshots(timestamp)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dhw_readings (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL,
            installation_id     TEXT,
            device_id           TEXT NOT NULL,
            device_type         TEXT,
            power               INTEGER,
            setpoint            REAL,
            tank_temp           REAL,
            raw_json            TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dhw_ts ON dhw_readings(timestamp)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS other_devices (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL,
            installation_id     TEXT,
            device_id           TEXT NOT NULL,
            device_type         TEXT,
            device_name         TEXT,
            raw_json            TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_other_ts ON other_devices(timestamp)")

    conn.commit()
    return conn


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_celsius(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, dict):
        return val.get("celsius")
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Poller ────────────────────────────────────────────────────────────────────

def poll_once(api: AirzoneCloudAPI, conn: sqlite3.Connection,
              email: str, password: str, interval: int = DEFAULT_INTERVAL):
    api.ensure_token(email, password)
    now = _now_iso()
    total_zones = total_dhw = total_other = 0

    installations = api.get_installations()
    conn.execute(
        "INSERT INTO poll_snapshots (timestamp, poll_type, raw_json) VALUES (?, ?, ?)",
        (now, "installations_list", json.dumps(installations, default=str)),
    )

    # Collect all devices
    all_devices = []
    for inst in installations:
        inst_id = inst.get("installation_id") or inst.get("id", "")
        inst_name = inst.get("name", "")
        try:
            detail = api.get_installation_detail(inst_id)
        except Exception as e:
            log.warning("Install detail failed for %s: %s", inst_id, e)
            continue

        conn.execute(
            "INSERT INTO installation_snapshots "
            "(timestamp, installation_id, installation_name, raw_json) VALUES (?, ?, ?, ?)",
            (now, inst_id, inst_name, json.dumps(detail, default=str)),
        )
        for group in detail.get("groups", []):
            for device in group.get("devices", []):
                all_devices.append((inst_id, inst_name, device))

    # Spread calls over 80% of interval
    n = len(all_devices)
    spread = max(25.0, min(45.0, (interval * 0.8) / n)) if n > 1 else 0
    log.info("Fetching %d devices (%ds apart)...", n, int(spread))

    for idx, (inst_id, inst_name, device) in enumerate(all_devices):
        dev_type = device.get("type", "")
        dev_id = device.get("device_id") or device.get("id", "")
        dev_name = device.get("name", "")

        if idx > 0:
            time.sleep(spread)

        # Fetch with retry on 429
        status = {}
        for attempt in range(3):
            if attempt > 0:
                time.sleep(15.0)
            try:
                status = api.get_device_status(dev_id, inst_id)
                break
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    log.warning("Rate-limited on %s, retry %d/2...", dev_name, attempt + 1)
                    continue
                log.warning("Status failed: %s (%s): %s", dev_name, dev_id, e)
                break

        merged = {**device, **status}
        raw = json.dumps(merged, default=str)
        reading_ts = _now_iso()

        if dev_type == "az_zone":
            temp = _extract_celsius(merged.get("local_temp") or merged.get("roomTemp"))
            humidity = merged.get("humidity")
            sp_heat = _extract_celsius(merged.get("setpoint_air_heat"))
            sp_cool = _extract_celsius(merged.get("setpoint_air_cool"))
            power = 1 if merged.get("power") else 0
            mode = merged.get("mode")
            mode_name = MODE_NAMES.get(mode, str(mode))
            connected = 1 if merged.get("isConnected") else 0
            air_active = 1 if merged.get("air_active") else 0
            aq = merged.get("aq_quality")

            conn.execute(
                "INSERT INTO zone_readings "
                "(timestamp, installation_id, installation_name, device_id, zone_name, "
                " device_type, temperature, humidity, setpoint_heat, setpoint_cool, "
                " power, mode, mode_name, is_connected, air_active, aq_quality, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (reading_ts, inst_id, inst_name, dev_id, dev_name, dev_type,
                 temp, humidity, sp_heat, sp_cool, power, mode, mode_name,
                 connected, air_active, aq, raw),
            )
            log.info("  %s: %.1fC, %s%%, sp=%sC, power=%s, mode=%s",
                     dev_name, temp or 0, humidity, sp_heat, "ON" if power else "OFF", mode_name)
            total_zones += 1

        elif dev_type in ("az_acs", "aidoo_acs"):
            dhw_power = 1 if merged.get("power") or merged.get("acs_power") else 0
            dhw_sp = _extract_celsius(
                merged.get("setpoint_air_heat") or merged.get("setpoint") or merged.get("acs_setpoint"))
            tank = _extract_celsius(
                merged.get("zone_work_temp") or merged.get("local_temp")
                or merged.get("tank_temp") or merged.get("acs_temp"))

            conn.execute(
                "INSERT INTO dhw_readings "
                "(timestamp, installation_id, device_id, device_type, power, setpoint, tank_temp, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (reading_ts, inst_id, dev_id, dev_type, dhw_power, dhw_sp, tank, raw),
            )
            log.info("  DHW %s: tank=%sC, sp=%sC, power=%s",
                     dev_name, tank, dhw_sp, "ON" if dhw_power else "OFF")
            total_dhw += 1

        else:
            conn.execute(
                "INSERT INTO other_devices "
                "(timestamp, installation_id, device_id, device_type, device_name, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (reading_ts, inst_id, dev_id, dev_type, dev_name, raw),
            )
            total_other += 1

    conn.commit()
    log.info("Poll OK: %d zones, %d DHW, %d other", total_zones, total_dhw, total_other)


def show_stats(db_path: Path):
    if not db_path.exists():
        print("No database file yet.")
        return
    conn = sqlite3.connect(str(db_path))
    zones = conn.execute("SELECT COUNT(*) FROM zone_readings").fetchone()[0]
    dhw = conn.execute("SELECT COUNT(*) FROM dhw_readings").fetchone()[0]
    other = conn.execute("SELECT COUNT(*) FROM other_devices").fetchone()[0]
    snaps = conn.execute("SELECT COUNT(*) FROM poll_snapshots").fetchone()[0]
    first = conn.execute("SELECT MIN(timestamp) FROM zone_readings").fetchone()[0]
    last = conn.execute("SELECT MAX(timestamp) FROM zone_readings").fetchone()[0]
    distinct = conn.execute("SELECT COUNT(DISTINCT zone_name) FROM zone_readings").fetchone()[0]
    conn.close()
    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    print(f"Database: {db_path}")
    print(f"  Size:          {size_mb:.2f} MB")
    print(f"  Zone readings: {zones:,}")
    print(f"  DHW readings:  {dhw:,}")
    print(f"  Other devices: {other:,}")
    print(f"  Snapshots:     {snaps:,}")
    print(f"  Distinct zones: {distinct}")
    print(f"  First reading: {first or 'N/A'}")
    print(f"  Last reading:  {last or 'N/A'}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Airzone Data Collector (headless)")
    parser.add_argument("--once", action="store_true", help="Single poll then exit")
    parser.add_argument("--stats", action="store_true", help="Show DB stats then exit")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="Poll interval in seconds (default: 300)")
    args = parser.parse_args()

    if args.stats:
        show_stats(DB_PATH)
        return

    email = os.environ.get("AIRZONE_EMAIL", "")
    password = os.environ.get("AIRZONE_PASSWORD", "")
    if not email or not password:
        log.error("Set AIRZONE_EMAIL and AIRZONE_PASSWORD in %s", ENV_PATH)
        sys.exit(1)

    api = AirzoneCloudAPI()
    log.info("Authenticating to Airzone Cloud...")
    api.login(email, password)
    log.info("Authenticated OK")

    conn = init_db(DB_PATH)
    log.info("Database: %s", DB_PATH)

    if args.once:
        poll_once(api, conn, email, password, interval=args.interval)
        conn.close()
        return

    # Graceful shutdown
    stop = False

    def _sigterm(signum, frame):
        nonlocal stop
        log.info("Received signal %d, shutting down...", signum)
        stop = True

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    log.info("Starting poller (interval=%ds)...", args.interval)

    while not stop:
        try:
            poll_once(api, conn, email, password, interval=args.interval)
        except Exception as e:
            log.error("Poll failed: %s", e, exc_info=True)

        # Wait for next cycle (check stop every second)
        for _ in range(30):
            if stop:
                break
            time.sleep(1)

    conn.close()
    log.info("Poller stopped.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Airzone Raw Data Collector
============================
Standalone script that polls Airzone Cloud every 5 minutes and stores
ALL raw data into a portable SQLite database.

Usage:
    python scripts/airzone_collector.py              # run forever (5 min intervals)
    python scripts/airzone_collector.py --once       # single poll then exit
    python scripts/airzone_collector.py --interval 60  # custom interval in seconds

The database is easy to copy to another machine (Mac, etc.) — it's a single file.
"""
from __future__ import annotations

import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Make src/ importable ──────────────────────────────────────────────────────
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from airzone_humidity_controller import AirzoneCloudAPI, load_config  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = Path(r"C:\Users\mauri\Applications\Airzone data\airzone_raw.db")
DEFAULT_INTERVAL = 300  # 5 minutes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("collector")

# ── Database ──────────────────────────────────────────────────────────────────
import sqlite3


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create SQLite database and tables for raw Airzone data."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # Raw poll snapshots — stores every field Airzone returns as JSON
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poll_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            poll_type   TEXT NOT NULL,
            raw_json    TEXT NOT NULL
        )
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_snap_ts
        ON poll_snapshots(timestamp)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_snap_type
        ON poll_snapshots(poll_type)""")

    # Parsed zone readings — one row per zone per poll
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
            humidity             INTEGER,
            setpoint            REAL,
            power               INTEGER,
            mode                INTEGER,
            mode_name           TEXT,
            raw_json            TEXT NOT NULL
        )
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_zr_ts
        ON zone_readings(timestamp)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_zr_device
        ON zone_readings(device_id, timestamp)""")

    # Parsed installation data — one row per installation per poll
    conn.execute("""
        CREATE TABLE IF NOT EXISTS installation_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL,
            installation_id     TEXT NOT NULL,
            installation_name   TEXT,
            raw_json            TEXT NOT NULL
        )
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_inst_ts
        ON installation_snapshots(timestamp)""")

    # DHW (hot water) readings
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
        )
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_dhw_ts
        ON dhw_readings(timestamp)""")

    # All non-zone devices (system units, gateways, etc.)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS other_devices (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL,
            installation_id     TEXT,
            device_id           TEXT NOT NULL,
            device_type         TEXT,
            device_name         TEXT,
            raw_json            TEXT NOT NULL
        )
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_other_ts
        ON other_devices(timestamp)""")

    conn.commit()
    return conn


# ── Helpers ───────────────────────────────────────────────────────────────────

MODE_NAMES = {1: "stop", 2: "cool", 3: "heat", 4: "fan", 5: "dry", 7: "auto"}


def _extract_celsius(val) -> float | None:
    """Extract temperature from float or {"celsius": N} format."""
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


# ── Collector ─────────────────────────────────────────────────────────────────

class AirzoneCollector:
    def __init__(self, db_path: Path = DB_PATH):
        self.api = AirzoneCloudAPI()
        self.conn = init_db(db_path)
        self.cfg = load_config()
        self._authenticated = False

    def authenticate(self):
        """Login to Airzone Cloud using saved credentials."""
        email = self.cfg.get("email", "")
        password = self.cfg.get("password", "")
        if not email or not password:
            log.error("No Airzone credentials found. Set AIRZONE_EMAIL and "
                      "AIRZONE_PASSWORD in .env or data/airzone_config.json")
            sys.exit(1)

        if not self.api.load_cached_tokens():
            self.api.login(email, password)
        else:
            self.api.ensure_token(email, password)

        self._authenticated = True
        log.info("Authenticated to Airzone Cloud")

    def poll_all(self):
        """Single poll: fetch everything from Airzone and store it."""
        if not self._authenticated:
            self.authenticate()

        # Re-ensure token is valid
        self.api.ensure_token(self.cfg["email"], self.cfg["password"])

        now = _now_iso()
        total_zones = 0
        total_dhw = 0
        total_other = 0

        try:
            installations = self.api.get_installations()
        except Exception as e:
            log.error("Failed to fetch installations: %s", e)
            self._authenticated = False
            return

        # Store raw installations list
        self.conn.execute(
            "INSERT INTO poll_snapshots (timestamp, poll_type, raw_json) "
            "VALUES (?, ?, ?)",
            (now, "installations_list", json.dumps(installations, default=str))
        )

        for inst in installations:
            inst_id = inst.get("installation_id") or inst.get("id", "")
            inst_name = inst.get("name", "")

            # Fetch full installation detail
            try:
                detail = self.api.get_installation_detail(inst_id)
            except Exception as e:
                log.warning("Failed to get detail for installation %s: %s",
                            inst_id, e)
                continue

            # Store raw installation detail
            self.conn.execute(
                "INSERT INTO installation_snapshots "
                "(timestamp, installation_id, installation_name, raw_json) "
                "VALUES (?, ?, ?, ?)",
                (now, inst_id, inst_name, json.dumps(detail, default=str))
            )

            # Walk all groups and devices
            for group in detail.get("groups", []):
                for device in group.get("devices", []):
                    dev_type = device.get("type", "")
                    dev_id = device.get("device_id") or device.get("id", "")
                    dev_name = device.get("name", "")

                    # Fetch live status for every device
                    try:
                        status = self.api.get_device_status(dev_id, inst_id)
                    except Exception as e:
                        log.warning("Status fetch failed for %s (%s): %s",
                                    dev_name, dev_id, e)
                        status = {}

                    merged = {**device, **status}
                    raw = json.dumps(merged, default=str)

                    if dev_type == "az_zone":
                        # Zone device
                        temp = _extract_celsius(
                            merged.get("local_temp") or merged.get("roomTemp"))
                        humidity = merged.get("humidity")
                        setpoint = _extract_celsius(merged.get("setpoint"))
                        power = 1 if merged.get("power") else 0
                        mode = merged.get("mode")
                        mode_name = MODE_NAMES.get(mode, str(mode))

                        self.conn.execute(
                            "INSERT INTO zone_readings "
                            "(timestamp, installation_id, installation_name, "
                            " device_id, zone_name, device_type, temperature, "
                            " humidity, setpoint, power, mode, mode_name, raw_json) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (now, inst_id, inst_name, dev_id, dev_name,
                             dev_type, temp, humidity, setpoint, power,
                             mode, mode_name, raw)
                        )
                        total_zones += 1

                    elif dev_type in ("az_acs", "aidoo_acs"):
                        # DHW device
                        power = 1 if merged.get("power") or merged.get("acs_power") else 0
                        setpoint = _extract_celsius(
                            merged.get("setpoint") or merged.get("acs_setpoint"))
                        tank_temp = _extract_celsius(
                            merged.get("tank_temp") or merged.get("acs_temp"))

                        self.conn.execute(
                            "INSERT INTO dhw_readings "
                            "(timestamp, installation_id, device_id, device_type, "
                            " power, setpoint, tank_temp, raw_json) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (now, inst_id, dev_id, dev_type,
                             power, setpoint, tank_temp, raw)
                        )
                        total_dhw += 1

                    else:
                        # Other device types (system units, gateways, etc.)
                        self.conn.execute(
                            "INSERT INTO other_devices "
                            "(timestamp, installation_id, device_id, device_type, "
                            " device_name, raw_json) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (now, inst_id, dev_id, dev_type, dev_name, raw)
                        )
                        total_other += 1

        self.conn.commit()
        log.info("Poll complete: %d zones, %d DHW, %d other devices",
                 total_zones, total_dhw, total_other)

    def run_forever(self, interval: int = DEFAULT_INTERVAL):
        """Poll in a loop until interrupted."""
        log.info("Starting Airzone collector (interval=%ds, db=%s)",
                 interval, DB_PATH)
        self.authenticate()

        while True:
            try:
                self.poll_all()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error("Poll failed: %s", e)
                self._authenticated = False

            log.info("Next poll in %d seconds...", interval)
            time.sleep(interval)

    def close(self):
        self.conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Airzone raw data collector")
    parser.add_argument("--once", action="store_true",
                        help="Single poll then exit")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Poll interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--db", type=str, default=str(DB_PATH),
                        help=f"Database path (default: {DB_PATH})")
    args = parser.parse_args()

    db_path = Path(args.db)
    collector = AirzoneCollector(db_path=db_path)

    # Graceful shutdown
    def _shutdown(sig, frame):
        log.info("Shutting down...")
        collector.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if args.once:
        collector.authenticate()
        collector.poll_all()
        collector.close()
        log.info("Single poll complete. Database: %s", db_path)
    else:
        try:
            collector.run_forever(interval=args.interval)
        finally:
            collector.close()


if __name__ == "__main__":
    main()

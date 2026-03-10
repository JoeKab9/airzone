"""
Airzone History Database
=========================
SQLite logger for zone temperature/humidity readings and control actions.
Used by the Pi daemon to record data, and by the web dashboard to serve graphs.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Make src/ importable for analytics module
_SRC_DIR = Path(__file__).parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from airzone_analytics import create_analytics_tables, migrate_analytics_tables  # noqa: E402
from airzone_linky import create_linky_tables  # noqa: E402
try:
    from airzone_control_brain import create_brain_tables  # noqa: E402
    _HAS_BRAIN = True
except ImportError:
    _HAS_BRAIN = False

try:
    from airzone_thermal_model import create_prediction_tables  # noqa: E402
    _HAS_THERMAL = True
except ImportError:
    _HAS_THERMAL = False

try:
    from airzone_baseline import create_baseline_tables  # noqa: E402
    _HAS_BASELINE = True
except ImportError:
    _HAS_BASELINE = False

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_DB_PATH = DATA_DIR / "airzone_history.db"


class HistoryDB:
    """Read/write interface to the Airzone history SQLite database."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        create_analytics_tables(self.conn)
        migrate_analytics_tables(self.conn)
        create_linky_tables(self.conn)
        if _HAS_BRAIN:
            create_brain_tables(self.conn)
        if _HAS_THERMAL:
            create_prediction_tables(self.conn)
        if _HAS_BASELINE:
            create_baseline_tables(self.conn)
        self._migrate()

    def _migrate(self):
        """Add columns introduced after initial schema."""
        cursor = self.conn.execute("PRAGMA table_info(zone_readings)")
        existing = {row[1] for row in cursor.fetchall()}
        if "outdoor_dew_point" not in existing:
            self.conn.execute(
                "ALTER TABLE zone_readings ADD COLUMN outdoor_dew_point REAL")
        if "outdoor_humidity" not in existing:
            self.conn.execute(
                "ALTER TABLE zone_readings ADD COLUMN outdoor_humidity REAL")
        self.conn.commit()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS zone_readings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT    NOT NULL,
                zone_name    TEXT    NOT NULL,
                device_id    TEXT    NOT NULL,
                temperature  REAL,
                humidity     INTEGER,
                power        INTEGER NOT NULL DEFAULT 0,
                mode         INTEGER,
                setpoint     REAL,
                outdoor_temp REAL
            );

            CREATE INDEX IF NOT EXISTS idx_readings_ts
                ON zone_readings(timestamp);
            CREATE INDEX IF NOT EXISTS idx_readings_zone_ts
                ON zone_readings(zone_name, timestamp);

            CREATE TABLE IF NOT EXISTS control_actions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT    NOT NULL,
                zone_name    TEXT    NOT NULL,
                device_id    TEXT    NOT NULL,
                action       TEXT    NOT NULL,
                humidity     INTEGER,
                reason       TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_actions_ts
                ON control_actions(timestamp);
        """)
        self.conn.commit()

    # ── Write methods (used by daemon) ───────────────────────────────────────

    def log_readings(self, zones: list[dict], outdoor_temp: float | None = None,
                     outdoor_dew_point: float | None = None,
                     outdoor_humidity: float | None = None):
        """Insert one row per zone for the current poll cycle."""
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        rows = []
        for z in zones:
            dev_id = z.get("_device_id", "")
            inst_name = z.get("_installation_name", "")
            name = z.get("name", dev_id)
            zone_name = f"{inst_name}/{name}" if inst_name else name

            # Extract temperature (can be float or {"celsius": N})
            local_temp = z.get("local_temp")
            if isinstance(local_temp, dict):
                temp = local_temp.get("celsius")
            else:
                temp = local_temp

            humidity = z.get("humidity")
            power = 1 if z.get("power") else 0
            mode = z.get("mode")

            # Extract setpoint
            sp = z.get("setpoint")
            if isinstance(sp, dict):
                sp = sp.get("celsius")

            rows.append((now, zone_name, dev_id, temp, humidity,
                         power, mode, sp, outdoor_temp, outdoor_dew_point,
                         outdoor_humidity))

        self.conn.executemany(
            "INSERT INTO zone_readings "
            "(timestamp, zone_name, device_id, temperature, humidity, "
            " power, mode, setpoint, outdoor_temp, outdoor_dew_point, "
            " outdoor_humidity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def log_action(self, zone_name: str, device_id: str, action: str,
                   humidity: int | None = None, reason: str = ""):
        """Log a control action (heating_on, heating_off, deferred, emergency)."""
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        self.conn.execute(
            "INSERT INTO control_actions "
            "(timestamp, zone_name, device_id, action, humidity, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, zone_name, device_id, action, humidity, reason),
        )
        self.conn.commit()

    # ── Read methods (used by dashboard) ─────────────────────────────────────

    def get_zone_names(self) -> list[str]:
        """Return all distinct zone names in the DB."""
        rows = self.conn.execute(
            "SELECT DISTINCT zone_name FROM zone_readings ORDER BY zone_name"
        ).fetchall()
        return [r["zone_name"] for r in rows]

    def get_latest(self) -> list[dict]:
        """Return the most recent reading for each zone."""
        rows = self.conn.execute("""
            SELECT r.*
            FROM zone_readings r
            INNER JOIN (
                SELECT zone_name, MAX(timestamp) AS max_ts
                FROM zone_readings
                GROUP BY zone_name
            ) latest ON r.zone_name = latest.zone_name
                    AND r.timestamp = latest.max_ts
            ORDER BY r.zone_name
        """).fetchall()
        return [dict(r) for r in rows]

    def get_readings(self, zone_name: str | None = None,
                     hours: int = 168) -> list[dict]:
        """Fetch readings for the last N hours. If zone_name is None, all zones."""
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat(
            timespec="seconds") + "Z"
        if zone_name:
            rows = self.conn.execute(
                "SELECT * FROM zone_readings "
                "WHERE zone_name = ? AND timestamp >= ? "
                "ORDER BY timestamp",
                (zone_name, cutoff),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM zone_readings "
                "WHERE timestamp >= ? "
                "ORDER BY timestamp",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_actions(self, hours: int = 168) -> list[dict]:
        """Return recent control actions."""
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat(
            timespec="seconds") + "Z"
        rows = self.conn.execute(
            "SELECT * FROM control_actions "
            "WHERE timestamp >= ? "
            "ORDER BY timestamp DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Return database statistics."""
        total = self.conn.execute(
            "SELECT COUNT(*) AS c FROM zone_readings").fetchone()["c"]
        first = self.conn.execute(
            "SELECT MIN(timestamp) AS t FROM zone_readings").fetchone()["t"]
        last = self.conn.execute(
            "SELECT MAX(timestamp) AS t FROM zone_readings").fetchone()["t"]
        db_size = os.path.getsize(self.db_path) if self.db_path.exists() else 0
        return {
            "total_readings": total,
            "first_reading": first,
            "last_reading": last,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
        }

    # ── Analytics methods ───────────────────────────────────────────────────

    def get_zone_profiles(self) -> list[dict]:
        """Return analytics profile for each zone with heating cycles."""
        from airzone_analytics import compute_zone_profile
        zone_names = self.get_zone_names()
        profiles = []
        for name in zone_names:
            p = compute_zone_profile(self.conn, name, days=30)
            if p["cycles_30d"] > 0:
                profiles.append(p)
        return profiles

    def get_heating_cycles(self, hours: int = 168,
                           zone_name: str | None = None) -> list[dict]:
        """Return recent heating cycles."""
        from airzone_analytics import get_recent_cycles
        days = max(1, hours // 24)
        return get_recent_cycles(self.conn, days=days, zone_name=zone_name)

    def get_warm_hours_recommendation(self, config: dict) -> dict | None:
        """Return warm hours recommendation or None."""
        from airzone_analytics import compute_optimal_warm_hours
        current = config.get("warm_hours_count", 6)
        return compute_optimal_warm_hours(self.conn, current, days=30)

    def get_optimization_log(self, limit: int = 20) -> list[dict]:
        """Return recent optimization log entries."""
        rows = self.conn.execute(
            "SELECT timestamp, metric, current_value, recommended_value, "
            "       confidence, reasoning, applied "
            "FROM optimization_log ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Energy (Linky) methods ───────────────────────────────────────────────

    def log_linky_readings(self, readings: list[dict]):
        """Bulk insert Linky load curve readings."""
        from airzone_linky import store_load_curve
        return store_load_curve(self.conn, readings)

    def get_energy_readings(self, hours: int = 168) -> list[dict]:
        """Return raw Linky readings for chart."""
        from airzone_linky import get_energy_readings
        return get_energy_readings(self.conn, hours=hours)

    def get_energy_analysis(self, days: int = 30) -> list[dict]:
        """Return daily energy analysis."""
        from airzone_linky import get_energy_analysis
        return get_energy_analysis(self.conn, days=days)

    def get_temp_band_efficiency(self, days: int = 30) -> list[dict]:
        """Return kWh/h by outdoor temperature band."""
        from airzone_linky import get_temp_band_efficiency
        return get_temp_band_efficiency(self.conn, days=days)

    def close(self):
        self.conn.close()

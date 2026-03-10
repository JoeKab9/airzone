#!/usr/bin/env python3
"""
Airzone Supabase -> Local SQLite Migration
============================================
Pulls ALL historical data from Supabase into the local SQLite database.

Uses only the `requests` library (no supabase-py dependency).
Fetches paginated data from the Supabase REST API and maps columns
to the local SQLite schema, handling deduplication via INSERT OR IGNORE
or primary key checks.

Usage:
    python3 src/airzone_migrate_supabase.py
    python3 src/airzone_migrate_supabase.py --supabase-url URL --service-key KEY
    python3 src/airzone_migrate_supabase.py --db-path /path/to/airzone_history.db
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library is required. Install with: pip install requests")
    sys.exit(1)

# Make src/ importable
_SRC_DIR = Path(__file__).parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

log = logging.getLogger("airzone.migrate")

# Default DB path (same as pi/airzone_db.py)
DEFAULT_DB_PATH = Path(__file__).parent.parent / "pi" / "data" / "airzone_history.db"
DEFAULT_SUPABASE_URL = "https://jywzwsnzyyqqadhssvrm.supabase.co"

# Page size for Supabase REST API pagination
PAGE_SIZE = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_keychain_value(key: str) -> str:
    """Try to read a value from the macOS keychain via airzone_secrets."""
    try:
        from airzone_secrets import secrets as sec
        return sec.get(key, "")
    except Exception:
        return ""


def _ensure_tables(conn: sqlite3.Connection):
    """Ensure all target tables exist in the local SQLite database."""
    # Import table creators from the existing codebase
    try:
        from airzone_control_brain import create_brain_tables
        create_brain_tables(conn)
    except ImportError:
        log.warning("Could not import create_brain_tables; "
                    "control_log/daily_assessment/system_state/dp_spread_predictions "
                    "tables may not exist")

    try:
        from airzone_thermal_model import create_prediction_tables
        create_prediction_tables(conn)
    except ImportError:
        log.warning("Could not import create_prediction_tables; "
                    "thermal_models/dp_predict_coefficients tables may not exist")

    try:
        from airzone_baseline import create_baseline_tables
        create_baseline_tables(conn)
    except ImportError:
        log.warning("Could not import create_baseline_tables; "
                    "energy_baseline/heating_experiments tables may not exist")

    try:
        from airzone_netatmo import create_netatmo_tables
        create_netatmo_tables(conn)
        # Also create backfill tracking table
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS netatmo_sync_status (
                module_mac   TEXT PRIMARY KEY,
                module_name  TEXT,
                oldest_date  TEXT,
                newest_date  TEXT,
                total_readings INTEGER DEFAULT 0,
                last_sync    TEXT
            );
        """)
        conn.commit()
    except ImportError:
        log.warning("Could not import create_netatmo_tables; "
                    "netatmo_readings table may not exist")


# ---------------------------------------------------------------------------
# SupabaseMigrator
# ---------------------------------------------------------------------------

class SupabaseMigrator:
    """Fetches data from Supabase REST API and inserts into local SQLite."""

    def __init__(self, supabase_url: str, service_key: str,
                 db_path: str | Path = DEFAULT_DB_PATH):
        self.url = supabase_url.rstrip("/")
        self.key = service_key
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        _ensure_tables(self.conn)

        # Statistics
        self.stats: dict[str, dict] = {}

    @property
    def _headers(self) -> dict:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    # -- Generic paginated fetch -----------------------------------------------

    def _fetch_table(self, table: str, order_col: str = "created_at",
                     select: str = "*") -> list[dict]:
        """
        Fetch ALL rows from a Supabase table using Range-header pagination.

        Returns the full list of rows as dicts.
        """
        all_rows: list[dict] = []
        offset = 0

        while True:
            headers = {
                **self._headers,
                "Range": f"{offset}-{offset + PAGE_SIZE - 1}",
                "Prefer": "count=exact",
            }
            params = {
                "select": select,
                "order": f"{order_col}.asc",
            }

            try:
                resp = requests.get(
                    f"{self.url}/rest/v1/{table}",
                    headers=headers,
                    params=params,
                    timeout=60,
                )
            except requests.RequestException as e:
                log.error("Network error fetching %s (offset %d): %s",
                          table, offset, e)
                break

            if resp.status_code == 416:
                # Range not satisfiable = no more rows
                break

            if resp.status_code not in (200, 206):
                log.error("Supabase API error for %s: %d %s",
                          table, resp.status_code, resp.text[:300])
                break

            rows = resp.json()
            if not rows:
                break

            all_rows.extend(rows)

            # Parse Content-Range to detect end
            # Format: "0-999/1500" or "0-999/*"
            content_range = resp.headers.get("Content-Range", "")
            if "/" in content_range:
                parts = content_range.split("/")
                total_str = parts[-1]
                if total_str != "*":
                    total = int(total_str)
                    if offset + len(rows) >= total:
                        break

            if len(rows) < PAGE_SIZE:
                # Fewer rows than requested = last page
                break

            offset += PAGE_SIZE
            time.sleep(0.1)  # Courtesy delay

        return all_rows

    # -- control_log -----------------------------------------------------------

    def migrate_control_log(self) -> int:
        """
        Migrate Supabase control_log -> local control_log.

        Supabase columns:
            id (UUID), created_at, zone_name, action, humidity_airzone,
            humidity_netatmo, temperature, outdoor_humidity, outdoor_temp,
            dewpoint, dp_spread, forecast_temp_max, forecast_best_hour,
            occupancy_detected, heating_minutes_today, energy_saved_pct,
            reason, success

        Local SQLite columns:
            id (INTEGER AUTO), created_at, zone_name, action,
            humidity_airzone, humidity_netatmo, temperature, dewpoint,
            dp_spread, outdoor_temp, outdoor_humidity, forecast_temp_max,
            forecast_best_hour, occupancy_detected, energy_saved_pct,
            prediction_decision, reason, success
        """
        print("\n[control_log] Fetching from Supabase...")
        rows = self._fetch_table("control_log", order_col="created_at")
        print(f"[control_log] Fetched {len(rows)} rows from Supabase")

        if not rows:
            self.stats["control_log"] = {"fetched": 0, "inserted": 0, "skipped": 0}
            return 0

        # Get existing timestamps+zone for dedup
        existing = set()
        try:
            cursor = self.conn.execute(
                "SELECT created_at, zone_name FROM control_log"
            )
            for r in cursor:
                existing.add((r[0], r[1]))
        except Exception:
            pass

        inserted = 0
        skipped = 0
        for row in rows:
            created_at = row.get("created_at", "")
            zone_name = row.get("zone_name", "")

            if (created_at, zone_name) in existing:
                skipped += 1
                continue

            try:
                self.conn.execute(
                    "INSERT INTO control_log "
                    "(created_at, zone_name, action, humidity_airzone, "
                    " humidity_netatmo, temperature, dewpoint, dp_spread, "
                    " outdoor_temp, outdoor_humidity, forecast_temp_max, "
                    " forecast_best_hour, occupancy_detected, energy_saved_pct, "
                    " reason, success) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        created_at,
                        zone_name,
                        row.get("action"),
                        row.get("humidity_airzone"),
                        row.get("humidity_netatmo"),
                        row.get("temperature"),
                        row.get("dewpoint"),
                        row.get("dp_spread"),
                        row.get("outdoor_temp"),
                        row.get("outdoor_humidity"),
                        row.get("forecast_temp_max"),
                        row.get("forecast_best_hour"),
                        1 if row.get("occupancy_detected") else 0,
                        row.get("energy_saved_pct", 0),
                        row.get("reason"),
                        1 if row.get("success", True) else 0,
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
            except Exception as e:
                log.warning("control_log insert error: %s", e)
                skipped += 1

        self.conn.commit()
        print(f"[control_log] Inserted: {inserted}, Skipped (duplicates): {skipped}")
        self.stats["control_log"] = {
            "fetched": len(rows), "inserted": inserted, "skipped": skipped,
        }
        return inserted

    # -- netatmo_readings ------------------------------------------------------

    def migrate_netatmo_readings(self) -> int:
        """
        Migrate Supabase netatmo_readings -> local netatmo_readings.

        Supabase columns:
            id (UUID), module_name, timestamp, temperature, humidity,
            co2, noise, pressure, created_at

        Local SQLite columns:
            id (INTEGER AUTO), timestamp, module_mac, module_name,
            temperature, humidity, co2, noise, pressure
            UNIQUE(timestamp, module_mac)

        Note: Supabase uses module_name as identifier; local uses module_mac.
        We use module_name as module_mac when the actual MAC is not available
        from Supabase.
        """
        print("\n[netatmo_readings] Fetching from Supabase...")
        rows = self._fetch_table("netatmo_readings", order_col="timestamp")
        print(f"[netatmo_readings] Fetched {len(rows)} rows from Supabase")

        if not rows:
            self.stats["netatmo_readings"] = {"fetched": 0, "inserted": 0, "skipped": 0}
            return 0

        inserted = 0
        skipped = 0

        # Batch insert with INSERT OR IGNORE (UNIQUE constraint on timestamp+module_mac)
        batch = []
        for row in rows:
            ts = row.get("timestamp", "")
            module_name = row.get("module_name", "")
            # Use module_name as module_mac since Supabase doesn't store MAC
            module_mac = module_name

            batch.append((
                ts,
                module_mac,
                module_name,
                row.get("temperature"),
                row.get("humidity"),
                row.get("co2"),
                row.get("noise"),
                row.get("pressure"),
            ))

        # Insert in chunks of 500 for efficiency
        for i in range(0, len(batch), 500):
            chunk = batch[i:i + 500]
            try:
                cursor = self.conn.executemany(
                    "INSERT OR IGNORE INTO netatmo_readings "
                    "(timestamp, module_mac, module_name, temperature, "
                    " humidity, co2, noise, pressure) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    chunk,
                )
                inserted += cursor.rowcount
            except Exception as e:
                log.warning("netatmo_readings batch insert error: %s", e)

        skipped = len(batch) - inserted
        self.conn.commit()
        print(f"[netatmo_readings] Inserted: {inserted}, Skipped (duplicates): {skipped}")
        self.stats["netatmo_readings"] = {
            "fetched": len(rows), "inserted": inserted, "skipped": skipped,
        }
        return inserted

    # -- netatmo_sync_status ---------------------------------------------------

    def migrate_netatmo_sync_status(self) -> int:
        """
        Migrate Supabase netatmo_sync_status -> local netatmo_sync_status.

        Supabase columns:
            module_name (PK), device_id, module_id, module_type,
            last_synced_ts, status, updated_at

        Local SQLite columns:
            module_mac (PK), module_name, oldest_date, newest_date,
            total_readings, last_sync

        The schemas differ significantly. We map module_name -> module_mac
        (as identifier), and use last_synced_ts for newest_date.
        """
        print("\n[netatmo_sync_status] Fetching from Supabase...")
        rows = self._fetch_table("netatmo_sync_status", order_col="module_name")
        print(f"[netatmo_sync_status] Fetched {len(rows)} rows from Supabase")

        if not rows:
            self.stats["netatmo_sync_status"] = {"fetched": 0, "inserted": 0, "skipped": 0}
            return 0

        inserted = 0
        skipped = 0
        for row in rows:
            module_name = row.get("module_name", "")
            # Use module_id as module_mac if available, else module_name
            module_mac = row.get("module_id") or module_name
            last_synced = row.get("last_synced_ts") or row.get("updated_at", "")

            # Convert last_synced timestamp to date if possible
            newest_date = ""
            if last_synced:
                try:
                    if isinstance(last_synced, (int, float)):
                        # Unix timestamp (seconds)
                        dt = datetime.utcfromtimestamp(last_synced)
                    else:
                        dt = datetime.fromisoformat(
                            str(last_synced).replace("Z", "+00:00"))
                    newest_date = str(dt.date())
                except Exception:
                    newest_date = str(last_synced)[:10]

            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO netatmo_sync_status "
                    "(module_mac, module_name, oldest_date, newest_date, "
                    " total_readings, last_sync) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        module_mac,
                        module_name,
                        None,  # oldest_date not available from Supabase
                        newest_date,
                        0,  # total_readings not tracked in Supabase schema
                        last_synced,
                    ),
                )
                inserted += 1
            except Exception as e:
                log.warning("netatmo_sync_status insert error: %s", e)
                skipped += 1

        self.conn.commit()
        print(f"[netatmo_sync_status] Inserted/Updated: {inserted}, Skipped: {skipped}")
        self.stats["netatmo_sync_status"] = {
            "fetched": len(rows), "inserted": inserted, "skipped": skipped,
        }
        return inserted

    # -- dp_spread_predictions -------------------------------------------------

    def migrate_dp_spread_predictions(self) -> int:
        """
        Migrate Supabase dp_spread_predictions -> local dp_spread_predictions.

        Supabase columns:
            id (UUID), zone_name, created_at, predicted_for, hours_ahead,
            predicted_dp_spread, current_dp_spread, current_indoor_temp,
            current_outdoor_temp, predicted_indoor_temp, predicted_outdoor_temp,
            predicted_outdoor_humidity, actual_dp_spread, actual_indoor_temp,
            prediction_error, validated, validated_at, decision_made,
            decision_correct

        Local SQLite columns:
            id (INTEGER AUTO), created_at, zone_name, predicted_for, hours_ahead,
            predicted_dp_spread, predicted_indoor_temp, predicted_outdoor_temp,
            predicted_outdoor_humidity, current_dp_spread, current_indoor_temp,
            current_outdoor_temp, decision_made, actual_dp_spread,
            actual_indoor_temp, prediction_error, validated, validated_at,
            decision_correct
        """
        print("\n[dp_spread_predictions] Fetching from Supabase...")
        rows = self._fetch_table("dp_spread_predictions", order_col="created_at")
        print(f"[dp_spread_predictions] Fetched {len(rows)} rows from Supabase")

        if not rows:
            self.stats["dp_spread_predictions"] = {"fetched": 0, "inserted": 0, "skipped": 0}
            return 0

        # Dedup using (zone_name, predicted_for, hours_ahead)
        existing = set()
        try:
            cursor = self.conn.execute(
                "SELECT zone_name, predicted_for, hours_ahead "
                "FROM dp_spread_predictions"
            )
            for r in cursor:
                existing.add((r[0], r[1], r[2]))
        except Exception:
            pass

        inserted = 0
        skipped = 0
        for row in rows:
            zone_name = row.get("zone_name", "")
            predicted_for = row.get("predicted_for", "")
            hours_ahead = row.get("hours_ahead")

            if (zone_name, predicted_for, hours_ahead) in existing:
                skipped += 1
                continue

            try:
                self.conn.execute(
                    "INSERT INTO dp_spread_predictions "
                    "(created_at, zone_name, predicted_for, hours_ahead, "
                    " predicted_dp_spread, predicted_indoor_temp, "
                    " predicted_outdoor_temp, predicted_outdoor_humidity, "
                    " current_dp_spread, current_indoor_temp, "
                    " current_outdoor_temp, decision_made, actual_dp_spread, "
                    " actual_indoor_temp, prediction_error, validated, "
                    " validated_at, decision_correct) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.get("created_at"),
                        zone_name,
                        predicted_for,
                        hours_ahead,
                        row.get("predicted_dp_spread"),
                        row.get("predicted_indoor_temp"),
                        row.get("predicted_outdoor_temp"),
                        row.get("predicted_outdoor_humidity"),
                        row.get("current_dp_spread"),
                        row.get("current_indoor_temp"),
                        row.get("current_outdoor_temp"),
                        row.get("decision_made"),
                        row.get("actual_dp_spread"),
                        row.get("actual_indoor_temp"),
                        row.get("prediction_error"),
                        1 if row.get("validated") else 0,
                        row.get("validated_at"),
                        row.get("decision_correct"),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
            except Exception as e:
                log.warning("dp_spread_predictions insert error: %s", e)
                skipped += 1

        self.conn.commit()
        print(f"[dp_spread_predictions] Inserted: {inserted}, Skipped (duplicates): {skipped}")
        self.stats["dp_spread_predictions"] = {
            "fetched": len(rows), "inserted": inserted, "skipped": skipped,
        }
        return inserted

    # -- system_state ----------------------------------------------------------

    def migrate_system_state(self) -> int:
        """
        Migrate Supabase system_state -> local system_state.

        Supabase columns:
            key (PK), value (JSONB), updated_at

        Local SQLite columns:
            key (PK), value (TEXT — JSON string), updated_at

        Special handling: Supabase stores JSONB values (dict/list/number),
        which need to be serialized to JSON strings for SQLite.
        """
        print("\n[system_state] Fetching from Supabase...")
        rows = self._fetch_table("system_state", order_col="key")
        print(f"[system_state] Fetched {len(rows)} rows from Supabase")

        if not rows:
            self.stats["system_state"] = {"fetched": 0, "inserted": 0, "skipped": 0}
            return 0

        inserted = 0
        skipped = 0
        for row in rows:
            key = row.get("key", "")
            value = row.get("value")
            updated_at = row.get("updated_at", "")

            if not key:
                skipped += 1
                continue

            # Serialize JSONB value to string for SQLite
            if isinstance(value, (dict, list)):
                value_str = json.dumps(value)
            elif value is None:
                value_str = "null"
            else:
                value_str = str(value)

            try:
                # Use INSERT OR REPLACE to always take the Supabase version
                # (since it's the authoritative source during migration)
                self.conn.execute(
                    "INSERT OR REPLACE INTO system_state "
                    "(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value_str, updated_at),
                )
                inserted += 1
            except Exception as e:
                log.warning("system_state insert error for key '%s': %s", key, e)
                skipped += 1

        self.conn.commit()
        print(f"[system_state] Inserted/Updated: {inserted}, Skipped: {skipped}")
        self.stats["system_state"] = {
            "fetched": len(rows), "inserted": inserted, "skipped": skipped,
        }
        return inserted

    # -- daily_assessment ------------------------------------------------------

    def migrate_daily_assessment(self) -> int:
        """
        Migrate Supabase daily_assessment -> local daily_assessment.

        Supabase columns:
            id (UUID), date, avg_humidity_before, avg_humidity_after,
            humidity_improved, total_heating_kwh, actual_kwh,
            estimation_accuracy_pct, correction_factor, total_cost_eur,
            heating_minutes, ventilation_suggestions, occupancy_detected,
            zones_above_65, zones_total, notes, created_at

        Local SQLite columns:
            id (INTEGER AUTO), date (UNIQUE), avg_humidity_before,
            avg_humidity_after, humidity_improved, total_heating_kwh,
            total_cost_eur, heating_minutes, occupancy_detected,
            zones_above_65, zones_total, actual_kwh,
            estimation_accuracy_pct, correction_factor, notes

        Note: Supabase has ventilation_suggestions; local does not.
        We append ventilation_suggestions to notes if present.
        """
        print("\n[daily_assessment] Fetching from Supabase...")
        rows = self._fetch_table("daily_assessment", order_col="date")
        print(f"[daily_assessment] Fetched {len(rows)} rows from Supabase")

        if not rows:
            self.stats["daily_assessment"] = {"fetched": 0, "inserted": 0, "skipped": 0}
            return 0

        # Dedup on date (UNIQUE constraint)
        existing_dates = set()
        try:
            cursor = self.conn.execute("SELECT date FROM daily_assessment")
            for r in cursor:
                existing_dates.add(r[0])
        except Exception:
            pass

        inserted = 0
        skipped = 0
        for row in rows:
            date_val = row.get("date", "")
            if date_val in existing_dates:
                skipped += 1
                continue

            # Combine notes and ventilation_suggestions
            notes = row.get("notes") or ""
            vent = row.get("ventilation_suggestions")
            if vent:
                if notes:
                    notes = f"{notes}; Ventilation: {vent}"
                else:
                    notes = f"Ventilation: {vent}"

            try:
                self.conn.execute(
                    "INSERT INTO daily_assessment "
                    "(date, avg_humidity_before, avg_humidity_after, "
                    " humidity_improved, total_heating_kwh, total_cost_eur, "
                    " heating_minutes, occupancy_detected, zones_above_65, "
                    " zones_total, actual_kwh, estimation_accuracy_pct, "
                    " correction_factor, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        date_val,
                        row.get("avg_humidity_before"),
                        row.get("avg_humidity_after"),
                        1 if row.get("humidity_improved") else 0,
                        row.get("total_heating_kwh"),
                        row.get("total_cost_eur"),
                        row.get("heating_minutes"),
                        1 if row.get("occupancy_detected") else 0,
                        row.get("zones_above_65", 0),
                        row.get("zones_total", 0),
                        row.get("actual_kwh"),
                        row.get("estimation_accuracy_pct"),
                        row.get("correction_factor"),
                        notes or None,
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
            except Exception as e:
                log.warning("daily_assessment insert error for date '%s': %s",
                            date_val, e)
                skipped += 1

        self.conn.commit()
        print(f"[daily_assessment] Inserted: {inserted}, Skipped (duplicates): {skipped}")
        self.stats["daily_assessment"] = {
            "fetched": len(rows), "inserted": inserted, "skipped": skipped,
        }
        return inserted

    # -- energy_baseline -------------------------------------------------------

    def migrate_energy_baseline(self) -> int:
        """
        Migrate Supabase energy_baseline -> local energy_baseline.

        Supabase columns:
            id (UUID), hour_of_day (unique), baseline_wh, sample_count,
            dhw_active_avg_wh, notes, last_updated

        Local SQLite columns:
            hour_of_day (PK), baseline_wh, sample_count, last_updated, notes

        Note: Supabase has dhw_active_avg_wh; local does not.
        We append dhw info to notes if present.
        """
        print("\n[energy_baseline] Fetching from Supabase...")
        rows = self._fetch_table("energy_baseline", order_col="hour_of_day")
        print(f"[energy_baseline] Fetched {len(rows)} rows from Supabase")

        if not rows:
            self.stats["energy_baseline"] = {"fetched": 0, "inserted": 0, "skipped": 0}
            return 0

        inserted = 0
        skipped = 0
        for row in rows:
            hour = row.get("hour_of_day")
            if hour is None:
                skipped += 1
                continue

            notes = row.get("notes") or ""
            dhw = row.get("dhw_active_avg_wh")
            if dhw is not None:
                dhw_note = f"DHW active avg: {dhw}Wh"
                notes = f"{notes}; {dhw_note}" if notes else dhw_note

            try:
                # Use INSERT OR REPLACE since hour_of_day is PK
                self.conn.execute(
                    "INSERT OR REPLACE INTO energy_baseline "
                    "(hour_of_day, baseline_wh, sample_count, last_updated, notes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        hour,
                        row.get("baseline_wh", 0),
                        row.get("sample_count", 0),
                        row.get("last_updated"),
                        notes or None,
                    ),
                )
                inserted += 1
            except Exception as e:
                log.warning("energy_baseline insert error for hour %s: %s", hour, e)
                skipped += 1

        self.conn.commit()
        print(f"[energy_baseline] Inserted/Updated: {inserted}, Skipped: {skipped}")
        self.stats["energy_baseline"] = {
            "fetched": len(rows), "inserted": inserted, "skipped": skipped,
        }
        return inserted

    # -- heating_experiments ---------------------------------------------------

    def migrate_heating_experiments(self) -> int:
        """
        Migrate Supabase heating_experiments -> local heating_experiments.

        Supabase columns:
            id (UUID), type, status, start_date, end_date, reason,
            avg_humidity_during, avg_humidity_before, avg_humidity_after,
            avg_outdoor_humidity, avg_outdoor_temp, avg_indoor_temp,
            thermal_runoff_hours, conclusion, recommendation,
            created_at, completed_at

        Local SQLite columns:
            id (INTEGER AUTO), start_date (UNIQUE), end_date, status,
            zones_blocked, created_at, completed_at, result_summary,
            avg_dp_spread, min_dp_spread, recommendation

        Significant schema differences. We map what we can and build
        result_summary from Supabase conclusion/reason fields.
        """
        print("\n[heating_experiments] Fetching from Supabase...")
        rows = self._fetch_table("heating_experiments", order_col="created_at")
        print(f"[heating_experiments] Fetched {len(rows)} rows from Supabase")

        if not rows:
            self.stats["heating_experiments"] = {"fetched": 0, "inserted": 0, "skipped": 0}
            return 0

        # Dedup on start_date (UNIQUE constraint)
        existing = set()
        try:
            cursor = self.conn.execute(
                "SELECT start_date FROM heating_experiments"
            )
            for r in cursor:
                existing.add(r[0])
        except Exception:
            pass

        inserted = 0
        skipped = 0
        for row in rows:
            start_date = row.get("start_date", "")
            if start_date in existing:
                skipped += 1
                continue

            # Build result_summary from available Supabase fields
            summary_parts = []
            if row.get("conclusion"):
                summary_parts.append(row["conclusion"])
            if row.get("reason"):
                summary_parts.append(f"Reason: {row['reason']}")
            if row.get("avg_humidity_before") is not None:
                summary_parts.append(
                    f"Humidity: {row.get('avg_humidity_before')}% -> "
                    f"{row.get('avg_humidity_after', '?')}%"
                )
            if row.get("thermal_runoff_hours") is not None:
                summary_parts.append(
                    f"Thermal runoff: {row['thermal_runoff_hours']}h"
                )
            result_summary = "; ".join(summary_parts) if summary_parts else None

            # Map experiment type to zones_blocked
            zones_blocked = row.get("type") or "all"

            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO heating_experiments "
                    "(start_date, end_date, status, zones_blocked, "
                    " created_at, completed_at, result_summary, "
                    " avg_dp_spread, min_dp_spread, recommendation) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        start_date,
                        row.get("end_date", ""),
                        row.get("status", "completed"),
                        zones_blocked,
                        row.get("created_at", ""),
                        row.get("completed_at"),
                        result_summary,
                        None,  # avg_dp_spread not in Supabase schema
                        None,  # min_dp_spread not in Supabase schema
                        row.get("recommendation"),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
            except Exception as e:
                log.warning("heating_experiments insert error: %s", e)
                skipped += 1

        self.conn.commit()
        print(f"[heating_experiments] Inserted: {inserted}, Skipped (duplicates): {skipped}")
        self.stats["heating_experiments"] = {
            "fetched": len(rows), "inserted": inserted, "skipped": skipped,
        }
        return inserted

    # -- Full migration --------------------------------------------------------

    def run_full_migration(self) -> dict:
        """
        Run the complete migration for all tables.

        Returns a summary dict with per-table statistics.
        """
        start_time = time.time()

        print("=" * 60)
        print("  Airzone: Supabase -> Local SQLite Migration")
        print("=" * 60)
        print(f"  Supabase URL : {self.url}")
        print(f"  Local DB     : {self.db_path}")
        print(f"  Started at   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        # Verify Supabase connectivity
        print("\nVerifying Supabase connection...")
        try:
            resp = requests.get(
                f"{self.url}/rest/v1/system_state?select=key&limit=1",
                headers=self._headers,
                timeout=15,
            )
            if resp.status_code not in (200, 206):
                print(f"ERROR: Supabase returned status {resp.status_code}")
                print(f"  Response: {resp.text[:300]}")
                return {"error": f"Supabase connection failed: {resp.status_code}"}
            print("  Connection OK")
        except Exception as e:
            print(f"ERROR: Cannot reach Supabase: {e}")
            return {"error": str(e)}

        # Migrate tables in order of dependency
        # (system_state first since others might reference learned params,
        #  then control_log as the main data, then supporting tables)
        self.migrate_system_state()
        self.migrate_control_log()
        self.migrate_netatmo_readings()
        self.migrate_netatmo_sync_status()
        self.migrate_dp_spread_predictions()
        self.migrate_daily_assessment()
        self.migrate_energy_baseline()
        self.migrate_heating_experiments()

        elapsed = time.time() - start_time

        # Print summary
        print("\n" + "=" * 60)
        print("  Migration Summary")
        print("=" * 60)
        total_fetched = 0
        total_inserted = 0
        total_skipped = 0
        for table, s in self.stats.items():
            fetched = s.get("fetched", 0)
            ins = s.get("inserted", 0)
            skip = s.get("skipped", 0)
            total_fetched += fetched
            total_inserted += ins
            total_skipped += skip
            status = "OK" if fetched > 0 or ins >= 0 else "EMPTY"
            print(f"  {table:30s}  fetched={fetched:>6d}  "
                  f"inserted={ins:>6d}  skipped={skip:>6d}  [{status}]")

        print("-" * 60)
        print(f"  {'TOTAL':30s}  fetched={total_fetched:>6d}  "
              f"inserted={total_inserted:>6d}  skipped={total_skipped:>6d}")
        print(f"\n  Completed in {elapsed:.1f}s")
        print(f"  DB size: {self.db_path.stat().st_size / (1024*1024):.2f} MB")
        print("=" * 60)

        return {
            "tables": self.stats,
            "total_fetched": total_fetched,
            "total_inserted": total_inserted,
            "total_skipped": total_skipped,
            "elapsed_seconds": round(elapsed, 1),
        }

    def close(self):
        """Close the SQLite connection."""
        self.conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Migrate Airzone data from Supabase to local SQLite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Use keychain for service key (default):
  python3 src/airzone_migrate_supabase.py

  # Provide credentials explicitly:
  python3 src/airzone_migrate_supabase.py --service-key 'eyJ...'

  # Custom DB path:
  python3 src/airzone_migrate_supabase.py --db-path ./my_history.db
""",
    )
    parser.add_argument(
        "--supabase-url", type=str, default=DEFAULT_SUPABASE_URL,
        help=f"Supabase project URL (default: {DEFAULT_SUPABASE_URL})",
    )
    parser.add_argument(
        "--service-key", type=str, default="",
        help="Supabase service key (if not provided, reads from keychain)",
    )
    parser.add_argument(
        "--db-path", type=str, default=str(DEFAULT_DB_PATH),
        help=f"Path to local SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--table", type=str, default="",
        help="Migrate a single table only (e.g., 'control_log', 'netatmo_readings')",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose/debug logging",
    )
    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Resolve service key
    service_key = args.service_key
    if not service_key:
        print("No --service-key provided, trying macOS keychain...")
        service_key = _get_keychain_value("supabase_service_key")
        if service_key:
            print("  Found service key in keychain")
        else:
            print("ERROR: No service key found. Provide --service-key or store "
                  "in keychain as 'supabase_service_key'")
            sys.exit(1)

    # Create migrator
    migrator = SupabaseMigrator(
        supabase_url=args.supabase_url,
        service_key=service_key,
        db_path=args.db_path,
    )

    try:
        if args.table:
            # Migrate a single table
            method_name = f"migrate_{args.table}"
            if hasattr(migrator, method_name):
                getattr(migrator, method_name)()
            else:
                valid = [m.replace("migrate_", "")
                         for m in dir(migrator) if m.startswith("migrate_")]
                print(f"ERROR: Unknown table '{args.table}'. "
                      f"Valid tables: {', '.join(valid)}")
                sys.exit(1)
        else:
            # Full migration
            migrator.run_full_migration()
    finally:
        migrator.close()


if __name__ == "__main__":
    main()

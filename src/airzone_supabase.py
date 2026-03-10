"""
Airzone Supabase Sync (Optional Transition Module)
====================================================
Background sync: local SQLite → Supabase to keep the Loveable React
dashboard alive during transition.

Toggle on/off in config: "supabase_sync": true/false
Credentials in keychain: supabase_url, supabase_service_key

This module has ZERO impact on core logic — the app works identically
with sync off.  Can be removed entirely once the React dashboard is retired.

Usage:
    from airzone_supabase import SupabaseSync
    sync = SupabaseSync(conn)
    sync.sync_control_log()     # push recent control_log entries
    sync.sync_system_state()    # push learned models/params
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

log = logging.getLogger("airzone")


class SupabaseSync:
    """Non-blocking sync from local SQLite to Supabase."""

    def __init__(self, conn: sqlite3.Connection,
                 supabase_url: str = "", service_key: str = ""):
        self.conn = conn
        self.url = supabase_url.rstrip("/")
        self.key = service_key
        self._enabled = bool(self.url and self.key)

        if not self._enabled:
            # Try loading from keychain
            try:
                from airzone_secrets import secrets as sec
                self.url = sec.get("supabase_url") or ""
                self.key = sec.get("supabase_service_key") or ""
                self._enabled = bool(self.url and self.key)
            except Exception:
                pass

    @property
    def enabled(self) -> bool:
        return self._enabled and _HAS_REQUESTS

    def _api_post(self, table: str, rows: list[dict],
                  upsert: bool = False) -> bool:
        """POST rows to Supabase REST API."""
        if not self.enabled:
            return False

        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if upsert:
            headers["Prefer"] = "resolution=merge-duplicates"

        try:
            resp = requests.post(
                f"{self.url}/rest/v1/{table}",
                headers=headers,
                json=rows,
                timeout=30,
            )
            if resp.status_code in (200, 201):
                return True
            log.warning("Supabase sync %s: %d %s",
                        table, resp.status_code, resp.text[:200])
            return False
        except Exception as e:
            log.warning("Supabase sync error: %s", e)
            return False

    def sync_control_log(self, hours: int = 1) -> int:
        """Sync recent control_log entries to Supabase."""
        if not self.enabled:
            return 0

        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"
        try:
            rows = self.conn.execute(
                "SELECT zone_name, created_at, action, temperature, "
                "       humidity_airzone, humidity_netatmo, outdoor_temp, "
                "       outdoor_humidity, dp_spread, reason "
                "FROM control_log WHERE created_at >= ? ORDER BY created_at",
                (cutoff,)
            ).fetchall()
        except Exception:
            return 0

        if not rows:
            return 0

        batch = []
        for r in rows:
            batch.append({
                "zone_name": r[0], "created_at": r[1], "action": r[2],
                "temperature": r[3], "humidity_airzone": r[4],
                "humidity_netatmo": r[5], "outdoor_temp": r[6],
                "outdoor_humidity": r[7], "dp_spread": r[8],
                "reason": r[9],
            })

        # Send in batches of 100
        sent = 0
        for i in range(0, len(batch), 100):
            chunk = batch[i:i + 100]
            if self._api_post("control_log", chunk, upsert=True):
                sent += len(chunk)

        log.info("Supabase: synced %d/%d control_log entries", sent, len(batch))
        return sent

    def sync_system_state(self) -> int:
        """Sync system_state key-value pairs to Supabase."""
        if not self.enabled:
            return 0

        try:
            rows = self.conn.execute(
                "SELECT key, value, updated_at FROM system_state"
            ).fetchall()
        except Exception:
            return 0

        if not rows:
            return 0

        batch = []
        for r in rows:
            val = r[1]
            # Parse JSON value if stored as string
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
            batch.append({
                "key": r[0],
                "value": val,
                "updated_at": r[2] or datetime.utcnow().isoformat() + "Z",
            })

        if self._api_post("system_state", batch, upsert=True):
            log.info("Supabase: synced %d system_state entries", len(batch))
            return len(batch)
        return 0

    def sync_daily_assessment(self, days: int = 7) -> int:
        """Sync recent daily_assessment entries."""
        if not self.enabled:
            return 0

        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
        try:
            rows = self.conn.execute(
                "SELECT date, total_zones_heated, total_heating_events, "
                "       estimated_kwh, estimated_cost, linky_actual_kwh, "
                "       correction_factor, avg_dp_spread, min_dp_spread "
                "FROM daily_assessment WHERE date >= ? ORDER BY date",
                (cutoff,)
            ).fetchall()
        except Exception:
            return 0

        if not rows:
            return 0

        batch = [dict(zip(
            ["date", "total_zones_heated", "total_heating_events",
             "estimated_kwh", "estimated_cost", "linky_actual_kwh",
             "correction_factor", "avg_dp_spread", "min_dp_spread"], r))
            for r in rows]

        if self._api_post("daily_assessment", batch, upsert=True):
            log.info("Supabase: synced %d daily_assessment entries", len(batch))
            return len(batch)
        return 0

    def sync_predictions(self, hours: int = 6) -> int:
        """Sync recent DP spread predictions."""
        if not self.enabled:
            return 0

        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"
        try:
            rows = self.conn.execute(
                "SELECT zone_name, predicted_for, hours_ahead, "
                "       predicted_dp_spread, current_dp_spread, "
                "       current_indoor_temp, current_outdoor_temp, decision_made "
                "FROM dp_spread_predictions WHERE created_at >= ?",
                (cutoff,)
            ).fetchall()
        except Exception:
            return 0

        if not rows:
            return 0

        batch = [dict(zip(
            ["zone_name", "predicted_for", "hours_ahead",
             "predicted_dp_spread", "current_dp_spread",
             "current_indoor_temp", "current_outdoor_temp",
             "decision_made"], r))
            for r in rows]

        if self._api_post("dp_spread_predictions", batch, upsert=True):
            log.info("Supabase: synced %d predictions", len(batch))
            return len(batch)
        return 0

    def run_full_sync(self) -> dict:
        """Run all sync operations. Returns summary."""
        if not self.enabled:
            return {"enabled": False, "message": "Supabase sync not configured"}

        return {
            "enabled": True,
            "control_log": self.sync_control_log(hours=1),
            "system_state": self.sync_system_state(),
            "daily_assessment": self.sync_daily_assessment(days=7),
            "predictions": self.sync_predictions(hours=6),
            "synced_at": datetime.utcnow().isoformat() + "Z",
        }

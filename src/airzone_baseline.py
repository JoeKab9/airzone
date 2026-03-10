"""
Airzone Energy Baseline Learning
==================================
Learns per-hour-of-day standby energy consumption from Linky load curve data.

Ported from Loveable TypeScript (supabase/functions/learn-baseline/index.ts).

Logic:
1. Fetch last 7 days of Linky load curve data
2. Cross-reference with control_log to identify hours when heating was active
3. For hours with NO heating, compute average consumption = baseline
4. Update energy_baseline table with exponential moving average (α=0.3)

The baseline represents the house's standby consumption (fridge, router, etc.)
per hour-of-day, excluding heat pump usage.

All data stored in local SQLite — no cloud dependency.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, date

log = logging.getLogger("airzone")

ALPHA = 0.3  # EMA weight for new data


# ── DB Schema ────────────────────────────────────────────────────────────────

def create_baseline_tables(conn: sqlite3.Connection):
    """Create energy baseline tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS energy_baseline (
            hour_of_day     INTEGER PRIMARY KEY,
            baseline_wh     REAL    NOT NULL DEFAULT 0,
            sample_count    INTEGER NOT NULL DEFAULT 0,
            last_updated    TEXT,
            notes           TEXT
        );

        CREATE TABLE IF NOT EXISTS heating_experiments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            start_date      TEXT    NOT NULL,
            end_date        TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'scheduled',
            zones_blocked   TEXT,
            created_at      TEXT    NOT NULL,
            completed_at    TEXT,
            result_summary  TEXT,
            avg_dp_spread   REAL,
            min_dp_spread   REAL,
            recommendation  TEXT,
            UNIQUE(start_date)
        );
    """)
    conn.commit()


# ── Baseline Learning ────────────────────────────────────────────────────────

def learn_baseline(conn: sqlite3.Connection, days: int = 7) -> dict:
    """
    Learn standby energy baseline from Linky data during non-heating hours.

    Returns summary of updates made.
    """
    create_baseline_tables(conn)

    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    # Get Linky readings
    linky_rows = conn.execute(
        "SELECT timestamp, wh FROM linky_readings "
        "WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp",
        (str(start_date), str(end_date + timedelta(days=1)))
    ).fetchall()

    if not linky_rows:
        return {"success": False, "message": "No Linky data available"}

    # Parse into hourly buckets
    hourly_consumption: dict[str, dict] = {}  # "YYYY-MM-DD|HH" -> {total_wh, slots}
    for ts_raw, wh in linky_rows:
        ts = str(ts_raw)
        try:
            if "T" in ts:
                parts = ts.split("T")
                date_key = parts[0]
                hour = int(parts[1][:2])
            elif " " in ts:
                parts = ts.split(" ")
                date_key = parts[0]
                hour = int(parts[1][:2])
            else:
                continue

            key = f"{date_key}|{hour}"
            entry = hourly_consumption.get(key, {"total_wh": 0, "slots": 0})
            entry["total_wh"] += wh
            entry["slots"] += 1
            hourly_consumption[key] = entry
        except (ValueError, IndexError):
            continue

    # Get control_log to identify heating hours
    # Try control_log first (brain), fall back to control_actions
    heating_hours: set[str] = set()

    try:
        logs = conn.execute(
            "SELECT created_at, action, reason FROM control_log "
            "WHERE created_at >= ? ORDER BY created_at",
            (str(start_date) + "T00:00:00Z",)
        ).fetchall()
        for row in logs:
            action = row[1]
            reason = row[2] or ""
            is_heating = (action == "heating_on" or
                          (action == "no_change" and "heating" in reason.lower()))
            if is_heating:
                ts = str(row[0])
                try:
                    if "T" in ts:
                        dt_key = ts.split("T")[0]
                        hour = int(ts.split("T")[1][:2])
                    else:
                        continue
                    heating_hours.add(f"{dt_key}|{hour}")
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass

    # Also check control_actions table
    try:
        actions = conn.execute(
            "SELECT timestamp, action FROM control_actions "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (str(start_date) + "T00:00:00Z",)
        ).fetchall()
        for row in actions:
            if "on" in str(row[1]).lower():
                ts = str(row[0])
                try:
                    if "T" in ts:
                        dt_key = ts.split("T")[0]
                        hour = int(ts.split("T")[1][:2])
                        heating_hours.add(f"{dt_key}|{hour}")
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass

    # Group non-heating consumption by hour-of-day
    hour_baselines: dict[int, dict] = {}  # hour -> {total_wh, count}
    for key, entry in hourly_consumption.items():
        if key in heating_hours:
            continue
        if entry["slots"] < 2:
            continue

        hour = int(key.split("|")[1])
        existing = hour_baselines.get(hour, {"total_wh": 0, "count": 0})
        existing["total_wh"] += entry["total_wh"]
        existing["count"] += 1
        hour_baselines[hour] = existing

    # Get current baselines
    current_rows = conn.execute("SELECT * FROM energy_baseline").fetchall()
    current_map: dict[int, dict] = {}
    for r in current_rows:
        current_map[r[0]] = {"baseline_wh": r[1], "sample_count": r[2]}

    # Update with EMA
    updates = []
    now = datetime.utcnow().isoformat() + "Z"

    for hour, data in hour_baselines.items():
        avg_wh = round(data["total_wh"] / data["count"], 1)
        current = current_map.get(hour)

        if not current or current["sample_count"] == 0:
            new_baseline = avg_wh
        else:
            # EMA: new = α * observed + (1-α) * old
            new_baseline = round(
                ALPHA * avg_wh + (1 - ALPHA) * current["baseline_wh"], 1)

        new_sample_count = (current["sample_count"] if current else 0) + data["count"]

        conn.execute(
            "INSERT OR REPLACE INTO energy_baseline "
            "(hour_of_day, baseline_wh, sample_count, last_updated, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (hour, new_baseline, new_sample_count, now,
             f"EMA updated from {data['count']} samples, avg={avg_wh}Wh"),
        )
        updates.append({"hour": hour, "new_baseline": new_baseline,
                         "samples": data["count"]})

    conn.commit()

    return {
        "success": True,
        "linky_readings": len(linky_rows),
        "heating_hours_excluded": len(heating_hours),
        "hours_updated": len(updates),
        "updates": updates,
    }


def get_baseline(conn: sqlite3.Connection) -> list[dict]:
    """Return learned baselines for all hours."""
    create_baseline_tables(conn)
    rows = conn.execute(
        "SELECT hour_of_day, baseline_wh, sample_count, last_updated "
        "FROM energy_baseline ORDER BY hour_of_day"
    ).fetchall()
    return [{"hour": r[0], "baseline_wh": r[1],
             "sample_count": r[2], "last_updated": r[3]} for r in rows]


# ── Heating Experiments ──────────────────────────────────────────────────────

def check_experiment_eligibility(conn: sqlite3.Connection,
                                 min_data_days: int = 14,
                                 cooldown_days: int = 7) -> dict:
    """
    Check if we should auto-schedule a no-heating experiment.

    Conditions:
    - At least min_data_days of control_log data
    - No experiment in the last cooldown_days
    - No active experiment
    """
    create_baseline_tables(conn)

    # Check data age
    try:
        first_log = conn.execute(
            "SELECT MIN(created_at) FROM control_log"
        ).fetchone()
        if not first_log or not first_log[0]:
            return {"eligible": False, "reason": "No control log data"}

        first_date = datetime.fromisoformat(
            first_log[0].replace("Z", "+00:00"))
        data_days = (datetime.utcnow() - first_date.replace(tzinfo=None)).days
        if data_days < min_data_days:
            return {"eligible": False,
                    "reason": f"Need {min_data_days} days of data, have {data_days}"}
    except Exception:
        return {"eligible": False, "reason": "Cannot read control log"}

    # Check active/recent experiments
    recent_cutoff = (date.today() - timedelta(days=cooldown_days)).isoformat()
    active = conn.execute(
        "SELECT COUNT(*) FROM heating_experiments "
        "WHERE status IN ('scheduled', 'active')"
    ).fetchone()[0]
    if active > 0:
        return {"eligible": False, "reason": "Experiment already active/scheduled"}

    recent = conn.execute(
        "SELECT COUNT(*) FROM heating_experiments WHERE end_date >= ?",
        (recent_cutoff,)
    ).fetchone()[0]
    if recent > 0:
        return {"eligible": False,
                "reason": f"Cooldown period ({cooldown_days} days)"}

    return {"eligible": True, "data_days": data_days}


def schedule_experiment(conn: sqlite3.Connection,
                        duration_days: int = 4,
                        zones: list[str] | None = None) -> dict:
    """Schedule a no-heating experiment starting tomorrow."""
    create_baseline_tables(conn)

    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=duration_days)
    now = datetime.utcnow().isoformat() + "Z"

    zones_str = ",".join(zones) if zones else "all"

    conn.execute(
        "INSERT OR IGNORE INTO heating_experiments "
        "(start_date, end_date, status, zones_blocked, created_at) "
        "VALUES (?, ?, 'scheduled', ?, ?)",
        (str(start), str(end), zones_str, now))
    conn.commit()

    return {
        "start_date": str(start),
        "end_date": str(end),
        "duration_days": duration_days,
        "zones_blocked": zones_str,
    }


def get_active_experiment(conn: sqlite3.Connection) -> dict | None:
    """Return the currently active experiment, if any."""
    create_baseline_tables(conn)
    today = str(date.today())

    row = conn.execute(
        "SELECT id, start_date, end_date, status, zones_blocked "
        "FROM heating_experiments "
        "WHERE start_date <= ? AND end_date >= ? "
        "  AND status IN ('scheduled', 'active') "
        "ORDER BY start_date DESC LIMIT 1",
        (today, today)
    ).fetchone()

    if not row:
        return None

    # Activate if scheduled
    if row[3] == "scheduled":
        conn.execute(
            "UPDATE heating_experiments SET status = 'active' WHERE id = ?",
            (row[0],))
        conn.commit()

    return {
        "id": row[0],
        "start_date": row[1],
        "end_date": row[2],
        "status": "active",
        "zones_blocked": row[4],
    }


def complete_experiment(conn: sqlite3.Connection, experiment_id: int) -> dict:
    """
    Complete an experiment and compute results.

    Analyzes DP spread during the experiment period to determine if
    heating was actually needed.
    """
    create_baseline_tables(conn)

    exp = conn.execute(
        "SELECT start_date, end_date FROM heating_experiments WHERE id = ?",
        (experiment_id,)
    ).fetchone()

    if not exp:
        return {"error": "Experiment not found"}

    # Get DP spread readings during experiment
    spreads = conn.execute(
        "SELECT dp_spread FROM control_log "
        "WHERE created_at >= ? AND created_at <= ? "
        "  AND dp_spread IS NOT NULL",
        (exp[0] + "T00:00:00Z", exp[1] + "T23:59:59Z")
    ).fetchall()

    if not spreads:
        summary = "No DP spread data collected during experiment"
        avg_spread = None
        min_spread = None
        recommendation = "Inconclusive — retry with more monitoring"
    else:
        spread_values = [r[0] for r in spreads]
        avg_spread = round(sum(spread_values) / len(spread_values), 1)
        min_spread = round(min(spread_values), 1)

        if min_spread >= 4:
            recommendation = ("Heating was NOT needed. Min DP spread stayed "
                              f"above 4°C ({min_spread}°C). Consider reducing "
                              "heating frequency.")
            summary = f"Safe — min spread {min_spread}°C, avg {avg_spread}°C"
        elif min_spread >= 2:
            recommendation = ("Heating is beneficial but not critical. "
                              f"Min DP spread dropped to {min_spread}°C. "
                              "Current thresholds are appropriate.")
            summary = f"Marginal — min spread {min_spread}°C, avg {avg_spread}°C"
        else:
            recommendation = ("Heating is essential! Min DP spread dropped to "
                              f"{min_spread}°C — condensation risk. "
                              "Do not reduce heating.")
            summary = f"Critical — min spread {min_spread}°C, avg {avg_spread}°C"

    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        "UPDATE heating_experiments SET "
        "status = 'completed', completed_at = ?, result_summary = ?, "
        "avg_dp_spread = ?, min_dp_spread = ?, recommendation = ? "
        "WHERE id = ?",
        (now, summary, avg_spread, min_spread, recommendation, experiment_id))
    conn.commit()

    return {
        "status": "completed",
        "summary": summary,
        "avg_dp_spread": avg_spread,
        "min_dp_spread": min_spread,
        "recommendation": recommendation,
    }


def get_experiments(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Return recent experiments."""
    create_baseline_tables(conn)
    rows = conn.execute(
        "SELECT id, start_date, end_date, status, zones_blocked, "
        "       created_at, completed_at, result_summary, "
        "       avg_dp_spread, min_dp_spread, recommendation "
        "FROM heating_experiments ORDER BY start_date DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [dict(zip(
        ["id", "start_date", "end_date", "status", "zones_blocked",
         "created_at", "completed_at", "result_summary",
         "avg_dp_spread", "min_dp_spread", "recommendation"], r))
        for r in rows]

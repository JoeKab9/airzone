"""
Airzone Self-Learning Analytics
================================
Analyzes historical zone readings to learn:
- Drying rates per zone (how fast humidity drops when heating)
- Recovery times (hours to go from ON→OFF threshold)
- Rebound rates (how fast humidity climbs after heating stops)
- Optimal warm_hours_count recommendation

No UI dependencies — usable by both macOS app and Pi daemon.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("airzone")

# ── DB Schema ────────────────────────────────────────────────────────────────

def create_analytics_tables(conn: sqlite3.Connection):
    """Create analytics tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS heating_cycles (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_name        TEXT    NOT NULL,
            device_id        TEXT    NOT NULL DEFAULT '',
            start_ts         TEXT    NOT NULL,
            end_ts           TEXT    NOT NULL,
            duration_hours   REAL    NOT NULL,
            humidity_start   INTEGER,
            humidity_end     INTEGER,
            humidity_drop    INTEGER,
            drying_rate      REAL,
            avg_outdoor_temp REAL,
            avg_indoor_temp  REAL,
            reached_threshold INTEGER DEFAULT 0,
            rebound_rate     REAL,
            UNIQUE(zone_name, start_ts)
        );

        CREATE INDEX IF NOT EXISTS idx_cycles_zone_ts
            ON heating_cycles(zone_name, start_ts);

        CREATE TABLE IF NOT EXISTS zone_analytics (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            date             TEXT    NOT NULL,
            zone_name        TEXT    NOT NULL,
            cycles_count     INTEGER DEFAULT 0,
            total_heating_hours REAL DEFAULT 0,
            avg_drying_rate  REAL,
            median_recovery_time REAL,
            avg_rebound_rate REAL,
            peak_humidity    INTEGER,
            trough_humidity  INTEGER,
            avg_outdoor_temp REAL,
            UNIQUE(date, zone_name)
        );

        CREATE TABLE IF NOT EXISTS optimization_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT    NOT NULL,
            metric           TEXT    NOT NULL,
            current_value    TEXT,
            recommended_value TEXT,
            confidence       REAL,
            reasoning        TEXT,
            applied          INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS control_decisions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT    NOT NULL,
            zone_name        TEXT    NOT NULL,
            humidity         INTEGER,
            room_temp        REAL,
            outdoor_temp     REAL,
            outdoor_dew_point REAL,
            on_thresh        INTEGER,
            effective_on_thresh INTEGER,
            off_thresh       INTEGER,
            effective_off    INTEGER,
            is_warm_now      INTEGER DEFAULT 0,
            action           TEXT NOT NULL,
            reason           TEXT,
            dew_point_decision TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_decisions_zone_ts
            ON control_decisions(zone_name, timestamp);
    """)


def log_control_decision(conn: sqlite3.Connection, zone_name: str,
                         humidity: int, room_temp: float | None,
                         outdoor_temp: float | None,
                         outdoor_dew_point: float | None,
                         on_thresh: int, effective_on_thresh: int,
                         off_thresh: int, effective_off: int,
                         is_warm_now: bool, action: str,
                         reason: str = None,
                         dew_point_decision: str = None):
    """Log every control decision for later analysis."""
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO control_decisions "
            "(timestamp, zone_name, humidity, room_temp, outdoor_temp, "
            " outdoor_dew_point, on_thresh, effective_on_thresh, "
            " off_thresh, effective_off, is_warm_now, action, reason, "
            " dew_point_decision) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"),
             zone_name, humidity, room_temp, outdoor_temp,
             outdoor_dew_point, on_thresh, effective_on_thresh,
             off_thresh, effective_off,
             1 if is_warm_now else 0, action, reason,
             dew_point_decision))
        conn.commit()
    except Exception:
        pass  # never let decision logging break control flow


def migrate_analytics_tables(conn: sqlite3.Connection):
    """Add columns introduced after initial schema."""
    cursor = conn.execute("PRAGMA table_info(heating_cycles)")
    existing = {row[1] for row in cursor.fetchall()}
    new_cols = {
        "runoff_drop": "REAL",
        "runoff_duration_hours": "REAL",
        "runoff_trough_humidity": "INTEGER",
        "avg_outdoor_dew_point": "REAL",
    }
    for col, col_type in new_cols.items():
        if col not in existing:
            conn.execute(
                f"ALTER TABLE heating_cycles ADD COLUMN {col} {col_type}")
    conn.commit()


# ── Heating Cycle Detection ──────────────────────────────────────────────────

def detect_heating_cycles(readings: list, off_threshold: int = 65,
                          max_gap_minutes: int = 15) -> list:
    """
    Detect heating cycles from time-ordered readings for ONE zone.

    A cycle starts when power transitions 0→1 and ends when power goes 1→0.
    Skips cycles shorter than 6 minutes or spanning data gaps > max_gap_minutes.

    Returns list of cycle dicts with metrics.
    """
    cycles = []
    in_cycle = False
    cycle_readings = []
    prev_ts = None

    for r in readings:
        power = r.get("power", 0)
        try:
            ts = datetime.fromisoformat(
                r["timestamp"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue

        # Check for data gap
        if prev_ts and in_cycle:
            gap = (ts - prev_ts).total_seconds() / 60
            if gap > max_gap_minutes:
                # Gap too large, discard incomplete cycle
                in_cycle = False
                cycle_readings = []

        if not in_cycle and power == 1:
            in_cycle = True
            cycle_readings = [r]
        elif in_cycle and power == 1:
            cycle_readings.append(r)
        elif in_cycle and power == 0:
            in_cycle = False
            if cycle_readings:
                cycle = _build_cycle(cycle_readings, off_threshold)
                if cycle and cycle["duration_hours"] >= 0.1:
                    cycles.append(cycle)
            cycle_readings = []

        prev_ts = ts

    return cycles


def _build_cycle(cycle_readings: list, off_threshold: int) -> dict:
    """Build a cycle dict from a list of readings where power=1."""
    if not cycle_readings:
        return None

    first = cycle_readings[0]
    last = cycle_readings[-1]

    try:
        ts_start = datetime.fromisoformat(
            first["timestamp"].replace("Z", "+00:00"))
        ts_end = datetime.fromisoformat(
            last["timestamp"].replace("Z", "+00:00"))
    except (ValueError, KeyError):
        return None

    duration = (ts_end - ts_start).total_seconds() / 3600
    if duration <= 0:
        return None

    hum_start = first.get("humidity")
    hum_end = last.get("humidity")

    outdoor = [r["outdoor_temp"] for r in cycle_readings
               if r.get("outdoor_temp") is not None]
    indoor = [r["temperature"] for r in cycle_readings
              if r.get("temperature") is not None]
    dew_pts = [r["outdoor_dew_point"] for r in cycle_readings
               if r.get("outdoor_dew_point") is not None]

    drop = (hum_start - hum_end) if (hum_start is not None and hum_end is not None) else None
    rate = drop / duration if (drop is not None and duration > 0) else None

    return {
        "zone_name": first.get("zone_name", ""),
        "device_id": first.get("device_id", ""),
        "start_ts": first["timestamp"],
        "end_ts": last["timestamp"],
        "duration_hours": round(duration, 2),
        "humidity_start": hum_start,
        "humidity_end": hum_end,
        "humidity_drop": drop,
        "drying_rate": round(rate, 2) if rate is not None else None,
        "avg_outdoor_temp": (round(sum(outdoor) / len(outdoor), 1)
                             if outdoor else None),
        "avg_indoor_temp": (round(sum(indoor) / len(indoor), 1)
                            if indoor else None),
        "avg_outdoor_dew_point": (round(sum(dew_pts) / len(dew_pts), 1)
                                  if dew_pts else None),
        "reached_threshold": (hum_end is not None
                              and hum_end <= off_threshold),
    }


# ── Rebound Rate ─────────────────────────────────────────────────────────────

def compute_rebound_rate(readings: list, cycle_end_ts: str,
                         window_hours: float = 6.0) -> float:
    """
    Compute humidity rebound rate after a heating cycle ends.
    Looks at readings in the window_hours after cycle_end_ts where power=0.
    Returns %/hour rise, or None if insufficient data.
    """
    try:
        end_dt = datetime.fromisoformat(cycle_end_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

    cutoff = end_dt + timedelta(hours=window_hours)
    post_readings = []

    for r in readings:
        try:
            ts = datetime.fromisoformat(
                r["timestamp"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        if ts <= end_dt:
            continue
        if ts > cutoff:
            break
        if r.get("power", 0) == 1:
            break  # zone turned on again, stop
        if r.get("humidity") is not None:
            post_readings.append((ts, r["humidity"]))

    if len(post_readings) < 2:
        return None

    first_ts, first_hum = post_readings[0]
    last_ts, last_hum = post_readings[-1]
    hours = (last_ts - first_ts).total_seconds() / 3600
    if hours < 0.5:
        return None

    return round((last_hum - first_hum) / hours, 2)


# ── Runoff (Coast-down) ──────────────────────────────────────────────────────

def compute_runoff(readings: list, cycle_end_ts: str,
                   cycle_end_humidity: int,
                   window_hours: float = 3.0) -> dict | None:
    """
    Compute continued humidity drop after heating stops (thermal inertia).

    After heating turns off, residual heat keeps lowering humidity for
    30-60+ minutes before rebound begins.

    Returns dict with runoff_drop, runoff_duration_hours,
    runoff_trough_humidity — or None if insufficient data.
    """
    try:
        end_dt = datetime.fromisoformat(cycle_end_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

    if cycle_end_humidity is None:
        return None

    cutoff = end_dt + timedelta(hours=window_hours)
    post_readings = []

    for r in readings:
        try:
            ts = datetime.fromisoformat(
                r["timestamp"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        if ts <= end_dt:
            continue
        if ts > cutoff:
            break
        if r.get("power", 0) == 1:
            break  # zone turned back on
        if r.get("humidity") is not None:
            post_readings.append((ts, r["humidity"]))

    if len(post_readings) < 2:
        return None

    # Find the humidity minimum (trough)
    min_hum = cycle_end_humidity
    min_ts = end_dt
    for ts, hum in post_readings:
        if hum < min_hum:
            min_hum = hum
            min_ts = ts

    runoff_drop = cycle_end_humidity - min_hum
    if runoff_drop <= 0:
        return {
            "runoff_drop": 0.0,
            "runoff_duration_hours": 0.0,
            "runoff_trough_humidity": cycle_end_humidity,
        }

    duration = (min_ts - end_dt).total_seconds() / 3600
    return {
        "runoff_drop": round(runoff_drop, 1),
        "runoff_duration_hours": round(duration, 2),
        "runoff_trough_humidity": min_hum,
    }


# ── Dew Point Bands ──────────────────────────────────────────────────────────

DEW_POINT_BANDS = [
    ("<0°C", -999, 0),
    ("0-5°C", 0, 5),
    ("5-10°C", 5, 10),
    ("10-15°C", 10, 15),
    (">15°C", 15, 999),
]


def _dew_point_band(dp: float) -> str:
    """Return the dew point band label for a given dew point."""
    for label, lo, hi in DEW_POINT_BANDS:
        if lo <= dp < hi:
            return label
    return ">15°C"


# ── Smart Early-Off (Dew-Point-Aware) ────────────────────────────────────────

def get_smart_early_off_adjustment(
        conn: sqlite3.Connection, zone_name: str,
        current_dew_point: float | None = None,
        max_adjustment: float = 5.0,
        min_cycles: int = 5,
        days: int = 30) -> dict:
    """
    Compute learned early-off adjustment for a zone, bucketed by dew point.

    Algorithm:
    1. Query heating cycles with runoff data for this zone (last N days)
    2. Bucket by dew point band (<0, 0-5, 5-10, 10-15, >15°C)
    3. If enough data in current band (>=3): use that band
    4. Else: fall back to all-band average
    5. Conservative: mean - 0.5*std, capped at max_adjustment
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"

    rows = conn.execute(
        "SELECT runoff_drop, avg_outdoor_dew_point "
        "FROM heating_cycles "
        "WHERE zone_name = ? AND start_ts >= ? "
        "  AND runoff_drop IS NOT NULL AND runoff_drop > 0",
        (zone_name, cutoff)
    ).fetchall()

    total_points = len(rows)
    if total_points < min_cycles:
        return {
            "adjustment": 0.0,
            "confidence": 0.0,
            "band_used": None,
            "reasoning": (f"Need {min_cycles} cycles with runoff data, "
                          f"have {total_points}"),
            "avg_runoff_drop": None,
            "data_points": total_points,
            "band_breakdown": {},
        }

    # Bucket by dew point band
    all_drops = [r[0] for r in rows]
    bands = {}
    for drop, dp in rows:
        band = _dew_point_band(dp) if dp is not None else "unknown"
        bands.setdefault(band, []).append(drop)

    # Build band breakdown for display
    band_breakdown = {}
    for band_label, drops in bands.items():
        band_breakdown[band_label] = {
            "avg_runoff": round(statistics.mean(drops), 1),
            "n": len(drops),
        }

    # Pick the right band
    band_used = None
    drops_to_use = all_drops  # fallback: all data

    if current_dew_point is not None:
        target_band = _dew_point_band(current_dew_point)
        band_drops = bands.get(target_band, [])
        if len(band_drops) >= 3:
            drops_to_use = band_drops
            band_used = target_band
        else:
            band_used = f"all (no data for {target_band})"
    else:
        band_used = "all (no dew point)"

    avg_drop = statistics.mean(drops_to_use)
    std_drop = (statistics.stdev(drops_to_use)
                if len(drops_to_use) > 1 else avg_drop * 0.5)

    # Conservative: mean - 0.5*std, capped
    conservative = max(0, avg_drop - 0.5 * std_drop)
    adjustment = round(min(conservative, max_adjustment), 1)

    # Confidence: sample size + consistency
    cv = std_drop / avg_drop if avg_drop > 0 else 1.0
    size_factor = min(1.0, len(drops_to_use) / 10)
    consistency_factor = max(0.3, 1.0 - cv)
    confidence = round(size_factor * consistency_factor, 2)

    return {
        "adjustment": adjustment,
        "confidence": confidence,
        "band_used": band_used,
        "reasoning": (
            f"Band '{band_used}': avg runoff {avg_drop:.1f}% "
            f"(std {std_drop:.1f}%, n={len(drops_to_use)}) "
            f"→ conservative {adjustment:.1f}% early-off"),
        "avg_runoff_drop": round(avg_drop, 1),
        "data_points": total_points,
        "band_breakdown": band_breakdown,
    }


# ── Zone Profile ─────────────────────────────────────────────────────────────

def compute_zone_profile(conn: sqlite3.Connection, zone_name: str,
                         days: int = 30) -> dict:
    """Aggregate stats for a zone over the last N days from heating_cycles."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"

    rows = conn.execute(
        "SELECT duration_hours, drying_rate, rebound_rate, "
        "       humidity_start, humidity_end, reached_threshold, "
        "       runoff_drop, runoff_duration_hours, avg_outdoor_dew_point "
        "FROM heating_cycles "
        "WHERE zone_name = ? AND start_ts >= ? "
        "ORDER BY start_ts",
        (zone_name, cutoff)
    ).fetchall()

    if not rows:
        return {
            "zone_name": zone_name,
            "cycles_30d": 0,
            "status": "No data",
        }

    durations = [r[0] for r in rows if r[0] is not None]
    rates = [r[1] for r in rows if r[1] is not None]
    rebounds = [r[2] for r in rows if r[2] is not None]
    peaks = [r[3] for r in rows if r[3] is not None]
    troughs = [r[4] for r in rows if r[4] is not None]

    # Runoff stats
    runoffs = [r[6] for r in rows if r[6] is not None and r[6] > 0]
    runoff_durs = [r[7] for r in rows if r[7] is not None and r[7] > 0]

    avg_rate = round(statistics.mean(rates), 1) if rates else None
    med_recovery = round(statistics.median(durations), 1) if durations else None
    avg_rebound = round(statistics.mean(rebounds), 1) if rebounds else None
    avg_runoff = round(statistics.mean(runoffs), 1) if runoffs else None
    avg_runoff_dur = (round(statistics.mean(runoff_durs), 2)
                      if runoff_durs else None)

    # Runoff per dew point band
    runoff_by_band = {}
    for r in rows:
        drop, dp = r[6], r[8]
        if drop is not None and drop > 0 and dp is not None:
            band = _dew_point_band(dp)
            runoff_by_band.setdefault(band, []).append(drop)
    band_stats = {
        band: {"avg_runoff": round(statistics.mean(drops), 1), "n": len(drops)}
        for band, drops in runoff_by_band.items()
    }

    # Classify status
    if avg_rate and avg_rate >= 2.0:
        status = "Good"
    elif avg_rate and avg_rate >= 1.0:
        status = "Moderate"
    elif avg_rate:
        status = "Slow"
    else:
        status = "Unknown"

    if avg_rebound and avg_rebound > 2.0:
        status += " / High rebound"
    if avg_runoff and avg_runoff >= 2.0:
        status += " / Good runoff"

    return {
        "zone_name": zone_name,
        "avg_drying_rate": avg_rate,
        "median_recovery_time": med_recovery,
        "avg_rebound_rate": avg_rebound,
        "typical_peak_humidity": (round(statistics.mean(peaks))
                                  if peaks else None),
        "typical_trough_humidity": (round(statistics.mean(troughs))
                                    if troughs else None),
        "cycles_30d": len(rows),
        "status": status,
        "avg_runoff_drop": avg_runoff,
        "avg_runoff_duration": avg_runoff_dur,
        "runoff_data_points": len(runoffs),
        "runoff_by_band": band_stats,
    }


# ── Optimal Warm Hours ───────────────────────────────────────────────────────

def compute_optimal_warm_hours(conn: sqlite3.Connection,
                               current_hours: int = 6,
                               days: int = 30) -> dict:
    """
    Recommend warm_hours_count based on actual heating durations.

    Algorithm:
    - For each day, compute effective heating hours (total / concurrent zones)
    - Take p75 + 25% safety margin, clamp to [2, 12]
    - Require >= 7 days and >= 5 cycles
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"

    rows = conn.execute(
        "SELECT zone_name, start_ts, duration_hours "
        "FROM heating_cycles WHERE start_ts >= ? ORDER BY start_ts",
        (cutoff,)
    ).fetchall()

    if len(rows) < 5:
        return None

    # Group by date
    daily = {}
    for zone_name, start_ts, duration in rows:
        day = start_ts[:10]
        daily.setdefault(day, []).append((zone_name, duration))

    if len(daily) < 7:
        return None

    daily_needs = []
    for day, day_cycles in daily.items():
        total = sum(d for _, d in day_cycles)
        zones = len(set(z for z, _ in day_cycles))
        effective = total / max(1, zones)
        daily_needs.append(effective)

    daily_needs.sort()
    p75_idx = int(len(daily_needs) * 0.75)
    p75 = daily_needs[p75_idx]
    recommended = math.ceil(p75 * 1.25)
    recommended = max(2, min(12, recommended))

    # Confidence from coefficient of variation
    mean = statistics.mean(daily_needs)
    std = statistics.stdev(daily_needs) if len(daily_needs) > 1 else 0
    cv = std / mean if mean > 0 else 1
    confidence = max(0.3, min(0.95, 1.0 - cv))

    return {
        "current": current_hours,
        "recommended": recommended,
        "confidence": round(confidence, 2),
        "data_days": len(daily),
        "data_cycles": len(rows),
        "p75_hours": round(p75, 1),
        "reasoning": (
            f"Based on {len(rows)} cycles over {len(daily)} days: "
            f"p75 effective heating = {p75:.1f}h → "
            f"recommended {recommended}h (with margin)"
        ),
    }


# ── Full Analysis Orchestrator ───────────────────────────────────────────────

def run_full_analysis(conn: sqlite3.Connection, config: dict,
                      days: int = 30) -> dict:
    """
    Run a full analytics pass: detect cycles from raw readings,
    compute zone profiles, and generate warm hours recommendation.

    Returns dict with zone_profiles and warm_hours_recommendation.
    """
    create_analytics_tables(conn)
    off_thresh = config.get("humidity_off_threshold", 65)
    poll_interval = config.get("poll_interval_seconds", 300)
    max_gap = int(poll_interval * 2.5 / 60)  # 2.5x poll interval in minutes

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"

    # Get all zone names
    zone_rows = conn.execute(
        "SELECT DISTINCT zone_name FROM zone_readings "
        "WHERE timestamp >= ?", (cutoff,)
    ).fetchall()
    zone_names = [r[0] for r in zone_rows]

    total_new = 0

    for zone_name in zone_names:
        # Get readings for this zone
        readings = conn.execute(
            "SELECT timestamp, zone_name, device_id, temperature, "
            "       humidity, power, outdoor_temp, outdoor_dew_point "
            "FROM zone_readings "
            "WHERE zone_name = ? AND timestamp >= ? "
            "ORDER BY timestamp ASC",
            (zone_name, cutoff)
        ).fetchall()

        readings = [dict(zip(
            ["timestamp", "zone_name", "device_id", "temperature",
             "humidity", "power", "outdoor_temp",
             "outdoor_dew_point"], r)) for r in readings]

        if not readings:
            continue

        # Detect cycles
        cycles = detect_heating_cycles(readings, off_thresh, max_gap)

        for cycle in cycles:
            # Compute rebound rate
            cycle["rebound_rate"] = compute_rebound_rate(
                readings, cycle["end_ts"])

            # Compute runoff (coast-down after heating stops)
            runoff = compute_runoff(
                readings, cycle["end_ts"],
                cycle.get("humidity_end"),
                window_hours=3.0)
            if runoff:
                cycle["runoff_drop"] = runoff["runoff_drop"]
                cycle["runoff_duration_hours"] = runoff["runoff_duration_hours"]
                cycle["runoff_trough_humidity"] = runoff["runoff_trough_humidity"]

            # Store (upsert — update metrics if cycle already exists)
            try:
                cur = conn.execute(
                    "INSERT INTO heating_cycles "
                    "(zone_name, device_id, start_ts, end_ts, "
                    " duration_hours, humidity_start, humidity_end, "
                    " humidity_drop, drying_rate, avg_outdoor_temp, "
                    " avg_indoor_temp, reached_threshold, rebound_rate, "
                    " runoff_drop, runoff_duration_hours, "
                    " runoff_trough_humidity, avg_outdoor_dew_point) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(zone_name, start_ts) DO UPDATE SET "
                    " end_ts=excluded.end_ts,"
                    " duration_hours=excluded.duration_hours,"
                    " humidity_end=excluded.humidity_end,"
                    " humidity_drop=excluded.humidity_drop,"
                    " drying_rate=excluded.drying_rate,"
                    " avg_outdoor_temp=excluded.avg_outdoor_temp,"
                    " reached_threshold=excluded.reached_threshold,"
                    " rebound_rate=excluded.rebound_rate,"
                    " runoff_drop=excluded.runoff_drop,"
                    " runoff_duration_hours=excluded.runoff_duration_hours,"
                    " runoff_trough_humidity=excluded.runoff_trough_humidity,"
                    " avg_outdoor_dew_point=excluded.avg_outdoor_dew_point",
                    (cycle["zone_name"], cycle["device_id"],
                     cycle["start_ts"], cycle["end_ts"],
                     cycle["duration_hours"], cycle["humidity_start"],
                     cycle["humidity_end"], cycle["humidity_drop"],
                     cycle["drying_rate"], cycle["avg_outdoor_temp"],
                     cycle["avg_indoor_temp"],
                     1 if cycle["reached_threshold"] else 0,
                     cycle.get("rebound_rate"),
                     cycle.get("runoff_drop"),
                     cycle.get("runoff_duration_hours"),
                     cycle.get("runoff_trough_humidity"),
                     cycle.get("avg_outdoor_dew_point")))
                if cur.rowcount > 0:
                    total_new += 1
            except sqlite3.IntegrityError:
                pass

    conn.commit()

    # Compute zone profiles
    zone_profiles = {}
    for zone_name in zone_names:
        profile = compute_zone_profile(conn, zone_name, days)
        if profile["cycles_30d"] > 0:
            zone_profiles[zone_name] = profile

    # Compute warm hours recommendation
    current_hours = config.get("warm_hours_count", 6)
    warm_rec = compute_optimal_warm_hours(conn, current_hours, days)

    log.info("Analytics: analyzed %d zones, found %d new cycles",
             len(zone_names), total_new)
    if warm_rec:
        log.info("Analytics: warm hours recommendation = %dh (currently %dh, "
                 "confidence %.0f%%)",
                 warm_rec["recommended"], warm_rec["current"],
                 warm_rec["confidence"] * 100)

    return {
        "zone_profiles": zone_profiles,
        "warm_hours_recommendation": warm_rec,
        "analyzed_at": datetime.utcnow().isoformat() + "Z",
    }


# ── Cross-Zone Heating Impact (Phase 8) ──────────────────────────────────────

def compute_cross_zone_impact(conn: sqlite3.Connection,
                              days: int = 30) -> dict:
    """
    Analyze how heating one zone affects adjacent zones' temperatures.

    For each heating cycle in zone A, measure temp change in zone B
    during the same period.  Returns a matrix of zone→zone influence.
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"

    # Get all heating cycles
    cycles = conn.execute(
        "SELECT zone_name, start_ts, end_ts, duration_hours "
        "FROM heating_cycles WHERE start_ts >= ? AND duration_hours >= 0.5",
        (cutoff,)
    ).fetchall()

    if not cycles:
        return {"matrix": {}, "message": "No heating cycles found"}

    zone_names = sorted(set(r[0] for r in cycles))
    matrix = {}

    for source_zone, start_ts, end_ts, duration in cycles:
        for target_zone in zone_names:
            if target_zone == source_zone:
                continue

            # Get target zone's temp readings during this cycle
            temps = conn.execute(
                "SELECT temperature FROM zone_readings "
                "WHERE zone_name = ? AND timestamp >= ? AND timestamp <= ? "
                "  AND temperature IS NOT NULL "
                "ORDER BY timestamp",
                (target_zone, start_ts, end_ts)
            ).fetchall()

            if len(temps) < 2:
                continue

            temp_change = temps[-1][0] - temps[0][0]
            key = f"{source_zone} → {target_zone}"
            matrix.setdefault(key, []).append(temp_change)

    # Average the impacts
    result = {}
    for key, changes in matrix.items():
        avg = sum(changes) / len(changes)
        result[key] = {
            "avg_temp_change": round(avg, 2),
            "observations": len(changes),
            "significant": abs(avg) > 0.3,
        }

    return {"matrix": result, "zone_count": len(zone_names)}


# ── Condensation Event Logger (Phase 8) ─────────────────────────────────────

def get_condensation_events(conn: sqlite3.Connection,
                            days: int = 30,
                            threshold: float = 4.0) -> list[dict]:
    """
    Find periods where DP spread dropped below threshold (condensation risk).

    Returns list of events with zone, start, duration, min_spread.
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"

    try:
        rows = conn.execute(
            "SELECT zone_name, created_at, dp_spread "
            "FROM control_log "
            "WHERE created_at >= ? AND dp_spread IS NOT NULL "
            "ORDER BY zone_name, created_at",
            (cutoff,)
        ).fetchall()
    except Exception:
        return []

    events = []
    current_event = None

    for zone, ts, spread in rows:
        if spread < threshold:
            if current_event is None or current_event["zone"] != zone:
                if current_event:
                    events.append(current_event)
                current_event = {
                    "zone": zone,
                    "start": ts,
                    "end": ts,
                    "min_spread": spread,
                    "readings": 1,
                }
            else:
                current_event["end"] = ts
                current_event["min_spread"] = min(
                    current_event["min_spread"], spread)
                current_event["readings"] += 1
        else:
            if current_event and current_event["zone"] == zone:
                events.append(current_event)
                current_event = None

    if current_event:
        events.append(current_event)

    # Compute durations
    for e in events:
        try:
            start = datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(e["end"].replace("Z", "+00:00"))
            e["duration_hours"] = round(
                (end - start).total_seconds() / 3600, 1)
        except Exception:
            e["duration_hours"] = 0

    return sorted(events, key=lambda e: e["start"], reverse=True)


def get_recent_cycles(conn: sqlite3.Connection, days: int = 7,
                      zone_name: str = None) -> list:
    """Get recent heating cycles for display."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
    cols = ("zone_name, start_ts, end_ts, duration_hours, "
            "humidity_start, humidity_end, drying_rate, "
            "avg_outdoor_temp, rebound_rate, reached_threshold, "
            "runoff_drop, runoff_duration_hours, avg_outdoor_dew_point")
    if zone_name:
        rows = conn.execute(
            f"SELECT {cols} FROM heating_cycles "
            "WHERE zone_name = ? AND start_ts >= ? ORDER BY start_ts DESC",
            (zone_name, cutoff)
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {cols} FROM heating_cycles "
            "WHERE start_ts >= ? ORDER BY start_ts DESC",
            (cutoff,)
        ).fetchall()

    col_names = [c.strip() for c in cols.split(",")]
    return [dict(zip(col_names, r)) for r in rows]

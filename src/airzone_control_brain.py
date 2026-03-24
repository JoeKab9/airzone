"""
Airzone Control Brain — DP-Spread Predictive Controller
========================================================
Replaces simple humidity-threshold control with a dew-point-spread-based
predictive decision engine.

The brain uses:
 - Sensor fusion (Netatmo humidity preferred, temperatures averaged)
 - DP spread hysteresis band (heat ON < 4°C, OFF ≥ 6°C)
 - Predictive shutoff for concrete thermal inertia (runoff)
 - DP spread predictions using weather forecast + learned thermal model
 - COP-aware deferral (defer heating to warmer outdoor hours)
 - Occupancy detection via Netatmo CO2 & noise
 - Daily energy assessment with Linky reconciliation

All data stored in local SQLite — no cloud dependency.

Usage:
    from airzone_control_brain import ControlBrain
    brain = ControlBrain(db_path)
    results = brain.run_cycle(api, cfg, weather_info, netatmo_modules)
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

log = logging.getLogger("airzone")


# ── Constants ────────────────────────────────────────────────────────────────

# DP spread thresholds — hysteresis band to prevent cycling
DP_SPREAD_HEAT_ON = 4       # Heat ON when spread drops below 4°C
DP_SPREAD_HEAT_OFF = 6      # Heat OFF when spread reaches 6°C (safe margin)
DP_SPREAD_CRITICAL = 2      # Immediate override — heat NOW regardless of COP/deferral
MAX_INDOOR_TEMP = 18        # Never heat if indoor > 18°C
PREDICTIVE_SHUTOFF_MARGIN = 1.0   # Shut off 1°C early for concrete runoff
DEFAULT_TARIFF = 0.1927     # €/kWh (EDF Tarif Bleu Base 9kVA) — overridden by config/DB
PREDICTION_HORIZON_H = 3    # Predict DP spread 3 hours ahead
PREDICTION_TRUST_RMSE = 1.5 # Trust predictions only if RMSE < 1.5°C
MIN_PREDICTIONS_FOR_TRUST = 10
DEFAULT_HP_KW = 2.5         # Default heat pump power — overridden by config

# Netatmo → Airzone zone name mapping (must match exact Airzone casing)
NETATMO_TO_AIRZONE = {
    "Cuisine Base": "Cuisine",
    "Boyz": "Studio",
    "Slaapkamer": "Mur bleu",
}
NETATMO_IGNORE = {"Indoor", "Ukkel Buiten"}


# ── Dew Point / Humidity Calculations ────────────────────────────────────────

def calc_dewpoint(temp_c: float, rh: float) -> float:
    """Magnus-Tetens formula for dew point temperature."""
    if rh <= 0 or temp_c is None:
        return 0.0
    a = 17.625
    b = 243.04
    gamma = (a * temp_c) / (b + temp_c) + math.log(max(rh, 1) / 100)
    return round((b * gamma) / (a - gamma), 1)


def calc_room_dewpoint(
    az_temp: float, az_hum: float,
    nt_temp: Optional[float] = None, nt_hum: Optional[float] = None,
) -> float:
    """
    Best dew point for a room using all available sensor data.
    Temperature: average Airzone + Netatmo.
    Humidity: prefer Netatmo (more accurate), fallback to Airzone.
    """
    temps = [az_temp]
    if nt_temp is not None and nt_temp > 0:
        temps.append(nt_temp)
    best_temp = sum(temps) / len(temps)
    best_hum = nt_hum if (nt_hum is not None and nt_hum > 0) else az_hum
    return calc_dewpoint(best_temp, best_hum)


def calc_absolute_humidity(temp_c: float, rh: float) -> float:
    """Absolute humidity in g/m³ (Magnus variant)."""
    es = 6.112 * math.exp((17.67 * temp_c) / (temp_c + 243.5))
    return (es * rh * 2.1674) / (273.15 + temp_c)


# ── Occupancy Detection ──────────────────────────────────────────────────────

def detect_occupancy(netatmo_modules: list[dict]) -> dict:
    """
    Detect occupancy from Netatmo CO2 + noise levels.
    Returns {occupied: bool, signals: [str, ...]}.
    """
    signals = []
    for mod in netatmo_modules:
        name = mod.get("name") or mod.get("module_name", "")
        if name in NETATMO_IGNORE:
            continue
        co2 = mod.get("CO2") or mod.get("co2")
        noise = mod.get("Noise") or mod.get("noise")
        if co2 and co2 > 600:
            signals.append(f"CO2 {co2}ppm in {name}")
        if noise and noise > 45:
            signals.append(f"Noise {noise}dB in {name}")
    return {"occupied": len(signals) >= 2, "signals": signals}


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class ZoneRunoff:
    avg_runoff_h: float = 1.5
    avg_peak_rise_c: float = 0.3
    avg_decay_rate_per_h: float = 0.2


@dataclass
class DpPrediction:
    predicted_dp_spread: float = 0.0
    predicted_indoor_temp: float = 0.0
    forecast_outdoor_temp: float = 0.0
    forecast_outdoor_humidity: float = 0.0
    confidence: str = "low"       # "high" / "medium" / "low"
    reasoning: str = ""
    natural_drying: bool = False  # outdoor AH < indoor AH
    runoff_boost: float = 0.0     # extra °C DP spread from concrete runoff
    best_cop_hour: Optional[str] = None
    best_cop_temp: Optional[float] = None
    cop_saving_pct: float = 0.0


@dataclass
class ZoneDecision:
    zone_name: str = ""
    action: str = "no_change"     # heating_on, heating_off, skip_heating, defer_heating, no_change
    reason: str = ""
    success: bool = True

    # Sensor readings
    humidity_airzone: int = 0
    humidity_netatmo: Optional[int] = None
    temperature: float = 0.0
    dewpoint: float = 0.0
    dp_spread: float = 0.0

    # Weather
    outdoor_temp: Optional[float] = None
    outdoor_humidity: Optional[float] = None
    forecast_temp_max: Optional[float] = None
    forecast_best_hour: Optional[str] = None

    # Extras
    occupancy_detected: bool = False
    energy_saved_pct: float = 0.0
    prediction_decision: Optional[str] = None  # skip_heating, defer_cop, heat_anyway, early_stop

    # Prediction details (optional)
    prediction: Optional[DpPrediction] = None


# ── DP Spread Prediction ─────────────────────────────────────────────────────

def _get_zone_runoff(learned: dict, zone_name: str) -> ZoneRunoff:
    """Get zone-specific runoff parameters from learned data."""
    runoff_data = learned.get("learned_runoff", {})

    # Try zone-specific data from heating-analysis
    by_zone = runoff_data.get("factors", {}).get("byZone", {})
    if zone_name in by_zone:
        z = by_zone[zone_name]
        return ZoneRunoff(
            avg_runoff_h=z.get("avgRunoff", 1.5),
            avg_peak_rise_c=z.get("avgRise", 0.3),
            avg_decay_rate_per_h=z.get("avgDecay", 0.2),
        )
    # Fallback to global averages
    return ZoneRunoff(
        avg_runoff_h=runoff_data.get("estimatedRunoffHours", 1.5),
        avg_peak_rise_c=0.3,
        avg_decay_rate_per_h=runoff_data.get("tempDecayRate", 0.2),
    )


def predict_dp_spread(
    current_indoor_temp: float,
    current_indoor_hum: float,
    current_dp_spread: float,
    forecast_24h: list[dict],
    learned: dict,
    zone_name: str,
    is_currently_heating: bool,
    hours_ahead: int = PREDICTION_HORIZON_H,
    best_heating_window: Optional[dict] = None,
) -> Optional[DpPrediction]:
    """
    Predict DP spread N hours ahead using current conditions + weather forecast.

    forecast_24h: list of {time: str, temp: float, humidity: float}
    learned: dict of learned parameters from system_state
    best_heating_window: {hour: str, temp: float} or None
    """
    if len(forecast_24h) < hours_ahead:
        return None

    zone_runoff = _get_zone_runoff(learned, zone_name)

    # Learned infiltration rate (how fast indoor AH drifts toward outdoor)
    pred_model = learned.get("prediction_model", {})
    infiltration_rate = pred_model.get("infiltration_rate", 0.15)

    current_indoor_ah = calc_absolute_humidity(current_indoor_temp, current_indoor_hum)

    # Forecast conditions over prediction window
    window = forecast_24h[:hours_ahead]
    avg_outdoor_temp = sum(f.get("temp", 0) for f in window) / len(window)
    avg_outdoor_hum = sum(f.get("humidity", 50) for f in window) / len(window)
    outdoor_ah = calc_absolute_humidity(avg_outdoor_temp, avg_outdoor_hum)

    # ── Predict indoor temp ──
    temp_delta = avg_outdoor_temp - current_indoor_temp
    predicted_temp = current_indoor_temp + \
        temp_delta * zone_runoff.avg_decay_rate_per_h * hours_ahead * 0.15

    # If currently heating: account for concrete thermal runoff
    runoff_temp_boost = 0.0
    if is_currently_heating:
        runoff_temp_boost = zone_runoff.avg_peak_rise_c
        predicted_temp += runoff_temp_boost

    # Clamp temperature
    if temp_delta < 0:
        clamped_temp = max(
            avg_outdoor_temp,
            min(predicted_temp, current_indoor_temp + runoff_temp_boost + 1),
        )
    else:
        clamped_temp = min(avg_outdoor_temp + 3, predicted_temp)

    # ── Predict indoor AH ──
    ah_delta = outdoor_ah - current_indoor_ah
    predicted_ah = current_indoor_ah + ah_delta * infiltration_rate * hours_ahead
    clamped_ah = max(1.0, min(predicted_ah, 30.0))

    # ── Convert to RH and DP spread ──
    es = 6.112 * math.exp((17.67 * clamped_temp) / (clamped_temp + 243.5))
    max_ah = (es * 100 * 2.1674) / (273.15 + clamped_temp)
    predicted_rh = min(100, max(10, (clamped_ah / max_ah) * 100))
    predicted_dp = calc_dewpoint(clamped_temp, predicted_rh)
    predicted_dp_spread = round(clamped_temp - predicted_dp, 1)

    # Runoff boost to DP spread
    runoff_boost = round(zone_runoff.avg_peak_rise_c, 1) if is_currently_heating else 0.0

    # ── Best COP window analysis ──
    best_cop_hour = None
    best_cop_temp = None
    cop_saving_pct = 0.0
    current_outdoor_temp = forecast_24h[0].get("temp", avg_outdoor_temp) if forecast_24h else avg_outdoor_temp
    if best_heating_window and best_heating_window.get("temp", 0) > current_outdoor_temp + 2:
        best_cop_hour = best_heating_window.get("hour")
        best_cop_temp = best_heating_window.get("temp")
        cop_saving_pct = round((best_cop_temp - current_outdoor_temp) * 3)

    # ── Confidence ──
    rmse = pred_model.get("rmse")
    if rmse is not None and rmse < 1.0:
        confidence = "high"
    elif rmse is not None and rmse < PREDICTION_TRUST_RMSE:
        confidence = "medium"
    else:
        confidence = "low"

    # ── Reasoning ──
    natural_drying = outdoor_ah < current_indoor_ah
    parts = []
    if natural_drying:
        parts.append(f"Outdoor AH {outdoor_ah:.1f}g/m³ < indoor "
                      f"{current_indoor_ah:.1f}g/m³ → drying.")
    else:
        parts.append(f"Outdoor AH {outdoor_ah:.1f}g/m³ ≥ indoor "
                      f"{current_indoor_ah:.1f}g/m³ → no drying.")
    if is_currently_heating and runoff_boost > 0:
        parts.append(f"Runoff: +{runoff_boost}° spread after stop "
                      f"({zone_runoff.avg_runoff_h:.1f}h thermal mass).")
    parts.append(f"Predicted: {predicted_dp_spread}° in {hours_ahead}h "
                  f"(temp {clamped_temp:.1f}°C).")
    if cop_saving_pct > 0:
        parts.append(f"Best COP: {best_cop_temp}°C at {best_cop_hour} "
                      f"({cop_saving_pct}% saving).")

    return DpPrediction(
        predicted_dp_spread=predicted_dp_spread,
        predicted_indoor_temp=round(clamped_temp, 1),
        forecast_outdoor_temp=round(avg_outdoor_temp, 1),
        forecast_outdoor_humidity=round(avg_outdoor_hum),
        confidence=confidence,
        reasoning=" ".join(parts),
        natural_drying=natural_drying,
        runoff_boost=runoff_boost,
        best_cop_hour=best_cop_hour,
        best_cop_temp=best_cop_temp,
        cop_saving_pct=cop_saving_pct,
    )


# ── SQLite Schema ────────────────────────────────────────────────────────────

def create_brain_tables(conn: sqlite3.Connection):
    """Create control brain tables if they don't exist."""
    conn.execute("""CREATE TABLE IF NOT EXISTS control_log (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            zone_name             TEXT    NOT NULL,
            action                TEXT    NOT NULL,
            humidity_airzone      INTEGER,
            humidity_netatmo      INTEGER,
            temperature           REAL,
            dewpoint              REAL,
            dp_spread             REAL,
            outdoor_temp          REAL,
            outdoor_humidity      REAL,
            forecast_temp_max     REAL,
            forecast_best_hour    TEXT,
            occupancy_detected    INTEGER DEFAULT 0,
            energy_saved_pct      REAL    DEFAULT 0,
            prediction_decision   TEXT,
            reason                TEXT,
            success               INTEGER DEFAULT 1
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_control_log_ts ON control_log(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_control_log_zone_ts ON control_log(zone_name, created_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_assessment (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            date                    TEXT    NOT NULL UNIQUE,
            avg_humidity_before     INTEGER,
            avg_humidity_after      INTEGER,
            humidity_improved       INTEGER DEFAULT 0,
            total_heating_kwh       REAL,
            total_cost_eur          REAL,
            heating_minutes         INTEGER,
            occupancy_detected      INTEGER DEFAULT 0,
            zones_above_65          INTEGER DEFAULT 0,
            zones_total             INTEGER DEFAULT 0,
            actual_kwh              REAL,
            estimation_accuracy_pct REAL,
            correction_factor       REAL,
            notes                   TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assessment_date ON daily_assessment(date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS system_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS dp_spread_predictions (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            zone_name                TEXT    NOT NULL,
            predicted_for            TEXT    NOT NULL,
            hours_ahead              INTEGER NOT NULL,
            predicted_dp_spread      REAL,
            predicted_indoor_temp    REAL,
            predicted_outdoor_temp   REAL,
            predicted_outdoor_humidity REAL,
            current_dp_spread        REAL,
            current_indoor_temp      REAL,
            current_outdoor_temp     REAL,
            decision_made            TEXT,
            actual_dp_spread         REAL,
            actual_indoor_temp       REAL,
            prediction_error         REAL,
            validated                INTEGER DEFAULT 0,
            validated_at             TEXT,
            decision_correct         INTEGER
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_zone_ts ON dp_spread_predictions(zone_name, predicted_for)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_validated ON dp_spread_predictions(validated, predicted_for)")
    conn.commit()


# ── System State Helpers ─────────────────────────────────────────────────────

def _get_state(conn: sqlite3.Connection, key: str) -> Optional[dict]:
    """Get a value from system_state (JSON-decoded)."""
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = ?", (key,)
    ).fetchone()
    if row:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _set_state(conn: sqlite3.Connection, key: str, value):
    """Set a value in system_state (JSON-encoded)."""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) "
        "VALUES (?, ?, ?)",
        (key, json.dumps(value), now),
    )
    conn.commit()


def _get_learned_params(conn: sqlite3.Connection) -> dict:
    """Load all learned parameters from system_state."""
    params = {}
    rows = conn.execute(
        "SELECT key, value FROM system_state WHERE key IN "
        "('learned_runoff', 'heating_stats', 'occupancy_history', "
        " 'emergency_stop', 'prediction_model', 'learned_correction_factor')"
    ).fetchall()
    for key, val in rows:
        try:
            params[key] = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            pass
    return params


# ── Prediction Validation ────────────────────────────────────────────────────

def _validate_predictions(
    conn: sqlite3.Connection,
    zone_name: str,
    actual_dp_spread: float,
    actual_indoor_temp: float,
):
    """Validate past predictions that should have come true by now."""
    now = datetime.utcnow()
    window_start = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end = (now + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute(
        "SELECT id, predicted_dp_spread, decision_made "
        "FROM dp_spread_predictions "
        "WHERE zone_name = ? AND validated = 0 "
        "  AND predicted_for >= ? AND predicted_for <= ? "
        "LIMIT 5",
        (zone_name, window_start, window_end),
    ).fetchall()

    if not rows:
        return

    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in rows:
        pred_id, predicted_spread, decision = row
        error = round(predicted_spread - actual_dp_spread, 1)

        # Validate decision correctness
        decision_correct = None
        if decision in ("skip_heating", "early_stop"):
            decision_correct = 1 if actual_dp_spread >= DP_SPREAD_HEAT_ON else 0
        elif decision == "heat_anyway":
            decision_correct = 1 if actual_dp_spread < DP_SPREAD_HEAT_ON else 0
        elif decision == "defer_cop":
            decision_correct = 1 if actual_dp_spread > 2 else 0

        conn.execute(
            "UPDATE dp_spread_predictions SET "
            "  actual_dp_spread = ?, actual_indoor_temp = ?, "
            "  prediction_error = ?, validated = 1, validated_at = ?, "
            "  decision_correct = ? "
            "WHERE id = ?",
            (actual_dp_spread, actual_indoor_temp, error,
             now_iso, decision_correct, pred_id),
        )

    conn.commit()


def _get_prediction_accuracy(conn: sqlite3.Connection) -> dict:
    """Compute prediction model accuracy from validated predictions."""
    rows = conn.execute(
        "SELECT prediction_error, decision_correct, decision_made "
        "FROM dp_spread_predictions "
        "WHERE validated = 1 "
        "ORDER BY validated_at DESC LIMIT 100"
    ).fetchall()

    if not rows:
        return {"rmse": None, "bias": None, "count": 0,
                "correct_decisions": 0, "total_decisions": 0}

    errors = [r[0] for r in rows if r[0] is not None]
    rmse = None
    bias = None
    if errors:
        rmse = round(math.sqrt(sum(e * e for e in errors) / len(errors)), 2)
        bias = round(sum(errors) / len(errors), 2)

    decisions = [r for r in rows if r[2] is not None and r[1] is not None]
    correct = sum(1 for r in decisions if r[1] == 1)

    return {
        "rmse": rmse,
        "bias": bias,
        "count": len(errors),
        "correct_decisions": correct,
        "total_decisions": len(decisions),
    }


def _store_prediction(
    conn: sqlite3.Connection,
    zone_name: str,
    prediction: DpPrediction,
    current_dp_spread: float,
    current_indoor_temp: float,
    current_outdoor_temp: float,
    decision: str,
):
    """Store a prediction for later validation."""
    predicted_for = (
        datetime.utcnow() + timedelta(hours=PREDICTION_HORIZON_H)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn.execute(
        "INSERT INTO dp_spread_predictions "
        "(zone_name, predicted_for, hours_ahead, predicted_dp_spread, "
        " predicted_indoor_temp, predicted_outdoor_temp, "
        " predicted_outdoor_humidity, current_dp_spread, "
        " current_indoor_temp, current_outdoor_temp, decision_made) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (zone_name, predicted_for, PREDICTION_HORIZON_H,
         prediction.predicted_dp_spread, prediction.predicted_indoor_temp,
         prediction.forecast_outdoor_temp, prediction.forecast_outdoor_humidity,
         current_dp_spread, current_indoor_temp, current_outdoor_temp,
         decision),
    )
    conn.commit()


# ── Daily Assessment ─────────────────────────────────────────────────────────

def _run_daily_assessment(conn: sqlite3.Connection,
                          cfg: dict | None = None) -> Optional[dict]:
    """Generate a daily assessment from today's control_log entries."""
    today = date.today().isoformat()

    # Check if already assessed
    existing = conn.execute(
        "SELECT id FROM daily_assessment WHERE date = ?", (today,)
    ).fetchone()
    if existing:
        return None

    start_of_day = f"{today}T00:00:00Z"
    logs = conn.execute(
        "SELECT action, dewpoint, dp_spread, humidity_airzone, "
        "       humidity_netatmo, occupancy_detected, zone_name "
        "FROM control_log "
        "WHERE created_at >= ? ORDER BY created_at",
        (start_of_day,),
    ).fetchall()

    if len(logs) < 10:
        return None

    heating_on_count = sum(1 for r in logs if r[0] == "heating_on")
    heating_minutes = heating_on_count * 5  # poll interval ≈ 5 min

    # DP spread improvement
    first_logs = logs[:min(5, len(logs))]
    last_logs = logs[-min(5, len(logs)):]

    avg_spread_before = sum(r[2] or 0 for r in first_logs) / len(first_logs)
    avg_spread_after = sum(r[2] or 0 for r in last_logs) / len(last_logs)

    # Humidity (use max of airzone/netatmo)
    avg_hum_before = sum(
        max(r[3] or 0, r[4] or 0) for r in first_logs
    ) / len(first_logs)
    avg_hum_after = sum(
        max(r[3] or 0, r[4] or 0) for r in last_logs
    ) / len(last_logs)

    # Zones that had low DP spread
    zones_low_spread = len(set(
        r[6] for r in logs if (r[2] or 99) < DP_SPREAD_HEAT_ON
    ))
    zones_total = len(set(r[6] for r in logs))

    # Learned correction factor
    cf_data = _get_state(conn, "learned_correction_factor")
    cf = cf_data.get("factor", 1.0) if cf_data else 1.0

    hp_kw = (cfg or {}).get("hp_kw", DEFAULT_HP_KW)
    tariff = (cfg or {}).get("tariff", DEFAULT_TARIFF)
    est_kwh = (heating_minutes / 60) * hp_kw * cf
    est_cost = est_kwh * tariff

    dp_improved = avg_spread_after > avg_spread_before
    hum_improved = avg_hum_after < avg_hum_before

    notes = (
        f"DP spread {'improved' if dp_improved else 'did not improve'}: "
        f"{avg_spread_before:.1f}° → {avg_spread_after:.1f}°. CF={cf}."
    )

    assessment = {
        "date": today,
        "avg_humidity_before": round(avg_hum_before),
        "avg_humidity_after": round(avg_hum_after),
        "humidity_improved": 1 if (dp_improved or hum_improved) else 0,
        "total_heating_kwh": round(est_kwh, 1),
        "total_cost_eur": round(est_cost, 2),
        "heating_minutes": heating_minutes,
        "occupancy_detected": 1 if any(r[5] for r in logs) else 0,
        "zones_above_65": zones_low_spread,
        "zones_total": zones_total,
        "notes": notes,
    }

    conn.execute(
        "INSERT OR IGNORE INTO daily_assessment "
        "(date, avg_humidity_before, avg_humidity_after, humidity_improved, "
        " total_heating_kwh, total_cost_eur, heating_minutes, "
        " occupancy_detected, zones_above_65, zones_total, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (today, assessment["avg_humidity_before"],
         assessment["avg_humidity_after"], assessment["humidity_improved"],
         assessment["total_heating_kwh"], assessment["total_cost_eur"],
         assessment["heating_minutes"], assessment["occupancy_detected"],
         assessment["zones_above_65"], assessment["zones_total"],
         assessment["notes"]),
    )
    conn.commit()

    log.info("Daily assessment: %d min heating, %.1f kWh, €%.2f. %s",
             heating_minutes, est_kwh, est_cost, notes)
    return assessment


# ── Linky Reconciliation ─────────────────────────────────────────────────────

def _reconcile_yesterday(conn: sqlite3.Connection, cfg: dict) -> Optional[dict]:
    """
    Reconcile yesterday's estimated kWh with actual Linky reading.
    Updates daily_assessment and learned correction factor.
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # Check if yesterday has an unreconciled assessment
    row = conn.execute(
        "SELECT total_heating_kwh FROM daily_assessment "
        "WHERE date = ? AND actual_kwh IS NULL",
        (yesterday,),
    ).fetchone()
    if not row:
        return None

    estimated_kwh = row[0] or 0

    # Fetch actual consumption from Linky
    actual_kwh = _fetch_linky_actual(cfg, yesterday)
    if actual_kwh is None:
        return None

    accuracy_pct = 0.0
    if estimated_kwh > 0:
        accuracy_pct = round(
            (1 - abs(actual_kwh - estimated_kwh) / max(actual_kwh, 0.01)) * 100, 1
        )
    elif actual_kwh == 0:
        accuracy_pct = 100.0

    cf = round(actual_kwh / estimated_kwh, 3) if estimated_kwh > 0.1 else 1.0

    conn.execute(
        "UPDATE daily_assessment SET "
        "  actual_kwh = ?, estimation_accuracy_pct = ?, correction_factor = ?, "
        "  notes = notes || ? "
        "WHERE date = ?",
        (actual_kwh, accuracy_pct, cf,
         f" | Reconciled: est {estimated_kwh} kWh vs actual {actual_kwh} kWh "
         f"({accuracy_pct}% accurate).",
         yesterday),
    )

    # Update rolling correction factor
    recent = conn.execute(
        "SELECT correction_factor FROM daily_assessment "
        "WHERE correction_factor IS NOT NULL "
        "ORDER BY date DESC LIMIT 7"
    ).fetchall()
    if recent:
        avg_cf = sum(r[0] for r in recent) / len(recent)
        _set_state(conn, "learned_correction_factor", {
            "factor": round(avg_cf, 3),
            "samples": len(recent),
        })

    conn.commit()
    log.info("Reconciliation: est %.1f kWh vs actual %.1f kWh (%.1f%% accurate, CF=%.3f)",
             estimated_kwh, actual_kwh, accuracy_pct, cf)

    return {
        "date": yesterday,
        "actual_kwh": actual_kwh,
        "estimated_kwh": estimated_kwh,
        "accuracy_pct": accuracy_pct,
        "correction_factor": cf,
    }


def _fetch_linky_actual(cfg: dict, date_str: str) -> Optional[float]:
    """Fetch actual daily consumption from Linky/Conso API."""
    token = cfg.get("linky_token", "")
    prm = cfg.get("linky_prm", "")
    if not token or not prm:
        return None

    try:
        import requests
        next_day = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        resp = requests.get(
            f"https://conso.boris.sh/api/daily_consumption",
            params={"prm": prm, "start": date_str, "end": next_day},
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "airzone-daemon/1.0",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        intervals = (data.get("meter_reading", {}).get("interval_reading")
                      or data.get("interval_reading", []))
        if not intervals:
            return None
        total_wh = sum(float(iv.get("value", 0)) for iv in intervals)
        return round(total_wh / 1000, 2)
    except Exception as e:
        log.error("Linky reconciliation fetch error: %s", e)
        return None


# ── Control Brain ────────────────────────────────────────────────────────────

class ControlBrain:
    """
    DP-spread-based predictive HVAC controller.

    Replaces simple humidity threshold logic with a decision cascade:
      1. Emergency stop check
      2. Experiment block (no heating during experiments)
      3. Temperature limit (>18°C) with runoff-aware early shutoff
      4. Currently heating: early stop if runoff covers it
      5. Spread < 4°C + not heating: critical / skip / defer / heat
      6. Spread ≥ 6°C + heating: turn off
    """

    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn
        create_brain_tables(self.conn)

    def run_cycle(
        self,
        api,  # AirzoneCloudAPI instance
        cfg: dict,
        weather_info: Optional[dict] = None,
        netatmo_modules: Optional[list[dict]] = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Run one control cycle for all zones.

        Returns dict with:
          - zones: list of ZoneDecision dicts
          - outdoor: weather data
          - occupancy: detection result
          - prediction_model: accuracy stats
          - assessment: daily assessment (if generated)
          - reconciliation: Linky reconciliation (if performed)
        """
        netatmo_modules = netatmo_modules or []
        decisions: list[ZoneDecision] = []

        # Load learned parameters
        learned = _get_learned_params(self.conn)

        # Check emergency stop
        emergency_stop = learned.get("emergency_stop", {})
        if emergency_stop.get("active"):
            log.warning("⛔ Emergency stop active — skipping all control")

        # Build Netatmo lookup: Airzone zone name → Netatmo module data
        netatmo_by_zone: dict[str, dict] = {}
        for mod in netatmo_modules:
            name = mod.get("name") or mod.get("module_name", "")
            if name in NETATMO_IGNORE:
                continue
            az_name = NETATMO_TO_AIRZONE.get(name)
            if az_name:
                netatmo_by_zone[az_name] = mod

        # Occupancy
        occupancy = detect_occupancy(netatmo_modules)
        log.info("Occupancy: %s — %s",
                 "YES" if occupancy["occupied"] else "no",
                 ", ".join(occupancy["signals"]) or "no signals")

        # Heating stats (running totals)
        heating_stats = learned.get("heating_stats", {
            "totalOnMinutes": 0, "totalCycles": 0, "totalSaved": 0,
        })

        # Weather / forecast
        current_outdoor_temp = None
        current_outdoor_humidity = None
        best_window = None
        best_temp = None
        forecast_24h = []

        if weather_info:
            current_outdoor_temp = weather_info.get("current_outdoor_temp")
            current_outdoor_humidity = weather_info.get("current_outdoor_humidity")
            best_window = weather_info.get("best_heating_window")
            best_temp = best_window.get("temp") if best_window else current_outdoor_temp
            forecast_24h = weather_info.get("forecast_24h", [])

        # Prediction model accuracy
        pred_accuracy = _get_prediction_accuracy(self.conn)
        trust_predictions = (
            pred_accuracy["count"] >= MIN_PREDICTIONS_FOR_TRUST
            and pred_accuracy["rmse"] is not None
            and pred_accuracy["rmse"] < PREDICTION_TRUST_RMSE
        )
        log.info("Prediction model: %d validated, RMSE %s, trusted: %s, "
                 "decisions correct: %d/%d",
                 pred_accuracy["count"],
                 pred_accuracy["rmse"] if pred_accuracy["rmse"] is not None else "N/A",
                 trust_predictions,
                 pred_accuracy["correct_decisions"],
                 pred_accuracy["total_decisions"])

        # Update learned prediction model params (gradient descent on bias)
        if pred_accuracy["count"] >= 5 and pred_accuracy["bias"] is not None:
            current_model = learned.get("prediction_model", {})
            current_rate = current_model.get("infiltration_rate", 0.15)
            bias = pred_accuracy["bias"]
            adjustment = -0.005 if bias > 0.3 else (0.005 if bias < -0.3 else 0)
            new_rate = max(0.05, min(0.4, current_rate + adjustment))
            _set_state(self.conn, "prediction_model", {
                **current_model,
                "infiltration_rate": round(new_rate, 3),
                "rmse": pred_accuracy["rmse"],
                "bias": pred_accuracy["bias"],
                "validated_count": pred_accuracy["count"],
                "correct_decisions": pred_accuracy["correct_decisions"],
                "total_decisions": pred_accuracy["total_decisions"],
                "last_updated": datetime.utcnow().isoformat() + "Z",
            })
            # Refresh learned so we use the new model
            learned["prediction_model"] = {
                **current_model,
                "infiltration_rate": round(new_rate, 3),
                "rmse": pred_accuracy["rmse"],
            }

        # Check for active experiment
        experiment_active = _is_experiment_active(self.conn)

        # Get zones from Airzone API
        try:
            zones = api.get_all_zones()
        except Exception as e:
            log.error("API error reading zones: %s", e)
            return {"zones": [], "error": str(e)}

        if not zones:
            log.warning("No zones returned from API")
            return {"zones": [], "error": "No zones"}

        heat_mode = cfg.get("heating_mode", 3)
        setpoint = cfg.get("heating_setpoint", 22.0)

        # ── Control each zone ────────────────────────────────────────────
        for zone in zones:
            name = zone.get("name", "")
            dev_id = zone.get("_device_id", "")
            inst_id = zone.get("_installation_id", "")
            az_hum = zone.get("humidity") or 0
            indoor_temp = zone.get("local_temp")
            if isinstance(indoor_temp, dict):
                indoor_temp = indoor_temp.get("celsius", 0)
            indoor_temp = indoor_temp or 0.0
            power = zone.get("power") in (True, 1)

            # Netatmo sensor data
            netatmo = netatmo_by_zone.get(name)
            nt_hum = None
            nt_temp = None
            if netatmo:
                nt_hum = netatmo.get("Humidity") or netatmo.get("humidity")
                nt_temp = netatmo.get("Temperature") or netatmo.get("temperature")

            # Calculate dew point using best available data
            dewpoint = calc_room_dewpoint(indoor_temp, az_hum, nt_temp, nt_hum)
            dp_spread = round(indoor_temp - dewpoint, 1)

            # Validate past predictions for this zone
            try:
                _validate_predictions(self.conn, name, dp_spread, indoor_temp)
            except Exception as e:
                log.error("Prediction validation error for %s: %s", name, e)

            # Generate prediction
            best_hum = nt_hum if nt_hum else az_hum
            prediction = predict_dp_spread(
                indoor_temp, best_hum, dp_spread, forecast_24h,
                learned, name, power, PREDICTION_HORIZON_H, best_window,
            )

            # Zone-specific runoff
            zone_runoff = _get_zone_runoff(learned, name)

            # Decision
            decision = ZoneDecision(
                zone_name=name,
                humidity_airzone=az_hum,
                humidity_netatmo=nt_hum,
                temperature=indoor_temp,
                dewpoint=dewpoint,
                dp_spread=dp_spread,
                outdoor_temp=current_outdoor_temp,
                outdoor_humidity=current_outdoor_humidity,
                forecast_temp_max=best_temp,
                forecast_best_hour=best_window.get("hour") if best_window else None,
                occupancy_detected=occupancy["occupied"],
                prediction=prediction,
            )

            # Predictive shutoff limit for concrete thermal inertia
            predictive_limit = MAX_INDOOR_TEMP - PREDICTIVE_SHUTOFF_MARGIN
            too_warm = indoor_temp > predictive_limit
            approaching_limit = indoor_temp > (predictive_limit - 0.5) and power

            if emergency_stop.get("active"):
                # ── Emergency stop: log only ──
                decision.action = "no_change"
                decision.reason = (
                    f"⛔ Emergency stop active. Manual control via Airzone app. "
                    f"DP spread {dp_spread}°."
                )

            elif experiment_active:
                # ── Experiment: block all heating ──
                if power:
                    decision.action = "heating_off"
                    decision.reason = (
                        f"🧪 Experiment active. Off for analysis. "
                        f"DP spread {dp_spread}°."
                    )
                    if not dry_run:
                        try:
                            api.set_zone(dev_id, installation_id=inst_id,
                                         power=False)
                        except Exception as e:
                            decision.reason += f" FAILED: {e}"
                            decision.success = False
                else:
                    decision.action = "no_change"
                    decision.reason = (
                        f"🧪 Experiment active. Heating blocked. "
                        f"DP spread {dp_spread}°, indoor {indoor_temp}°C."
                    )

            elif too_warm or approaching_limit:
                # ── Temp limit: concrete runoff shutoff ──
                if power:
                    decision.action = "heating_off"
                    decision.reason = (
                        f"Predictive shutoff: {indoor_temp}°C approaching "
                        f"{MAX_INDOOR_TEMP}°C (margin {PREDICTIVE_SHUTOFF_MARGIN}°C, "
                        f"runoff {zone_runoff.avg_runoff_h:.1f}h). "
                        f"DP spread {dp_spread}°."
                    )
                    if not dry_run:
                        try:
                            api.set_zone(dev_id, installation_id=inst_id,
                                         power=False)
                        except Exception as e:
                            decision.reason += f" FAILED: {e}"
                            decision.success = False
                else:
                    decision.action = "no_change"
                    decision.reason = (
                        f"Indoor {indoor_temp}°C ≥ {predictive_limit}°C limit. "
                        f"No heating. DP spread {dp_spread}°."
                    )

            elif power and dp_spread < DP_SPREAD_HEAT_OFF:
                # ── Currently heating: check if runoff will carry us to safety ──
                spread_after_runoff = dp_spread + zone_runoff.avg_peak_rise_c
                if (
                    prediction
                    and trust_predictions
                    and spread_after_runoff >= DP_SPREAD_HEAT_OFF
                    and prediction.predicted_dp_spread >= DP_SPREAD_HEAT_ON
                ):
                    # Runoff + natural trends → safe zone → stop early!
                    decision.action = "heating_off"
                    decision.prediction_decision = "early_stop"
                    decision.energy_saved_pct = 30
                    decision.reason = (
                        f"⏱ Early stop: spread {dp_spread}° + runoff "
                        f"{zone_runoff.avg_peak_rise_c:.1f}° = "
                        f"~{spread_after_runoff:.1f}° (≥{DP_SPREAD_HEAT_OFF}°). "
                        f"Model: {prediction.predicted_dp_spread}° in "
                        f"{PREDICTION_HORIZON_H}h. {prediction.reasoning}"
                    )
                    if not dry_run:
                        try:
                            api.set_zone(dev_id, installation_id=inst_id,
                                         power=False)
                        except Exception as e:
                            decision.reason += f" FAILED: {e}"
                            decision.success = False
                else:
                    # Keep heating
                    decision.action = "no_change"
                    decision.reason = (
                        f"Heating: spread {dp_spread}° (target "
                        f"≥{DP_SPREAD_HEAT_OFF}°), runoff would add "
                        f"~{zone_runoff.avg_peak_rise_c:.1f}°."
                    )
                    if prediction:
                        decision.reason += (
                            f" Model: {prediction.predicted_dp_spread}° "
                            f"in {PREDICTION_HORIZON_H}h."
                        )
                    heating_stats["totalOnMinutes"] = \
                        heating_stats.get("totalOnMinutes", 0) + 5

            elif dp_spread < DP_SPREAD_HEAT_ON and not power:
                # ── DP spread too low — unified decision ──
                self._decide_low_spread(
                    decision, prediction, trust_predictions,
                    dp_spread, indoor_temp, dewpoint,
                    current_outdoor_temp, best_temp, best_window,
                    zone_runoff, heating_stats,
                    api, dev_id, inst_id, heat_mode, setpoint,
                    dry_run,
                )

            elif dp_spread >= DP_SPREAD_HEAT_OFF and power:
                # ── DP spread safe → turn off ──
                decision.action = "heating_off"
                decision.reason = (
                    f"DP spread {dp_spread}° ≥ {DP_SPREAD_HEAT_OFF}° (safe). "
                    f"DP {dewpoint}°C. Off."
                )
                if not dry_run:
                    try:
                        api.set_zone(dev_id, installation_id=inst_id,
                                     power=False)
                    except Exception as e:
                        decision.reason += f" FAILED: {e}"
                        decision.success = False

            else:
                # ── Idle or in-band ──
                decision.action = "no_change"
                decision.reason = (
                    f"DP spread {dp_spread}°, indoor {indoor_temp}°C, "
                    f"DP {dewpoint}°C — "
                    f"{'heating (band)' if power else 'idle'}."
                )
                if prediction:
                    decision.reason += (
                        f" Forecast: {prediction.predicted_dp_spread}° "
                        f"in {PREDICTION_HORIZON_H}h."
                    )

            # Store prediction if a decision was made
            if prediction and decision.prediction_decision:
                try:
                    _store_prediction(
                        self.conn, name, prediction, dp_spread,
                        indoor_temp, current_outdoor_temp or 0,
                        decision.prediction_decision,
                    )
                except Exception as e:
                    log.error("Store prediction error: %s", e)

            decisions.append(decision)
            log.info("  %s: %s — %s", name, decision.action, decision.reason)

        # ── Log all decisions ────────────────────────────────────────────
        self._log_decisions(decisions)

        # ── Update learned params ────────────────────────────────────────
        heating_stats["totalSaved"] = heating_stats.get("totalSaved", 0) + \
            sum(d.energy_saved_pct for d in decisions)
        _set_state(self.conn, "heating_stats", heating_stats)
        _set_state(self.conn, "occupancy_history", {
            "lastCheck": datetime.utcnow().isoformat() + "Z",
            "occupied": occupancy["occupied"],
            "signals": occupancy["signals"],
        })

        # ── Daily assessment + reconcile ─────────────────────────────────
        assessment = None
        reconciliation = None
        try:
            assessment = _run_daily_assessment(self.conn)
        except Exception as e:
            log.error("Assessment error: %s", e)
        try:
            if cfg.get("linky_enabled"):
                reconciliation = _reconcile_yesterday(self.conn, cfg)
        except Exception as e:
            log.error("Reconciliation error: %s", e)

        return {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "outdoor": {
                "temp": current_outdoor_temp,
                "humidity": current_outdoor_humidity,
            },
            "best_heating_window": best_window,
            "occupancy": occupancy,
            "prediction_model": {
                "trusted": trust_predictions,
                "rmse": pred_accuracy["rmse"],
                "bias": pred_accuracy["bias"],
                "validated_count": pred_accuracy["count"],
                "correct_decisions": (
                    f"{pred_accuracy['correct_decisions']}/"
                    f"{pred_accuracy['total_decisions']}"
                ),
            },
            "zones": [self._decision_to_dict(d) for d in decisions],
            "assessment": assessment,
            "reconciliation": reconciliation,
        }

    def _decide_low_spread(
        self,
        decision: ZoneDecision,
        prediction: Optional[DpPrediction],
        trust_predictions: bool,
        dp_spread: float,
        indoor_temp: float,
        dewpoint: float,
        current_outdoor_temp: Optional[float],
        best_temp: Optional[float],
        best_window: Optional[dict],
        zone_runoff: ZoneRunoff,
        heating_stats: dict,
        api, dev_id: str, inst_id: str,
        heat_mode: int, setpoint: float,
        dry_run: bool,
    ):
        """Handle the case where DP spread is below HEAT_ON threshold."""
        if dp_spread <= DP_SPREAD_CRITICAL:
            # CRITICAL: always heat immediately — no deferral, no COP check
            decision.action = "heating_on"
            decision.prediction_decision = "heat_anyway"
            decision.reason = (
                f"⚠ CRITICAL DP spread {dp_spread}° (≤ {DP_SPREAD_CRITICAL}°). "
                f"Condensation imminent. Immediate override — no deferral. "
                f"Indoor {indoor_temp}°C, DP {dewpoint}°C."
            )
            heating_stats["totalOnMinutes"] = \
                heating_stats.get("totalOnMinutes", 0) + 5
            heating_stats["totalCycles"] = \
                heating_stats.get("totalCycles", 0) + 1
            if not dry_run:
                try:
                    api.set_zone(dev_id, installation_id=inst_id,
                                 power=True, mode=heat_mode,
                                 setpoint_air_heat={"celsius": setpoint})
                except Exception as e:
                    decision.reason += f" FAILED: {e}"
                    decision.success = False

        elif prediction and trust_predictions and prediction.confidence != "low":
            # We have a trusted prediction — make unified decision
            will_recover = prediction.predicted_dp_spread >= DP_SPREAD_HEAT_ON
            has_better_cop = prediction.cop_saving_pct >= 10

            if will_recover and prediction.natural_drying:
                # Natural drying will fix it — skip entirely
                decision.action = "skip_heating"
                decision.prediction_decision = "skip_heating"
                decision.energy_saved_pct = 100
                decision.reason = (
                    f"🔮 Skip: spread {dp_spread}° but model predicts "
                    f"{prediction.predicted_dp_spread}° in "
                    f"{PREDICTION_HORIZON_H}h. {prediction.reasoning}"
                )
            elif has_better_cop and not will_recover:
                # Need to heat, but defer to better COP window
                decision.action = "defer_heating"
                decision.prediction_decision = "defer_cop"
                decision.energy_saved_pct = prediction.cop_saving_pct
                decision.reason = (
                    f"📅 Defer: spread {dp_spread}° needs heating, "
                    f"but outdoor {current_outdoor_temp}°C → "
                    f"{prediction.best_cop_temp}°C at "
                    f"{prediction.best_cop_hour} "
                    f"({prediction.cop_saving_pct}% COP saving). "
                    f"{prediction.reasoning}"
                )
            elif will_recover and has_better_cop:
                # Both: natural drying AND better window — definitely skip
                decision.action = "skip_heating"
                decision.prediction_decision = "skip_heating"
                decision.energy_saved_pct = 100
                decision.reason = (
                    f"🔮📅 Skip: natural drying predicts "
                    f"{prediction.predicted_dp_spread}° in "
                    f"{PREDICTION_HORIZON_H}h, plus better COP window at "
                    f"{prediction.best_cop_hour}. {prediction.reasoning}"
                )
            else:
                # Model says it won't recover, no better window → heat now
                self._heat_now(
                    decision, dp_spread, indoor_temp, dewpoint,
                    prediction, heating_stats,
                    api, dev_id, inst_id, heat_mode, setpoint, dry_run,
                )

        else:
            # No trusted prediction — fallback to simple COP deferral
            outdoor = current_outdoor_temp or 0
            is_near_best = outdoor >= (best_temp or outdoor) - 2
            if (
                not is_near_best
                and best_temp is not None
                and best_temp > outdoor + 3
            ):
                decision.action = "defer_heating"
                decision.prediction_decision = "defer_cop" if prediction else None
                decision.energy_saved_pct = round((best_temp - outdoor) * 3)
                decision.reason = (
                    f"DP spread {dp_spread}°. No trusted prediction yet. "
                    f"Deferring: outdoor {outdoor}°C → forecast "
                    f"{best_temp}°C at {best_window.get('hour') if best_window else '?'}."
                )
            else:
                self._heat_now(
                    decision, dp_spread, indoor_temp, dewpoint,
                    prediction, heating_stats,
                    api, dev_id, inst_id, heat_mode, setpoint, dry_run,
                )

    def _heat_now(
        self,
        decision: ZoneDecision,
        dp_spread: float,
        indoor_temp: float,
        dewpoint: float,
        prediction: Optional[DpPrediction],
        heating_stats: dict,
        api, dev_id: str, inst_id: str,
        heat_mode: int, setpoint: float,
        dry_run: bool,
    ):
        """Turn heating on immediately."""
        decision.action = "heating_on"
        decision.prediction_decision = "heat_anyway"
        decision.reason = (
            f"DP spread {dp_spread}° < {DP_SPREAD_HEAT_ON}°. "
            f"Indoor {indoor_temp}°C, DP {dewpoint}°C."
        )
        if prediction:
            trust_str = "" if prediction.confidence != "low" else " (untrusted)"
            decision.reason += (
                f" Model{trust_str}: "
                f"{prediction.predicted_dp_spread}° in {PREDICTION_HORIZON_H}h."
            )
        decision.reason += " Heating on."
        heating_stats["totalOnMinutes"] = \
            heating_stats.get("totalOnMinutes", 0) + 5
        heating_stats["totalCycles"] = \
            heating_stats.get("totalCycles", 0) + 1
        if not dry_run:
            try:
                api.set_zone(dev_id, installation_id=inst_id,
                             power=True, mode=heat_mode,
                             setpoint_air_heat={"celsius": setpoint})
            except Exception as e:
                decision.reason += f" FAILED: {e}"
                decision.success = False

    def _log_decisions(self, decisions: list[ZoneDecision]):
        """Insert all zone decisions into control_log."""
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = []
        for d in decisions:
            rows.append((
                now, d.zone_name, d.action, d.humidity_airzone,
                d.humidity_netatmo, d.temperature, d.dewpoint, d.dp_spread,
                d.outdoor_temp, d.outdoor_humidity,
                d.forecast_temp_max, d.forecast_best_hour,
                1 if d.occupancy_detected else 0,
                d.energy_saved_pct, d.prediction_decision,
                d.reason, 1 if d.success else 0,
            ))
        self.conn.executemany(
            "INSERT INTO control_log "
            "(created_at, zone_name, action, humidity_airzone, "
            " humidity_netatmo, temperature, dewpoint, dp_spread, "
            " outdoor_temp, outdoor_humidity, forecast_temp_max, "
            " forecast_best_hour, occupancy_detected, energy_saved_pct, "
            " prediction_decision, reason, success) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    @staticmethod
    def _decision_to_dict(d: ZoneDecision) -> dict:
        """Convert a ZoneDecision to a JSON-serializable dict."""
        result = {
            "zone_name": d.zone_name,
            "action": d.action,
            "reason": d.reason,
            "success": d.success,
            "humidity_airzone": d.humidity_airzone,
            "humidity_netatmo": d.humidity_netatmo,
            "temperature": d.temperature,
            "dewpoint": d.dewpoint,
            "dp_spread": d.dp_spread,
            "outdoor_temp": d.outdoor_temp,
            "outdoor_humidity": d.outdoor_humidity,
            "forecast_temp_max": d.forecast_temp_max,
            "forecast_best_hour": d.forecast_best_hour,
            "occupancy_detected": d.occupancy_detected,
            "energy_saved_pct": d.energy_saved_pct,
            "prediction_decision": d.prediction_decision,
        }
        if d.prediction:
            result["prediction"] = {
                "predicted_dp_spread": d.prediction.predicted_dp_spread,
                "predicted_indoor_temp": d.prediction.predicted_indoor_temp,
                "confidence": d.prediction.confidence,
                "natural_drying": d.prediction.natural_drying,
                "runoff_boost": d.prediction.runoff_boost,
                "cop_saving_pct": d.prediction.cop_saving_pct,
                "reasoning": d.prediction.reasoning,
            }
        return result

    # ── Emergency Stop ─────────────────────────────────────────────────

    def set_emergency_stop(self, active: bool, reason: str = ""):
        """Toggle emergency stop (blocks all automatic heating)."""
        _set_state(self.conn, "emergency_stop", {
            "active": active,
            "reason": reason,
            "set_at": datetime.utcnow().isoformat() + "Z",
        })
        log.warning("Emergency stop %s: %s",
                     "ACTIVATED" if active else "deactivated", reason)

    def get_emergency_stop(self) -> dict:
        """Return current emergency stop state."""
        return _get_state(self.conn, "emergency_stop") or {"active": False}

    # ── Accessors ──────────────────────────────────────────────────────

    def get_learned_params(self) -> dict:
        """Return all learned parameters."""
        return _get_learned_params(self.conn)

    def get_prediction_accuracy(self) -> dict:
        """Return prediction model accuracy stats."""
        return _get_prediction_accuracy(self.conn)

    def get_recent_decisions(self, hours: int = 24,
                              zone_name: Optional[str] = None) -> list[dict]:
        """Return recent control decisions."""
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        if zone_name:
            rows = self.conn.execute(
                "SELECT * FROM control_log "
                "WHERE zone_name = ? AND created_at >= ? "
                "ORDER BY created_at DESC",
                (zone_name, cutoff),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM control_log "
                "WHERE created_at >= ? "
                "ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        cols = [desc[0] for desc in self.conn.execute(
            "SELECT * FROM control_log LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def get_daily_assessments(self, days: int = 30) -> list[dict]:
        """Return recent daily assessments."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM daily_assessment WHERE date >= ? ORDER BY date DESC",
            (cutoff,),
        ).fetchall()
        cols = [desc[0] for desc in self.conn.execute(
            "SELECT * FROM daily_assessment LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]


# ── Experiment Check ─────────────────────────────────────────────────────────

def _is_experiment_active(conn: sqlite3.Connection) -> bool:
    """Check if a heating experiment is currently active."""
    today = date.today().isoformat()
    # The heating_experiments table may not exist yet (Phase 3)
    try:
        row = conn.execute(
            "SELECT id FROM heating_experiments "
            "WHERE status = 'active' AND start_date <= ? AND end_date >= ? "
            "LIMIT 1",
            (today, today),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        # Table doesn't exist yet — no experiments
        return False


# ── Weather Info Builder ─────────────────────────────────────────────────────

def build_weather_info(forecast: dict) -> dict:
    """
    Build the weather_info dict expected by ControlBrain.run_cycle()
    from the existing airzone_weather module's compute_warm_window() result.

    This bridges the existing weather module with the new brain.

    forecast: dict from airzone_weather.get_forecast()
    """
    from airzone_weather import compute_warm_window

    warm_window = compute_warm_window(forecast)

    # Build 24h forecast for DP predictions
    times = forecast.get("hourly_times", [])
    temps = forecast.get("hourly_temps", [])
    humidities = forecast.get("hourly_rel_humidity", [])
    dew_points = forecast.get("hourly_dew_points", [])

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    from datetime import datetime as dt
    tz_paris = ZoneInfo("Europe/Paris")
    now = dt.now(tz_paris).replace(tzinfo=None)

    forecast_24h = []
    for i, t_str in enumerate(times):
        if i >= len(temps):
            break
        try:
            t = dt.fromisoformat(t_str)
        except (ValueError, TypeError):
            continue
        if t > now and len(forecast_24h) < 24:
            hum = humidities[i] if i < len(humidities) else 50
            forecast_24h.append({
                "time": t_str,
                "temp": temps[i],
                "humidity": hum,
            })

    # Best heating window (warmest hour in next 24h)
    best_window = None
    if forecast_24h:
        best = max(forecast_24h, key=lambda f: f["temp"])
        best_window = {"hour": best["time"], "temp": best["temp"]}

    return {
        "current_outdoor_temp": warm_window.get("current_outdoor_temp"),
        "current_outdoor_humidity": warm_window.get("current_outdoor_humidity"),
        "current_outdoor_dew_point": warm_window.get("current_outdoor_dew_point"),
        "is_warm_now": warm_window.get("is_warm_now", False),
        "forecast_24h": forecast_24h,
        "best_heating_window": best_window,
        # Pass through for backward compat with old controller
        **{k: v for k, v in warm_window.items()
           if k not in ("forecast_24h", "best_heating_window")},
    }

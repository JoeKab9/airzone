"""
Airzone Thermal Model Learning + DP Predictions
=================================================
Per-zone self-learning thermal models that predict DP spread 3h and 24h ahead.

Ported from Loveable TypeScript (supabase/functions/dp-predict/index.ts).

For each heating_off event, learns via linear regression:
 - Runoff duration as fn of heating duration
 - Peak temperature rise during runoff
 - Cooling rate as fn of indoor-outdoor ΔT
 - Humidity drift rate post-heating
 - RH→DP spread coupling coefficient
 - Confidence metric (asymptotic, half-life of 20 samples)

Prediction engine: current state + weather forecast → predicted DP spread
at +3h and +24h.

All data stored in local SQLite — no cloud dependency.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("airzone")

# ── Constants ────────────────────────────────────────────────────────────────

PREDICTION_HORIZONS = [3, 24]  # hours
MIN_SAMPLES = 3
CONFIDENCE_HALF_LIFE = 20  # asymptotic: confidence = 1 - e^(-samples/half_life)


# ── Helpers ──────────────────────────────────────────────────────────────────

def calc_dewpoint(temp_c: float, rh: float) -> float:
    """Magnus-Tetens dew point from temperature (°C) and RH (%)."""
    if rh <= 0:
        return 0.0
    a, b = 17.625, 243.04
    gamma = (a * temp_c) / (b + temp_c) + math.log(max(rh, 1) / 100)
    return (b * gamma) / (a - gamma)


def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Simple y = a + b*x.  Returns (a, b)."""
    n = len(xs)
    if n < 2:
        return (ys[0] if ys else 0, 0)
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_x2 = sum(x * x for x in xs)
    denom = n * sum_x2 - sum_x * sum_x
    if abs(denom) < 0.0001:
        return (sum_y / n, 0)
    b = (n * sum_xy - sum_x * sum_y) / denom
    a = (sum_y - b * sum_x) / n
    return (a, b)


# ── DB tables (prediction tracking) ─────────────────────────────────────────

def create_prediction_tables(conn: sqlite3.Connection):
    """Create tables for prediction tracking and coefficient learning."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS thermal_models (
            zone_name        TEXT PRIMARY KEY,
            samples          INTEGER DEFAULT 0,
            runoff_base      REAL DEFAULT 0,
            runoff_per_heat_min REAL DEFAULT 0,
            peak_per_heat_min REAL DEFAULT 0,
            peak_max_observed REAL DEFAULT 0,
            decay_coeff      REAL DEFAULT 0,
            rh_drift_coeff   REAL DEFAULT 0,
            rh_to_dp_coeff   REAL DEFAULT 0,
            confidence       REAL DEFAULT 0,
            data_days        REAL DEFAULT 0,
            last_updated     TEXT
        );

        CREATE TABLE IF NOT EXISTS dp_predict_coefficients (
            id                    INTEGER PRIMARY KEY CHECK (id = 1),
            outdoor_temp_weight   REAL DEFAULT 0,
            outdoor_hum_weight    REAL DEFAULT 0,
            time_decay_weight     REAL DEFAULT 0,
            base_drift            REAL DEFAULT 0,
            learning_count        INTEGER DEFAULT 0,
            last_avg_error        REAL,
            last_updated          TEXT
        );

        INSERT OR IGNORE INTO dp_predict_coefficients (id) VALUES (1);
    """)
    conn.commit()


# ── Thermal Model Learning ───────────────────────────────────────────────────

@dataclass
class ThermalObservation:
    """Single observation from a heating_off event."""
    heating_min: float
    runoff_h: float
    peak_rise: float
    indoor_outdoor_delta: float
    decay_rate_per_h: float
    rh_drift_per_h: float
    rh_to_dp_coeff: float


@dataclass
class ZoneThermalModel:
    """Learned thermal model for one zone."""
    samples: int = 0
    runoff_base: float = 0
    runoff_per_heat_min: float = 0
    peak_per_heat_min: float = 0
    peak_max_observed: float = 0
    decay_coeff: float = 0
    rh_drift_coeff: float = 0
    rh_to_dp_coeff: float = 0
    confidence: float = 0
    data_days: float = 0
    last_updated: str = ""


def learn_thermal_models(conn: sqlite3.Connection,
                         days: int = 90) -> dict[str, ZoneThermalModel]:
    """
    Learn thermal models from control_log data.

    Analyzes ALL heating_off events per zone to build regression models
    for runoff and decay behavior.
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"

    rows = conn.execute(
        "SELECT zone_name, created_at, action, temperature, "
        "       humidity_airzone, humidity_netatmo, outdoor_temp "
        "FROM control_log "
        "WHERE created_at >= ? "
        "ORDER BY created_at ASC",
        (cutoff,)
    ).fetchall()

    if len(rows) < 10:
        return {}

    # Group by zone
    by_zone: dict[str, list] = {}
    for row in rows:
        zone = row[0]
        by_zone.setdefault(zone, []).append({
            "zone_name": row[0],
            "created_at": row[1],
            "action": row[2],
            "temperature": row[3],
            "humidity_airzone": row[4],
            "humidity_netatmo": row[5],
            "outdoor_temp": row[6],
        })

    first_time = datetime.fromisoformat(rows[0][1].replace("Z", "+00:00"))
    last_time = datetime.fromisoformat(rows[-1][1].replace("Z", "+00:00"))
    data_days = (last_time - first_time).total_seconds() / 86400

    models: dict[str, ZoneThermalModel] = {}

    for zone, zone_logs in by_zone.items():
        observations: list[ThermalObservation] = []

        for i, log_entry in enumerate(zone_logs):
            if log_entry["action"] != "heating_off":
                continue

            off_temp = log_entry["temperature"]
            off_time_str = log_entry["created_at"]
            outdoor_temp = log_entry["outdoor_temp"]
            off_hum = max(log_entry["humidity_airzone"] or 0,
                          log_entry["humidity_netatmo"] or 0)

            if off_temp is None or outdoor_temp is None:
                continue

            off_time = datetime.fromisoformat(
                off_time_str.replace("Z", "+00:00"))

            # Find heating duration
            heating_dur_min = 0
            for k in range(i - 1, max(0, i - 200) - 1, -1):
                if zone_logs[k]["action"] == "heating_on":
                    on_time = datetime.fromisoformat(
                        zone_logs[k]["created_at"].replace("Z", "+00:00"))
                    heating_dur_min = (off_time - on_time).total_seconds() / 60
                    break
                if zone_logs[k]["action"] == "heating_off":
                    break

            if heating_dur_min < 3:
                continue

            # Collect post-heating readings
            post_readings = []
            for j in range(i + 1, len(zone_logs)):
                t_str = zone_logs[j]["created_at"]
                t = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                if (t - off_time).total_seconds() > 8 * 3600:
                    break
                if zone_logs[j]["action"] == "heating_on":
                    break
                if zone_logs[j]["temperature"] is None:
                    continue
                h = max(zone_logs[j]["humidity_airzone"] or 0,
                        zone_logs[j]["humidity_netatmo"] or 0)
                post_readings.append({
                    "t": t,
                    "temp": zone_logs[j]["temperature"],
                    "hum": h,
                })

            if len(post_readings) < 4:
                continue

            # Smooth temps (3-point moving average)
            smoothed = []
            for s in range(1, len(post_readings) - 1):
                smoothed.append({
                    "t": post_readings[s]["t"],
                    "temp": (post_readings[s - 1]["temp"] +
                             post_readings[s]["temp"] +
                             post_readings[s + 1]["temp"]) / 3,
                    "hum": (post_readings[s - 1]["hum"] +
                            post_readings[s]["hum"] +
                            post_readings[s + 1]["hum"]) / 3,
                })
            if len(smoothed) < 3:
                continue

            # Find peak temperature (end of runoff)
            peak_temp = smoothed[0]["temp"]
            peak_idx = 0
            for s in range(1, len(smoothed)):
                if smoothed[s]["temp"] >= peak_temp - 0.05:
                    if smoothed[s]["temp"] > peak_temp:
                        peak_temp = smoothed[s]["temp"]
                        peak_idx = s
                else:
                    # Check sustained decline
                    sustained = True
                    for look in range(1, min(3, len(smoothed) - s)):
                        if smoothed[s + look]["temp"] >= smoothed[s]["temp"]:
                            sustained = False
                            break
                    if sustained:
                        break
                    if smoothed[s]["temp"] > peak_temp:
                        peak_temp = smoothed[s]["temp"]
                        peak_idx = s

            runoff_h = (smoothed[peak_idx]["t"] - off_time).total_seconds() / 3600
            peak_rise = max(0, peak_temp - off_temp)

            # Decay rate after peak
            after_peak = smoothed[peak_idx + 1:]
            decay_rate_per_h = 0
            if len(after_peak) >= 2:
                last_r = after_peak[-1]
                decay_h = (last_r["t"] - smoothed[peak_idx]["t"]).total_seconds() / 3600
                decay_c = peak_temp - last_r["temp"]
                delta = peak_temp - outdoor_temp
                if decay_h > 0.3 and delta > 0.5:
                    decay_rate_per_h = decay_c / decay_h / delta

            # Humidity drift + DP spread response
            rh_drift_per_h = 0
            rh_to_dp = 0
            if len(post_readings) >= 3 and off_hum > 0:
                last_hum = post_readings[-1]["hum"]
                time_span_h = ((post_readings[-1]["t"] - off_time)
                               .total_seconds() / 3600)
                if time_span_h > 0.5 and last_hum > 0:
                    rh_drift_per_h = (last_hum - off_hum) / time_span_h
                    off_dp = calc_dewpoint(off_temp, off_hum)
                    last_temp = post_readings[-1]["temp"]
                    last_dp = calc_dewpoint(last_temp, last_hum)
                    dp_spread_change = (last_temp - last_dp) - (off_temp - off_dp)
                    rh_change = last_hum - off_hum
                    if abs(rh_change) > 1:
                        rh_to_dp = dp_spread_change / rh_change

            if runoff_h >= 0.05:
                observations.append(ThermalObservation(
                    heating_min=heating_dur_min,
                    runoff_h=runoff_h,
                    peak_rise=peak_rise,
                    indoor_outdoor_delta=off_temp - outdoor_temp,
                    decay_rate_per_h=decay_rate_per_h,
                    rh_drift_per_h=rh_drift_per_h,
                    rh_to_dp_coeff=rh_to_dp,
                ))

        if not observations:
            continue

        # Linear regression: runoff_hours = a + b * heating_minutes
        runoff_base, runoff_per_min = _linear_regression(
            [o.heating_min for o in observations],
            [o.runoff_h for o in observations],
        )

        # Peak rise per heating minute
        avg_peak_per_min = sum(
            o.peak_rise / o.heating_min
            for o in observations if o.heating_min > 0
        ) / len(observations)

        # Average decay coefficient
        decay_obs = [o for o in observations if o.decay_rate_per_h > 0]
        avg_decay = (sum(o.decay_rate_per_h for o in decay_obs) / len(decay_obs)
                     if decay_obs else 0)

        # Average RH drift
        rh_obs = [o for o in observations if o.rh_drift_per_h != 0]
        avg_rh_drift = (sum(o.rh_drift_per_h for o in rh_obs) / len(rh_obs)
                        if rh_obs else 0)

        # RH-to-DP coefficient
        dp_obs = [o for o in observations if o.rh_to_dp_coeff != 0]
        avg_rh_to_dp = (sum(o.rh_to_dp_coeff for o in dp_obs) / len(dp_obs)
                        if dp_obs else 0)

        # Asymptotic confidence
        confidence = 1 - math.exp(-len(observations) / CONFIDENCE_HALF_LIFE)

        models[zone] = ZoneThermalModel(
            samples=len(observations),
            runoff_base=round(max(0, runoff_base), 3),
            runoff_per_heat_min=round(max(0, runoff_per_min), 4),
            peak_per_heat_min=round(avg_peak_per_min, 4),
            peak_max_observed=round(max(o.peak_rise for o in observations), 2),
            decay_coeff=round(avg_decay, 5),
            rh_drift_coeff=round(avg_rh_drift, 3),
            rh_to_dp_coeff=round(avg_rh_to_dp, 4),
            confidence=round(confidence, 2),
            data_days=round(data_days, 1),
            last_updated=datetime.utcnow().isoformat() + "Z",
        )

    return models


def persist_thermal_models(conn: sqlite3.Connection,
                           models: dict[str, ZoneThermalModel]):
    """Store learned thermal models in the database."""
    create_prediction_tables(conn)
    for zone, m in models.items():
        conn.execute(
            "INSERT OR REPLACE INTO thermal_models "
            "(zone_name, samples, runoff_base, runoff_per_heat_min, "
            " peak_per_heat_min, peak_max_observed, decay_coeff, "
            " rh_drift_coeff, rh_to_dp_coeff, confidence, data_days, "
            " last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (zone, m.samples, m.runoff_base, m.runoff_per_heat_min,
             m.peak_per_heat_min, m.peak_max_observed, m.decay_coeff,
             m.rh_drift_coeff, m.rh_to_dp_coeff, m.confidence,
             m.data_days, m.last_updated),
        )
    conn.commit()


def load_thermal_models(conn: sqlite3.Connection) -> dict[str, ZoneThermalModel]:
    """Load persisted thermal models."""
    create_prediction_tables(conn)
    rows = conn.execute("SELECT * FROM thermal_models").fetchall()
    models = {}
    for r in rows:
        models[r[0]] = ZoneThermalModel(
            samples=r[1], runoff_base=r[2], runoff_per_heat_min=r[3],
            peak_per_heat_min=r[4], peak_max_observed=r[5],
            decay_coeff=r[6], rh_drift_coeff=r[7], rh_to_dp_coeff=r[8],
            confidence=r[9], data_days=r[10], last_updated=r[11] or "",
        )
    return models


# ── DP Spread Prediction Engine ──────────────────────────────────────────────

@dataclass
class DpPrediction:
    """Prediction result for one zone at one horizon."""
    current_dp_spread: float
    predicted_dp_spread: float
    trend: int  # -2..+2
    confidence: float
    factors: list[str] = field(default_factory=list)
    is_learning: bool = False


def _get_last_heating_off(zone_logs: list[dict]) -> Optional[dict]:
    """Find last heating_off event with valid heating duration."""
    for i in range(len(zone_logs) - 1, -1, -1):
        if zone_logs[i]["action"] == "heating_off":
            off_time = datetime.fromisoformat(
                zone_logs[i]["created_at"].replace("Z", "+00:00"))
            # Find heating duration
            dur_min = 0
            for k in range(i - 1, max(0, i - 200) - 1, -1):
                if zone_logs[k]["action"] == "heating_on":
                    on_time = datetime.fromisoformat(
                        zone_logs[k]["created_at"].replace("Z", "+00:00"))
                    dur_min = (off_time - on_time).total_seconds() / 60
                    break
                if zone_logs[k]["action"] == "heating_off":
                    break
            if dur_min > 0:
                return {"time": off_time.timestamp() * 1000,
                        "duration_min": dur_min}
            break
    return None


def _get_outdoor_coefficients(conn: sqlite3.Connection) -> dict:
    """Load outdoor prediction coefficients."""
    create_prediction_tables(conn)
    row = conn.execute(
        "SELECT outdoor_temp_weight, outdoor_hum_weight, time_decay_weight, "
        "       base_drift, learning_count "
        "FROM dp_predict_coefficients WHERE id = 1"
    ).fetchone()
    if not row:
        return {"outdoor_temp_weight": 0, "outdoor_hum_weight": 0,
                "time_decay_weight": 0, "base_drift": 0, "learning_count": 0}
    return {
        "outdoor_temp_weight": row[0] or 0,
        "outdoor_hum_weight": row[1] or 0,
        "time_decay_weight": row[2] or 0,
        "base_drift": row[3] or 0,
        "learning_count": row[4] or 0,
    }


def predict_dp_spread(
        conn: sqlite3.Connection,
        models: dict[str, ZoneThermalModel],
        weather_forecast: Optional[dict] = None,
) -> dict:
    """
    Compute DP spread predictions for all zones at 3h and 24h horizons.

    weather_forecast should contain:
        current_temp, current_hum, temp_3h, hum_3h, temp_24h, hum_24h

    Returns dict with 'predictions_3h' and 'predictions_24h' per zone.
    """
    create_prediction_tables(conn)
    now_ms = time.time() * 1000

    # Get recent control_log entries (last 3h for current state)
    since_3h = (datetime.utcnow() - timedelta(hours=3)).isoformat() + "Z"
    since_90d = (datetime.utcnow() - timedelta(days=90)).isoformat() + "Z"

    recent_rows = conn.execute(
        "SELECT zone_name, created_at, action, temperature, "
        "       humidity_airzone, humidity_netatmo, outdoor_temp, "
        "       outdoor_humidity, dp_spread "
        "FROM control_log WHERE created_at >= ? ORDER BY created_at",
        (since_3h,)
    ).fetchall()

    if not recent_rows:
        return {"predictions_3h": {}, "predictions_24h": {}}

    # Latest reading per zone
    latest_by_zone: dict[str, dict] = {}
    for row in reversed(recent_rows):
        zone = row[0]
        if zone not in latest_by_zone:
            latest_by_zone[zone] = {
                "zone_name": row[0], "created_at": row[1], "action": row[2],
                "temperature": row[3], "humidity_airzone": row[4],
                "humidity_netatmo": row[5], "outdoor_temp": row[6],
                "outdoor_humidity": row[7], "dp_spread": row[8],
            }

    # All logs by zone for heating_off search
    all_rows = conn.execute(
        "SELECT zone_name, created_at, action FROM control_log "
        "WHERE created_at >= ? ORDER BY created_at",
        (since_90d,)
    ).fetchall()

    logs_by_zone: dict[str, list] = {}
    for r in all_rows:
        logs_by_zone.setdefault(r[0], []).append({
            "zone_name": r[0], "created_at": r[1], "action": r[2],
        })

    coefficients = _get_outdoor_coefficients(conn)

    predictions_3h: dict[str, dict] = {}
    predictions_24h: dict[str, dict] = {}

    for zone, log_entry in latest_by_zone.items():
        az_temp = log_entry["temperature"]
        if az_temp is None:
            continue

        # Best humidity
        nt_hum = log_entry["humidity_netatmo"] or 0
        az_hum = log_entry["humidity_airzone"] or 0
        best_hum = nt_hum if nt_hum > 0 else az_hum
        if best_hum <= 0:
            continue

        dp = calc_dewpoint(az_temp, best_hum)
        current_dp_spread = log_entry["dp_spread"] or (az_temp - dp)

        is_heating = log_entry["action"] == "heating_on"
        model = models.get(zone)
        has_model = model is not None and model.samples >= MIN_SAMPLES

        outdoor_temp_now = log_entry["outdoor_temp"]
        outdoor_hum_now = log_entry["outdoor_humidity"]
        if outdoor_temp_now is None and weather_forecast:
            outdoor_temp_now = weather_forecast.get("current_temp")
        if outdoor_hum_now is None and weather_forecast:
            outdoor_hum_now = weather_forecast.get("current_hum")

        # Last heating off for this zone
        zone_logs = logs_by_zone.get(zone, [])
        last_off = _get_last_heating_off(zone_logs)

        for hours_ahead in PREDICTION_HORIZONS:
            outdoor_temp_fut = None
            outdoor_hum_fut = None
            if weather_forecast:
                if hours_ahead <= 3:
                    outdoor_temp_fut = weather_forecast.get("temp_3h", outdoor_temp_now)
                    outdoor_hum_fut = weather_forecast.get("hum_3h", outdoor_hum_now)
                else:
                    outdoor_temp_fut = weather_forecast.get("temp_24h", outdoor_temp_now)
                    outdoor_hum_fut = weather_forecast.get("hum_24h", outdoor_hum_now)

            factors: list[str] = []
            predicted_change = 0.0

            # A) Outdoor forecast trend (learned weights)
            if (outdoor_temp_now is not None and outdoor_temp_fut is not None
                    and coefficients["learning_count"] > 0):
                temp_change = outdoor_temp_fut - outdoor_temp_now
                predicted_change += temp_change * coefficients["outdoor_temp_weight"]
                if abs(temp_change) > 1:
                    factors.append("Outdoor warming" if temp_change > 0
                                   else "Outdoor cooling")

            if (outdoor_hum_now is not None and outdoor_hum_fut is not None
                    and coefficients["learning_count"] > 0):
                hum_change = outdoor_hum_fut - outdoor_hum_now
                predicted_change += hum_change * coefficients["outdoor_hum_weight"]
                if abs(hum_change) > 5:
                    factors.append("Rising outdoor RH" if hum_change > 0
                                   else "Falling outdoor RH")

            # B) Thermal model (only if learned)
            if has_model:
                if not is_heating and last_off:
                    hours_since_off = (now_ms - last_off["time"]) / 3600_000
                    expected_runoff = (model.runoff_base +
                                       model.runoff_per_heat_min * last_off["duration_min"])

                    if hours_since_off < expected_runoff:
                        remain_h = expected_runoff - hours_since_off
                        peak_rise = min(
                            model.peak_per_heat_min * last_off["duration_min"],
                            model.peak_max_observed,
                        )
                        runoff_in_window = min(remain_h, hours_ahead)
                        runoff_effect = peak_rise * (runoff_in_window / expected_runoff)
                        predicted_change += runoff_effect
                        factors.append(
                            f"Learned runoff ({remain_h:.1f}h left, "
                            f"{model.samples} obs.)")

                        # After runoff ends, apply decay
                        if (hours_ahead > remain_h and outdoor_temp_now is not None
                                and model.decay_coeff > 0):
                            decay_window = hours_ahead - remain_h
                            delta = (az_temp + runoff_effect) - outdoor_temp_now
                            decay_per_h = model.decay_coeff * delta
                            predicted_change -= decay_per_h * decay_window
                            if decay_per_h * decay_window > 0.2:
                                factors.append(
                                    f"Post-runoff decay ({decay_window:.1f}h)")

                    elif outdoor_temp_now is not None:
                        delta = az_temp - outdoor_temp_now
                        decay_per_h = model.decay_coeff * delta
                        post_runoff_h = hours_since_off - expected_runoff
                        window_remaining = max(0, hours_ahead - post_runoff_h)
                        predicted_change -= decay_per_h * window_remaining
                        if decay_per_h * window_remaining > 0.2:
                            factors.append(f"Learned decay ({model.samples} obs.)")

                    # RH drift effect on DP spread
                    if model.rh_drift_coeff != 0 and model.rh_to_dp_coeff != 0:
                        rh_effect = (model.rh_drift_coeff * hours_ahead
                                     * model.rh_to_dp_coeff)
                        predicted_change += rh_effect

                elif not is_heating and outdoor_temp_now is not None:
                    delta = az_temp - outdoor_temp_now
                    decay_per_h = model.decay_coeff * delta
                    predicted_change -= decay_per_h * hours_ahead
                    if decay_per_h * hours_ahead > 0.2:
                        factors.append(f"Learned cooling ({model.samples} obs.)")
            else:
                factors.append(
                    f"Learning ({model.samples if model else 0}"
                    f"/{MIN_SAMPLES} cycles observed)")

            # C) Time-of-day (learned)
            hour = datetime.now().hour
            if (hour >= 18 or hour < 6) and coefficients["learning_count"] > 0:
                night_effect = coefficients["time_decay_weight"] * (hours_ahead / 3)
                predicted_change += night_effect
                if abs(night_effect) > 0.05:
                    factors.append("Learned night effect")

            if not factors:
                factors.append("Insufficient data")

            predicted_dp_spread = round(current_dp_spread + predicted_change, 1)

            # Confidence scoring
            confidence = 0.0
            if has_model:
                confidence += ((1 - math.exp(-model.samples / CONFIDENCE_HALF_LIFE))
                               * 0.5)
            if coefficients["learning_count"] > 10:
                confidence += 0.2
            if weather_forecast:
                confidence += 0.15

            # Historical trend check
            zone_log_count = len([l for l in zone_logs
                                  if l.get("zone_name") == zone])
            if zone_log_count >= 20:
                confidence += 0.15

            if hours_ahead > 12:
                confidence *= 0.7
            confidence = min(confidence, 0.99)

            # Trend classification
            if predicted_change > 1.5:
                trend = 2
            elif predicted_change > 0.4:
                trend = 1
            elif predicted_change > -0.4:
                trend = 0
            elif predicted_change > -1.5:
                trend = -1
            else:
                trend = -2

            pred = {
                "current_dp_spread": round(current_dp_spread, 1),
                "predicted_dp_spread": predicted_dp_spread,
                "trend": trend,
                "confidence": round(confidence, 2),
                "factors": factors,
                "is_learning": not has_model,
            }

            if hours_ahead == 3:
                predictions_3h[zone] = pred
            else:
                predictions_24h[zone] = pred

        # Store 3h prediction for future validation
        if not is_heating and zone in predictions_3h:
            pred3h = predictions_3h[zone]
            try:
                predicted_for = (datetime.utcnow() + timedelta(hours=3)
                                 ).isoformat() + "Z"
                conn.execute(
                    "INSERT INTO dp_spread_predictions "
                    "(zone_name, predicted_for, hours_ahead, predicted_dp_spread, "
                    " current_dp_spread, current_indoor_temp, current_outdoor_temp, "
                    " decision_made) "
                    "VALUES (?, ?, 3, ?, ?, ?, ?, ?)",
                    (zone, predicted_for, pred3h["predicted_dp_spread"],
                     pred3h["current_dp_spread"], az_temp, outdoor_temp_now,
                     "model_prediction" if has_model else "learning"),
                )
                conn.commit()
            except Exception:
                pass

    return {
        "predictions_3h": predictions_3h,
        "predictions_24h": predictions_24h,
        "thermal_models": {
            z: {"samples": m.samples, "confidence": m.confidence,
                "decay_coeff": m.decay_coeff, "data_days": m.data_days}
            for z, m in models.items()
        },
    }


# ── Prediction Validation (Learning Feedback Loop) ──────────────────────────

def validate_past_predictions(conn: sqlite3.Connection):
    """
    Check predictions that have passed their target time and compare
    with actual readings.  Updates prediction_error and decision_correct.
    """
    now = datetime.utcnow().isoformat() + "Z"

    try:
        unvalidated = conn.execute(
            "SELECT id, zone_name, predicted_for, predicted_dp_spread, "
            "       current_dp_spread "
            "FROM dp_spread_predictions "
            "WHERE validated = 0 AND predicted_for < ? "
            "ORDER BY created_at LIMIT 50",
            (now,)
        ).fetchall()
    except Exception:
        return  # Table may not have validated column yet

    if not unvalidated:
        return

    for pred in unvalidated:
        pred_id, zone_name, predicted_for, pred_spread, current_spread = pred

        target_time = datetime.fromisoformat(predicted_for.replace("Z", "+00:00"))
        window_start = (target_time - timedelta(minutes=30)).isoformat() + "Z"
        window_end = (target_time + timedelta(minutes=30)).isoformat() + "Z"

        actuals = conn.execute(
            "SELECT dp_spread, temperature, action "
            "FROM control_log "
            "WHERE zone_name = ? AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (zone_name, window_start, window_end)
        ).fetchall()

        if not actuals:
            # If > 6h past target time, mark as validated without result
            elapsed = (datetime.utcnow() - target_time).total_seconds()
            if elapsed > 6 * 3600:
                conn.execute(
                    "UPDATE dp_spread_predictions SET validated = 1, "
                    "validated_at = ? WHERE id = ?",
                    (now, pred_id))
            continue

        actual = actuals[0]
        if actual[2] == "heating_on":
            conn.execute(
                "UPDATE dp_spread_predictions SET validated = 1, "
                "validated_at = ?, decision_made = 'heating_active_at_validation' "
                "WHERE id = ?",
                (now, pred_id))
            continue

        actual_spread = actual[0]
        if actual_spread is None:
            continue

        error = pred_spread - actual_spread
        decision_correct = None
        if current_spread is not None:
            decision_correct = ((pred_spread >= current_spread)
                                == (actual_spread >= current_spread))

        conn.execute(
            "UPDATE dp_spread_predictions SET "
            "validated = 1, validated_at = ?, "
            "actual_dp_spread = ?, actual_indoor_temp = ?, "
            "prediction_error = ?, decision_correct = ? "
            "WHERE id = ?",
            (now, actual_spread, actual[1], round(error, 2),
             1 if decision_correct else 0 if decision_correct is not None else None,
             pred_id))

    conn.commit()


def update_prediction_coefficients(conn: sqlite3.Connection):
    """
    Update outdoor prediction coefficients via gradient descent
    on validated predictions.
    """
    create_prediction_tables(conn)
    coefficients = _get_outdoor_coefficients(conn)

    validated = conn.execute(
        "SELECT predicted_dp_spread, current_dp_spread, prediction_error, "
        "       current_outdoor_temp "
        "FROM dp_spread_predictions "
        "WHERE validated = 1 AND prediction_error IS NOT NULL "
        "  AND decision_made != 'heating_active_at_validation' "
        "ORDER BY validated_at DESC LIMIT 100"
    ).fetchall()

    if len(validated) < 10:
        return

    avg_error = sum(r[2] for r in validated) / len(validated)
    learning_rate = 0.01

    temp_corr = 0
    count = 0
    for r in validated:
        if r[3] is not None and r[2] is not None:
            temp_corr += r[2]
            count += 1
    if count > 0:
        temp_corr /= count

    conn.execute(
        "UPDATE dp_predict_coefficients SET "
        "outdoor_temp_weight = ?, outdoor_hum_weight = ?, "
        "time_decay_weight = ?, base_drift = ?, "
        "learning_count = ?, last_avg_error = ?, last_updated = ? "
        "WHERE id = 1",
        (
            coefficients["outdoor_temp_weight"] - learning_rate * temp_corr,
            coefficients["outdoor_hum_weight"] - learning_rate * avg_error,
            coefficients["time_decay_weight"] - learning_rate * avg_error,
            coefficients["base_drift"] - learning_rate * avg_error,
            coefficients["learning_count"] + len(validated),
            round(avg_error, 2),
            datetime.utcnow().isoformat() + "Z",
        ))
    conn.commit()


# ── Full Prediction Cycle ────────────────────────────────────────────────────

def run_prediction_cycle(conn: sqlite3.Connection,
                         weather_forecast: Optional[dict] = None) -> dict:
    """
    Full prediction cycle:
    1. Validate past predictions
    2. Learn thermal models from all data
    3. Compute new predictions
    4. Update coefficients
    """
    # 1. Validate
    validate_past_predictions(conn)

    # 2. Learn
    models = learn_thermal_models(conn, days=90)
    if models:
        persist_thermal_models(conn, models)
    else:
        models = load_thermal_models(conn)

    # 3. Predict
    result = predict_dp_spread(conn, models, weather_forecast)

    # 4. Update coefficients
    update_prediction_coefficients(conn)

    return result

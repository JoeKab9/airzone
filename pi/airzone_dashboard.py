#!/usr/bin/env python3
"""
Airzone Web Dashboard
======================
Lightweight Flask app that serves historical temperature/humidity graphs.
Reads from the same SQLite database that the Pi daemon writes to.

Usage:
    python3 airzone_dashboard.py                    # http://0.0.0.0:5000
    python3 airzone_dashboard.py --port 8080        # custom port
    gunicorn -w 1 -b 0.0.0.0:5000 airzone_dashboard:app   # production
"""

import argparse
import json
import logging
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("airzone.dashboard")

# Make src/ importable for analytics module (used by HistoryDB)
_SRC_DIR = Path(__file__).parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

try:
    from flask import Flask, jsonify, render_template, request
except ImportError:
    print("Missing dependency:  pip install flask")
    import sys
    sys.exit(1)

from airzone_db import HistoryDB

# ── Flask app ────────────────────────────────────────────────────────────────

app = Flask(__name__,
            template_folder=str(Path(__file__).parent / "templates"),
            static_folder=str(Path(__file__).parent / "static"))

DB_PATH = Path(__file__).parent / "data" / "airzone_history.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


_shared_db = None
_shared_db_lock = threading.Lock()


def get_db() -> HistoryDB:
    """Return a fresh DB connection per call (avoids cross-thread corruption)."""
    return HistoryDB(DB_PATH)


# ── Background poller ────────────────────────────────────────────────────────
# Fetches live data from the Airzone Cloud API and writes it to the DB.
# This replaces the need for a separate daemon process.

_poller_started = False
POLL_INTERVAL = 300  # 5 minutes


def _background_poller():
    """Background thread: poll Airzone Cloud API → write to SQLite DB."""
    from airzone_humidity_controller import AirzoneCloudAPI, load_config
    from airzone_secrets import secrets

    # Load credentials
    email = secrets.get("email")
    password = secrets.get("password")
    if not email or not password:
        log.warning("No credentials in .env — background poller disabled. "
                    "Add AIRZONE_EMAIL and AIRZONE_PASSWORD to .env")
        return

    api = AirzoneCloudAPI()

    # Try to reuse cached token
    if not api.load_cached_tokens():
        try:
            api.login(email, password)
        except Exception as e:
            log.error("Login failed: %s — poller stopping", e)
            return

    log.info("Background poller started (every %ds)", POLL_INTERVAL)

    # Track Linky state across poll cycles
    linky_state = {"last_fetch": "", "backfill_done": False, "last_attempt_hour": -1}

    while True:
        try:
            api.ensure_token(email, password)
            zones = api.get_all_zones()
            if zones:
                # Fetch weather for outdoor data
                outdoor_temp = None
                outdoor_dew_point = None
                outdoor_humidity = None
                outdoor_wind_speed = None
                outdoor_wind_dir = None
                outdoor_rain = None
                outdoor_solar = None
                try:
                    from airzone_weather import get_forecast, compute_warm_window
                    cfg = _load_dashboard_config()
                    forecast = get_forecast(
                        cfg.get("latitude", 44.07),
                        cfg.get("longitude", -1.26))
                    if forecast:
                        weather_info = compute_warm_window(
                            forecast, cfg.get("warm_hours_count", 6))
                        outdoor_temp = weather_info.get("current_outdoor_temp")
                        outdoor_dew_point = weather_info.get("current_outdoor_dew_point")
                        outdoor_humidity = weather_info.get("current_outdoor_humidity")
                        outdoor_wind_speed = weather_info.get("current_outdoor_wind_speed")
                        outdoor_wind_dir = weather_info.get("current_outdoor_wind_dir")
                        outdoor_rain = weather_info.get("current_outdoor_rain")
                        outdoor_solar = weather_info.get("current_outdoor_solar")
                except Exception:
                    pass

                db = get_db()
                db.log_readings(zones, outdoor_temp,
                                outdoor_dew_point=outdoor_dew_point,
                                outdoor_humidity=outdoor_humidity,
                                outdoor_wind_speed=outdoor_wind_speed,
                                outdoor_wind_dir=outdoor_wind_dir,
                                outdoor_rain=outdoor_rain,
                                outdoor_solar=outdoor_solar)
                log.info("Polled %d zones from Airzone Cloud", len(zones))
            else:
                log.warning("No zones returned from API")
        except Exception as e:
            log.error("Poll failed: %s", e)

        # ── Linky energy data ────────────────────────────────────────
        try:
            linky_token = secrets.get("linky_token")
            linky_prm = secrets.get("linky_prm")
            if linky_token and linky_prm:
                from datetime import date, timedelta as td
                from airzone_linky import fetch_load_curve, run_energy_analysis

                today_str = str(date.today())
                now_hour = datetime.now().hour

                # Backfill removed — only fetch missing daily data
                linky_state["backfill_done"] = True

                # Daily fetch: retry hourly from 10 AM until success
                if (linky_state["last_fetch"] != today_str
                        and now_hour >= 10
                        and now_hour != linky_state["last_attempt_hour"]):
                    linky_state["last_attempt_hour"] = now_hour
                    yesterday = date.today() - td(days=1)
                    readings = fetch_load_curve(
                        linky_token, linky_prm, yesterday,
                        yesterday + td(days=1))
                    if readings:
                        db = get_db()
                        db.log_linky_readings(readings)
                        log.info("Linky: stored %d readings for %s",
                                 len(readings), yesterday)
                        linky_state["last_fetch"] = today_str
                        # Update energy analysis with new data
                        try:
                            run_energy_analysis(db.conn, days=7)
                        except Exception as ea:
                            log.error("Energy analysis error: %s", ea)
                    else:
                        log.info("Linky: no data yet for %s, will retry",
                                 yesterday)
        except Exception as e:
            log.error("Linky fetch error: %s", e)

        time.sleep(POLL_INTERVAL)


def start_poller():
    """Start the background poller thread (once)."""
    global _poller_started
    if _poller_started:
        return
    _poller_started = True
    t = threading.Thread(target=_background_poller, daemon=True)
    t.start()


# ── HTML route ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the dashboard page."""
    return render_template("dashboard.html")


# ── API routes ───────────────────────────────────────────────────────────────

@app.route("/api/zones")
def api_zones():
    """Return list of zone names."""
    db = get_db()
    zones = db.get_zone_names()
    return jsonify(zones)


@app.route("/api/latest")
def api_latest():
    """Return the most recent reading for each zone."""
    db = get_db()
    latest = db.get_latest()
    return jsonify(latest)


@app.route("/api/readings")
def api_readings():
    """Return time-series readings. Query params: zone (optional), hours (default 168)."""
    zone = request.args.get("zone")
    hours = request.args.get("hours", 168, type=int)
    hours = min(hours, 8760)  # cap at 1 year

    db = get_db()
    readings = db.get_readings(zone_name=zone, hours=hours)
    return jsonify(readings)


@app.route("/api/actions")
def api_actions():
    """Return recent control actions. Query param: hours (default 168)."""
    hours = request.args.get("hours", 168, type=int)
    hours = min(hours, 8760)

    db = get_db()
    actions = db.get_actions(hours=hours)
    return jsonify(actions)


@app.route("/api/stats")
def api_stats():
    """Return database statistics."""
    db = get_db()
    stats = db.get_stats()
    return jsonify(stats)


# ── Analytics API routes ────────────────────────────────────────────────────

PI_DIR = Path(__file__).parent
PI_CONFIG_PATH = PI_DIR / "airzone_pi_config.json"
PARENT_DIR = PI_DIR.parent


def _load_dashboard_config() -> dict:
    """Load config for analytics queries."""
    if PI_CONFIG_PATH.exists():
        with open(PI_CONFIG_PATH) as f:
            return json.load(f)
    parent_cfg = PARENT_DIR / "airzone_config.json"
    if parent_cfg.exists():
        with open(parent_cfg) as f:
            return json.load(f)
    return {"warm_hours_count": 6}


@app.route("/api/analytics/profiles")
def api_zone_profiles():
    """Return zone analytics profiles."""
    db = get_db()
    profiles = db.get_zone_profiles()
    return jsonify(profiles)


@app.route("/api/analytics/cycles")
def api_heating_cycles():
    """Return recent heating cycles. Query params: zone (optional), hours (default 168)."""
    zone = request.args.get("zone")
    hours = request.args.get("hours", 168, type=int)
    hours = min(hours, 8760)
    db = get_db()
    cycles = db.get_heating_cycles(hours=hours, zone_name=zone)
    return jsonify(cycles)


@app.route("/api/analytics/warm-hours")
def api_warm_hours():
    """Return warm hours recommendation."""
    cfg = _load_dashboard_config()
    db = get_db()
    rec = db.get_warm_hours_recommendation(cfg)
    return jsonify(rec)


@app.route("/api/analytics/runoff")
def api_runoff():
    """Return smart early-off adjustment for each zone."""
    from airzone_analytics import get_smart_early_off_adjustment
    cfg = _load_dashboard_config()
    db = get_db()
    zones = db.get_zone_names()
    result = {}
    for zone in zones:
        adj = get_smart_early_off_adjustment(
            db.conn, zone,
            max_adjustment=cfg.get("smart_early_off_max", 5.0),
            min_cycles=cfg.get("smart_early_off_min_cycles", 5))
        if adj["data_points"] > 0:
            result[zone] = adj
    return jsonify(result)


@app.route("/api/analytics/optimization-log")
def api_optimization_log():
    """Return optimization log entries."""
    db = get_db()
    log_entries = db.get_optimization_log(limit=20)
    return jsonify(log_entries)


# ── Energy API routes ──────────────────────────────────────────────────────

@app.route("/api/energy")
def api_energy():
    """Return raw Linky 30-min readings. Query param: hours (default 168)."""
    hours = request.args.get("hours", 168, type=int)
    hours = min(hours, 8760)
    db = get_db()
    readings = db.get_energy_readings(hours=hours)
    return jsonify(readings)


@app.route("/api/energy/analysis")
def api_energy_analysis():
    """Return daily energy analysis. Query param: days (default 30)."""
    days = request.args.get("days", 30, type=int)
    days = min(days, 365)
    db = get_db()
    analysis = db.get_energy_analysis(days=days)
    return jsonify(analysis)


@app.route("/api/energy/efficiency")
def api_energy_efficiency():
    """Return kWh/h by outdoor temperature band."""
    days = request.args.get("days", 30, type=int)
    db = get_db()
    bands = db.get_temp_band_efficiency(days=days)
    return jsonify(bands)


@app.route("/api/energy/pull-linky")
def api_pull_linky():
    """Trigger an immediate Linky data pull for yesterday."""
    try:
        from airzone_secrets import secrets
        linky_token = secrets.get("linky_token")
        linky_prm = secrets.get("linky_prm")
        if not linky_token or not linky_prm:
            return jsonify({"status": "error",
                            "error": "No Linky credentials configured. "
                                     "Set LINKY_TOKEN and LINKY_PRM in .env"})

        from datetime import date, timedelta
        from airzone_linky import fetch_load_curve, store_load_curve

        yesterday = date.today() - timedelta(days=1)
        try:
            readings = fetch_load_curve(
                linky_token, linky_prm, yesterday, yesterday + timedelta(days=1))
        except Exception as e:
            log.error("Linky API fetch failed: %s", e)
            return jsonify({"status": "error",
                            "error": f"Enedis API fetch failed: {e}"})

        if not readings:
            return jsonify({"status": "ok", "readings": 0,
                            "message": "No new data available from Enedis"})

        # Store readings via HistoryDB connection
        try:
            db = get_db()
            store_load_curve(db.conn, readings)
        except Exception as e:
            log.error("DB error storing Linky readings: %s", e)
            return jsonify({"status": "error",
                            "error": f"Database error: {e}"})

        return jsonify({"status": "ok", "readings": len(readings),
                        "date": str(yesterday)})

    except ImportError as e:
        log.error("Missing module for Linky pull: %s", e)
        return jsonify({"status": "error",
                        "error": f"Missing module: {e}"})
    except Exception as e:
        log.error("Unexpected error in pull-linky: %s", e, exc_info=True)
        return jsonify({"status": "error", "error": str(e)})


@app.route("/api/energy/savings")
def api_energy_savings():
    """Return warm vs cold heating efficiency comparison."""
    from airzone_linky import compute_savings
    db = get_db()
    savings = compute_savings(db.conn, days=30)
    return jsonify(savings)


# ── Netatmo API routes ───────────────────────────────────────────────────

@app.route("/api/netatmo/modules")
def api_netatmo_modules():
    """Return distinct Netatmo module names."""
    try:
        from airzone_netatmo import get_netatmo_module_names
        db = get_db()
        names = get_netatmo_module_names(db.conn)
        return jsonify(names)
    except Exception:
        return jsonify([])


@app.route("/api/netatmo/readings")
def api_netatmo_readings():
    """Return Netatmo readings. Query params: module (optional), hours (default 168)."""
    try:
        from airzone_netatmo import get_netatmo_readings
        module = request.args.get("module")
        hours = request.args.get("hours", 168, type=int)
        hours = min(hours, 8760)
        db = get_db()
        readings = get_netatmo_readings(db.conn, module_name=module, hours=hours)
        return jsonify(readings)
    except Exception:
        return jsonify([])


@app.route("/api/netatmo/sync-status")
def api_netatmo_sync_status():
    """Return Netatmo backfill sync status per module."""
    try:
        from airzone_netatmo import get_sync_status
        db = get_db()
        status = get_sync_status(db.conn)
        return jsonify(status)
    except Exception:
        return jsonify([])


# ── Thermal Model & Predictions API routes ──────────────────────────────────

@app.route("/api/predictions")
def api_predictions():
    """Return DP spread predictions for all zones."""
    try:
        from airzone_thermal_model import run_prediction_cycle
        db = get_db()
        result = run_prediction_cycle(db.conn)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/thermal-models")
def api_thermal_models():
    """Return learned thermal models per zone."""
    try:
        from airzone_thermal_model import load_thermal_models, create_prediction_tables
        db = get_db()
        create_prediction_tables(db.conn)
        models = load_thermal_models(db.conn)
        return jsonify({z: {"samples": m.samples, "confidence": m.confidence,
                            "decay_coeff": m.decay_coeff,
                            "runoff_base": m.runoff_base,
                            "runoff_per_heat_min": m.runoff_per_heat_min,
                            "rh_drift_coeff": m.rh_drift_coeff,
                            "data_days": m.data_days}
                        for z, m in models.items()})
    except Exception as e:
        return jsonify({"error": str(e)})


# ── Baseline & Experiments API routes ───────────────────────────────────────

@app.route("/api/baseline")
def api_baseline():
    """Return learned energy baselines per hour-of-day."""
    try:
        from airzone_baseline import get_baseline
        db = get_db()
        baselines = get_baseline(db.conn)
        return jsonify(baselines)
    except Exception:
        return jsonify([])


@app.route("/api/experiments")
def api_experiments():
    """Return heating experiments."""
    try:
        from airzone_baseline import get_experiments
        db = get_db()
        experiments = get_experiments(db.conn)
        return jsonify(experiments)
    except Exception:
        return jsonify([])


@app.route("/api/experiments/eligibility")
def api_experiment_eligibility():
    """Check if a no-heating experiment can be scheduled."""
    try:
        from airzone_baseline import check_experiment_eligibility
        db = get_db()
        result = check_experiment_eligibility(db.conn)
        return jsonify(result)
    except Exception as e:
        return jsonify({"eligible": False, "reason": str(e)})


# ── Best Price API routes ────────────────────────────────────────────────────

@app.route("/api/tariffs")
def api_tariffs():
    """Return tariff comparison based on Linky data."""
    try:
        from airzone_best_price import BestPriceAnalyzer
        cfg = _load_dashboard_config()
        db = get_db()
        analyzer = BestPriceAnalyzer(
            kva=cfg.get("kva", 9),
            hc_schedule=cfg.get("hc_schedule", "22-6"),
        )
        days = request.args.get("days", 365, type=int)
        result = analyzer.run_analysis(days=days, conn=db.conn)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})


# ── Tariff Periods API ───────────────────────────────────────────────────────

@app.route("/api/tariff-periods")
def api_tariff_periods():
    """Return all tariff periods."""
    db = get_db()
    return jsonify(db.get_tariff_periods())


@app.route("/api/tariff-periods", methods=["POST"])
def api_tariff_periods_add():
    """Add or update a tariff period."""
    data = request.get_json()
    if not data or "start_date" not in data:
        return jsonify({"error": "start_date required"}), 400
    db = get_db()
    row_id = db.add_tariff_period(data)
    return jsonify({"id": row_id, "ok": True})


@app.route("/api/tariff-periods/<int:period_id>", methods=["DELETE"])
def api_tariff_periods_delete(period_id):
    """Delete a tariff period."""
    db = get_db()
    db.delete_tariff_period(period_id)
    return jsonify({"ok": True})


# ── COP Model API route ──────────────────────────────────────────────────────

@app.route("/api/cop-model")
def api_cop_model():
    """Return current COP model coefficients (learned or default from config)."""
    cfg = _load_dashboard_config()
    db = get_db()

    # Check if we have a learned model in the DB
    learned = None
    try:
        row = db.conn.execute(
            "SELECT intercept, slope, r_squared, n_samples, updated_at "
            "FROM cop_model ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if row:
            learned = {
                "intercept": row[0], "slope": row[1],
                "r_squared": row[2], "n_samples": row[3],
                "updated_at": row[4],
            }
    except Exception:
        pass  # Table may not exist yet

    if learned and learned["n_samples"] >= 20 and learned["r_squared"] >= 0.3:
        return jsonify({
            "intercept": learned["intercept"],
            "slope": learned["slope"],
            "learned": True,
            "r_squared": learned["r_squared"],
            "n_samples": learned["n_samples"],
            "updated_at": learned["updated_at"],
        })

    # Fall back to config defaults
    return jsonify({
        "intercept": cfg.get("cop_intercept", 2.5),
        "slope": cfg.get("cop_slope", 0.08),
        "learned": False,
    })


# ── Weather API route ────────────────────────────────────────────────────────

@app.route("/api/weather")
def api_weather():
    """Return current weather + warm-window forecast from Open-Meteo."""
    try:
        from airzone_weather import get_forecast, compute_warm_window
        cfg = _load_dashboard_config()
        lat = cfg.get("latitude", 44.07)
        lon = cfg.get("longitude", -1.26)
        forecast = get_forecast(lat, lon, max_age_seconds=1800)
        if not forecast:
            return jsonify({"error": "Forecast unavailable"})
        warm = compute_warm_window(forecast)
        # Magnus dew point from current outdoor conditions
        current_temp = warm.get("current_outdoor_temp")
        current_rh   = warm.get("current_outdoor_humidity")
        dew = None
        if current_temp is not None and current_rh:
            import math
            a, b = 17.625, 243.04
            try:
                g = math.log(current_rh / 100) + (a * current_temp) / (b + current_temp)
                dew = round((b * g) / (a - g), 1)
            except Exception:
                pass
        return jsonify({
            "current_outdoor_temp": current_temp,
            "current_outdoor_rh":   current_rh,
            "current_dewpoint":     dew,
            "is_warm_now":          warm.get("is_warm_now", False),
            "next_warm_start":      warm.get("next_warm_start"),
            "next_warm_end":        warm.get("next_warm_end"),
            "avg_warm_temp":        warm.get("avg_warm_temp"),
            "warm_window":          warm,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── Phase 8 Feature API routes ──────────────────────────────────────────────

@app.route("/api/analytics/cross-zone")
def api_cross_zone_impact():
    """Return cross-zone heating impact matrix."""
    try:
        from airzone_analytics import compute_cross_zone_impact
        days = request.args.get("days", 30, type=int)
        db = get_db()
        result = compute_cross_zone_impact(db.conn, days=days)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/analytics/condensation")
def api_condensation_events():
    """Return condensation risk events."""
    try:
        from airzone_analytics import get_condensation_events
        days = request.args.get("days", 30, type=int)
        threshold = request.args.get("threshold", 4.0, type=float)
        db = get_db()
        events = get_condensation_events(db.conn, days=days, threshold=threshold)
        return jsonify(events)
    except Exception:
        return jsonify([])


# ── Correlation Matrix API route ─────────────────────────────────────────────

@app.route("/api/correlation-matrix")
def api_correlation_matrix():
    """Compute Pearson correlation matrix across all tracked variables."""
    import math
    days = request.args.get("days", 30, type=int)
    days = min(days, 365)
    hours = days * 24

    db = get_db()
    try:
        readings = db.get_readings(hours=hours)
        zones = db.get_zone_names()

        # Try to get energy data
        energy = []
        try:
            energy = db.get_energy_readings(hours=hours)
        except Exception:
            pass

        # Try to get Netatmo data
        netatmo = []
        try:
            from airzone_netatmo import get_netatmo_readings
            netatmo = get_netatmo_readings(db.conn, hours=hours)
        except Exception:
            pass
    finally:
        pass  # shared connection — do not close

    if not readings or len(readings) < 2:
        return jsonify({"error": "Insufficient data", "variables": [], "matrix": []})

    # ── Build time-aligned data vectors ──────────────────────────────────
    # Group readings by timestamp (rounded to 5 min)
    from collections import defaultdict
    buckets = defaultdict(dict)

    def bucket_key(ts_str):
        """Round timestamp to 5-min bucket."""
        try:
            from datetime import datetime as dt
            t = dt.fromisoformat(ts_str.replace("Z", "+00:00")) if "T" in ts_str else dt.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            m = (t.minute // 5) * 5
            return t.replace(minute=m, second=0, microsecond=0).isoformat()
        except Exception:
            return ts_str[:16]

    def magnus_dewpoint(temp, rh):
        if temp is None or rh is None or rh <= 0:
            return None
        a, b = 17.625, 243.04
        try:
            g = math.log(rh / 100) + (a * temp) / (b + temp)
            return round((b * g) / (a - g), 2)
        except Exception:
            return None

    # Process zone readings into buckets
    for r in readings:
        ts = r.get("timestamp") or r.get("created_at", "")
        bk = bucket_key(ts)
        zn = r.get("zone_name", "")
        temp = r.get("temperature")
        rh = r.get("humidity")
        power = r.get("power", 0)
        outdoor_t = r.get("outdoor_temp")
        outdoor_rh = r.get("outdoor_humidity")
        outdoor_dp = r.get("outdoor_dew_point")

        if temp is not None:
            buckets[bk][f"{zn}_temp"] = temp
        if rh is not None:
            buckets[bk][f"{zn}_rh"] = rh
        if temp is not None and rh is not None:
            dp = magnus_dewpoint(temp, rh)
            if dp is not None:
                buckets[bk][f"{zn}_dp_spread"] = round(temp - dp, 2)
                buckets[bk][f"{zn}_dewpoint"] = dp
            buckets[bk][f"{zn}_heating"] = 1 if power else 0
            # Split temp/rh by heating state
            if power:
                buckets[bk][f"{zn}_temp_heating_on"] = temp
                buckets[bk][f"{zn}_rh_heating_on"] = rh
            else:
                buckets[bk][f"{zn}_temp_heating_off"] = temp
                buckets[bk][f"{zn}_rh_heating_off"] = rh

        # Outdoor (same for all zones, just overwrite)
        if outdoor_t is not None:
            buckets[bk]["outdoor_temp"] = outdoor_t
        if outdoor_rh is not None:
            buckets[bk]["outdoor_rh"] = outdoor_rh
        if outdoor_dp is not None:
            buckets[bk]["outdoor_dewpoint"] = outdoor_dp
        # Additional outdoor vars from DB columns (if available)
        for col_name, var_name in [
            ("outdoor_wind_speed", "outdoor_wind_speed"),
            ("outdoor_wind_dir", "outdoor_wind_dir"),
            ("outdoor_rain", "outdoor_rain"),
            ("outdoor_solar", "outdoor_solar"),
        ]:
            val = r.get(col_name)
            if val is not None:
                buckets[bk][var_name] = val

        # Time features
        try:
            from datetime import datetime as dt
            t = dt.fromisoformat(bk) if "T" in bk else dt.strptime(bk, "%Y-%m-%d %H:%M:%S")
            buckets[bk]["hour_of_day"] = t.hour
            buckets[bk]["day_of_week"] = t.weekday()
        except Exception:
            pass

    # Energy buckets
    for e in energy:
        ts = e.get("timestamp") or e.get("interval_start", "")
        bk = bucket_key(ts)
        wh = e.get("wh") or e.get("value")
        if wh is not None:
            buckets[bk]["linky_kwh"] = wh / 1000 if wh > 100 else wh

    # Netatmo buckets
    for n in netatmo:
        ts = n.get("timestamp") or n.get("created_at", "")
        bk = bucket_key(ts)
        mod = n.get("module_name", "nt")
        if n.get("temperature") is not None:
            buckets[bk][f"nt_{mod}_temp"] = n["temperature"]
        if n.get("humidity") is not None:
            buckets[bk][f"nt_{mod}_rh"] = n["humidity"]
        if n.get("co2") is not None:
            buckets[bk][f"nt_{mod}_co2"] = n["co2"]
        if n.get("noise") is not None:
            buckets[bk][f"nt_{mod}_noise"] = n["noise"]
        if n.get("pressure") is not None:
            buckets[bk][f"nt_{mod}_pressure"] = n["pressure"]

    # ── Inject weather forecast data (wind, rain, solar) into buckets ───
    try:
        from airzone_weather import get_forecast
        cfg = _load_dashboard_config()
        wf = get_forecast(cfg.get("latitude", 44.07), cfg.get("longitude", -1.26))
        if wf:
            wf_times = wf.get("hourly_times", [])
            wf_ws = wf.get("hourly_wind_speed", [])
            wf_wd = wf.get("hourly_wind_direction", [])
            wf_rain = wf.get("hourly_rain", [])
            wf_sol = wf.get("hourly_solar_radiation", [])
            for wi, wt_str in enumerate(wf_times):
                wbk = bucket_key(wt_str)
                if wbk in buckets:
                    if wi < len(wf_ws) and wf_ws[wi] is not None:
                        buckets[wbk]["outdoor_wind_speed"] = wf_ws[wi]
                    if wi < len(wf_wd) and wf_wd[wi] is not None:
                        buckets[wbk]["outdoor_wind_dir"] = wf_wd[wi]
                    if wi < len(wf_rain) and wf_rain[wi] is not None:
                        buckets[wbk]["outdoor_rain"] = wf_rain[wi]
                    if wi < len(wf_sol) and wf_sol[wi] is not None:
                        buckets[wbk]["outdoor_solar"] = wf_sol[wi]
    except Exception:
        pass

    # ── Stove estimation: Salon temp rising while HP OFF ─────────────────
    sorted_keys = sorted(buckets.keys())
    prev_salon_temp = None
    for bk in sorted_keys:
        b = buckets[bk]
        salon_temp = b.get("Salon_temp")
        salon_heating = b.get("Salon_heating", 0)
        if salon_temp is not None and prev_salon_temp is not None:
            temp_change = salon_temp - prev_salon_temp
            # Stove likely running if Salon rising >0.3°C/interval while HP is off
            b["stove_estimated"] = 1 if (temp_change > 0.3 and not salon_heating) else 0
        prev_salon_temp = salon_temp if salon_temp is not None else prev_salon_temp

    # ── Compute change speeds per zone ───────────────────────────────────
    for zn in zones:
        prev_t = prev_rh = prev_dp = None
        for bk in sorted_keys:
            b = buckets[bk]
            t = b.get(f"{zn}_temp")
            rh = b.get(f"{zn}_rh")
            dps = b.get(f"{zn}_dp_spread")
            if t is not None and prev_t is not None:
                b[f"{zn}_temp_change_speed"] = round((t - prev_t) * 12, 2)  # per hour (5min intervals)
            if rh is not None and prev_rh is not None:
                b[f"{zn}_rh_change_speed"] = round((rh - prev_rh) * 12, 2)
            if dps is not None and prev_dp is not None:
                b[f"{zn}_dp_change_speed"] = round((dps - prev_dp) * 12, 2)
            prev_t = t if t is not None else prev_t
            prev_rh = rh if rh is not None else prev_rh
            prev_dp = dps if dps is not None else prev_dp

    # ── Collect all variable names and build aligned vectors ─────────────
    all_vars = set()
    for b in buckets.values():
        all_vars.update(b.keys())
    # Sort: outdoor first, then per-zone, then energy, then netatmo, then derived
    var_list = sorted(all_vars)

    # Build aligned number arrays — only include timestamps where variable has data
    n_buckets = len(sorted_keys)
    var_vectors = {}
    for v in var_list:
        vec = []
        for bk in sorted_keys:
            val = buckets[bk].get(v)
            if val is not None:
                vec.append((bk, float(val)))
        if len(vec) >= 5:  # need minimum data points
            var_vectors[v] = vec

    final_vars = sorted(var_vectors.keys())

    # ── Compute Pearson correlation matrix ───────────────────────────────
    def pearson(v1_data, v2_data):
        """Compute Pearson r for two variable datasets (aligned by timestamp)."""
        # Build aligned pairs
        d1 = dict(v1_data)
        d2 = dict(v2_data)
        common = set(d1.keys()) & set(d2.keys())
        if len(common) < 3:
            return None
        xs = [d1[k] for k in common]
        ys = [d2[k] for k in common]
        n = len(xs)
        sx = sum(xs)
        sy = sum(ys)
        sxx = sum(x * x for x in xs)
        syy = sum(y * y for y in ys)
        sxy = sum(x * y for x, y in zip(xs, ys))
        val = (n * sxx - sx * sx) * (n * syy - sy * sy)
        if val <= 0:
            return 0.0
        denom = math.sqrt(val)
        if denom == 0:
            return 0.0
        return round((n * sxy - sx * sy) / denom, 3)

    # Only compute upper triangle (matrix is symmetric)
    matrix = []
    for i, v1 in enumerate(final_vars):
        row = []
        for j, v2 in enumerate(final_vars):
            if i == j:
                row.append(1.0)
            elif j < i:
                row.append(matrix[j][i])  # symmetric
            else:
                r = pearson(var_vectors[v1], var_vectors[v2])
                row.append(r)
        matrix.append(row)

    return jsonify({
        "variables": final_vars,
        "matrix": matrix,
        "data_points": n_buckets,
        "days": days,
    })


# ── Control Brain API routes ────────────────────────────────────────────────

@app.route("/api/brain/status")
def api_brain_status():
    """Return current control brain status and recent decisions."""
    try:
        db = get_db()
        recent = db.conn.execute(
            "SELECT zone_name, created_at, action, dp_spread, reason "
            "FROM control_log ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        return jsonify([dict(zip(
            ["zone_name", "created_at", "action", "dp_spread", "reason"], r))
            for r in recent])
    except Exception:
        return jsonify([])


@app.route("/api/brain/daily-assessment")
def api_brain_daily_assessment():
    """Return daily energy assessments."""
    try:
        days = request.args.get("days", 30, type=int)
        db = get_db()
        rows = db.conn.execute(
            "SELECT date, total_zones_heated, total_heating_events, "
            "       estimated_kwh, estimated_cost, linky_actual_kwh, "
            "       correction_factor, avg_dp_spread, min_dp_spread "
            "FROM daily_assessment ORDER BY date DESC LIMIT ?",
            (days,)
        ).fetchall()
        return jsonify([dict(zip(
            ["date", "total_zones_heated", "total_heating_events",
             "estimated_kwh", "estimated_cost", "linky_actual_kwh",
             "correction_factor", "avg_dp_spread", "min_dp_spread"], r))
            for r in rows])
    except Exception:
        return jsonify([])


# ── Analytics readings alias ──────────────────────────────────────────────────

@app.route("/api/analytics/readings")
def api_analytics_readings():
    """Alias for /api/readings — used by frontend self-learning and correlation."""
    zone = request.args.get("zone")
    hours = request.args.get("hours", 168, type=int)
    hours = min(hours, 8760)

    db = get_db()
    readings = db.get_readings(zone_name=zone, hours=hours)
    return jsonify(readings)


# ── DHW (Domestic Hot Water) API routes ──────────────────────────────────────

@app.route("/api/dhw/status")
def api_dhw_status():
    """Return current DHW status from Airzone API."""
    try:
        from airzone_humidity_controller import AirzoneCloudAPI
        # Try to get DHW status from cached API state
        db = get_db()
        # Check if we have DHW data in zone_readings (zone named 'DHW' or similar)
        row = db.conn.execute(
            "SELECT temperature, humidity, created_at "
            "FROM zone_readings WHERE zone_name LIKE '%DHW%' OR zone_name LIKE '%ECS%' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row:
            return jsonify({
                "current_temp": row[0],
                "target_temp": 56,
                "status": "Monitoring",
                "timestamp": row[2],
            })
        return jsonify({
            "current_temp": None,
            "target_temp": 56,
            "status": "No DHW data",
            "timestamp": None,
        })
    except Exception as e:
        return jsonify({
            "current_temp": None,
            "target_temp": 56,
            "status": "API endpoint coming",
            "error": str(e),
        })


# ── Settings save endpoint ───────────────────────────────────────────────────

@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    """Save dashboard settings to config file."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        cfg = _load_dashboard_config()
        # Merge new settings into existing config
        for key, value in data.items():
            cfg[key] = value

        # Write to config file
        with open(PI_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)

        return jsonify({"status": "ok", "saved_keys": list(data.keys())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Airzone Web Dashboard")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--no-poller", action="store_true",
                        help="Disable background API polling (read-only from DB)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.no_poller:
        start_poller()

    print(f"Airzone Dashboard: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

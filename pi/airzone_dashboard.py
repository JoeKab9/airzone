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
import sys
from pathlib import Path

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


def get_db() -> HistoryDB:
    """Open a read-only connection to the history DB."""
    return HistoryDB(DB_PATH)


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
    db.close()
    return jsonify(zones)


@app.route("/api/latest")
def api_latest():
    """Return the most recent reading for each zone."""
    db = get_db()
    latest = db.get_latest()
    db.close()
    return jsonify(latest)


@app.route("/api/readings")
def api_readings():
    """Return time-series readings. Query params: zone (optional), hours (default 168)."""
    zone = request.args.get("zone")
    hours = request.args.get("hours", 168, type=int)
    hours = min(hours, 8760)  # cap at 1 year

    db = get_db()
    readings = db.get_readings(zone_name=zone, hours=hours)
    db.close()
    return jsonify(readings)


@app.route("/api/actions")
def api_actions():
    """Return recent control actions. Query param: hours (default 168)."""
    hours = request.args.get("hours", 168, type=int)
    hours = min(hours, 8760)

    db = get_db()
    actions = db.get_actions(hours=hours)
    db.close()
    return jsonify(actions)


@app.route("/api/stats")
def api_stats():
    """Return database statistics."""
    db = get_db()
    stats = db.get_stats()
    db.close()
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
    db.close()
    return jsonify(profiles)


@app.route("/api/analytics/cycles")
def api_heating_cycles():
    """Return recent heating cycles. Query params: zone (optional), hours (default 168)."""
    zone = request.args.get("zone")
    hours = request.args.get("hours", 168, type=int)
    hours = min(hours, 8760)
    db = get_db()
    cycles = db.get_heating_cycles(hours=hours, zone_name=zone)
    db.close()
    return jsonify(cycles)


@app.route("/api/analytics/warm-hours")
def api_warm_hours():
    """Return warm hours recommendation."""
    cfg = _load_dashboard_config()
    db = get_db()
    rec = db.get_warm_hours_recommendation(cfg)
    db.close()
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
    db.close()
    return jsonify(result)


@app.route("/api/analytics/optimization-log")
def api_optimization_log():
    """Return optimization log entries."""
    db = get_db()
    log_entries = db.get_optimization_log(limit=20)
    db.close()
    return jsonify(log_entries)


# ── Energy API routes ──────────────────────────────────────────────────────

@app.route("/api/energy")
def api_energy():
    """Return raw Linky 30-min readings. Query param: hours (default 168)."""
    hours = request.args.get("hours", 168, type=int)
    hours = min(hours, 8760)
    db = get_db()
    readings = db.get_energy_readings(hours=hours)
    db.close()
    return jsonify(readings)


@app.route("/api/energy/analysis")
def api_energy_analysis():
    """Return daily energy analysis. Query param: days (default 30)."""
    days = request.args.get("days", 30, type=int)
    days = min(days, 365)
    db = get_db()
    analysis = db.get_energy_analysis(days=days)
    db.close()
    return jsonify(analysis)


@app.route("/api/energy/efficiency")
def api_energy_efficiency():
    """Return kWh/h by outdoor temperature band."""
    days = request.args.get("days", 30, type=int)
    db = get_db()
    bands = db.get_temp_band_efficiency(days=days)
    db.close()
    return jsonify(bands)


@app.route("/api/energy/savings")
def api_energy_savings():
    """Return warm vs cold heating efficiency comparison."""
    from airzone_linky import compute_savings
    db = get_db()
    savings = compute_savings(db.conn, days=30)
    db.close()
    return jsonify(savings)


# ── Netatmo API routes ───────────────────────────────────────────────────

@app.route("/api/netatmo/modules")
def api_netatmo_modules():
    """Return distinct Netatmo module names."""
    try:
        from airzone_netatmo import get_netatmo_module_names
        db = get_db()
        names = get_netatmo_module_names(db.conn)
        db.close()
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
        db.close()
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
        db.close()
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
        db.close()
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
        db.close()
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
        db.close()
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
        db.close()
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
        db.close()
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
        db.close()
        return jsonify(result)
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
        db.close()
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
        db.close()
        return jsonify(events)
    except Exception:
        return jsonify([])


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
        db.close()
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
        db.close()
        return jsonify([dict(zip(
            ["date", "total_zones_heated", "total_heating_events",
             "estimated_kwh", "estimated_cost", "linky_actual_kwh",
             "correction_factor", "avg_dp_spread", "min_dp_spread"], r))
            for r in rows])
    except Exception:
        return jsonify([])


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Airzone Web Dashboard")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    print(f"Airzone Dashboard: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

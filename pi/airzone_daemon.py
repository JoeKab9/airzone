#!/usr/bin/env python3
"""
Airzone Pi Daemon
==================
Headless humidity controller for Raspberry Pi.

Reuses the core API and control logic from the parent directory,
adds SQLite history logging, and runs as a systemd service.

Usage:
    python3 airzone_daemon.py                  # run as daemon
    python3 airzone_daemon.py --once           # single check, then exit
    python3 airzone_daemon.py --once --dry-run # test without sending commands
    python3 airzone_daemon.py --status         # print current zone data
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Import parent modules ────────────────────────────────────────────────────
# The core controller and weather modules live in ../src/.
PI_DIR = Path(__file__).parent
PARENT_DIR = PI_DIR.parent
sys.path.insert(0, str(PARENT_DIR / "src"))

from airzone_humidity_controller import (  # noqa: E402
    AirzoneCloudAPI,
    DEFAULT_CONFIG,
    MODE_NAMES,
    check_and_control,
    load_config,
    print_status,
    setup_logging,
)

from airzone_db import HistoryDB  # noqa: E402
from airzone_analytics import run_full_analysis, create_analytics_tables  # noqa: E402

# DP-spread control brain (new predictive controller)
try:
    from airzone_control_brain import (  # noqa: E402
        ControlBrain, build_weather_info, create_brain_tables,
    )
    HAS_CONTROL_BRAIN = True
except ImportError:
    HAS_CONTROL_BRAIN = False

# ── Override paths for Pi ────────────────────────────────────────────────────
# Keep state, tokens, and DB inside pi/data/ so they're isolated from the Mac.
import airzone_humidity_controller as _ctrl  # noqa: E402

DATA_DIR = PI_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_ctrl.STATE_PATH = DATA_DIR / "airzone_state.json"
_ctrl.TOKEN_PATH = DATA_DIR / ".airzone_tokens.json"

# Also redirect the weather cache
try:
    import airzone_weather as _weather
    _weather.WEATHER_CACHE_PATH = DATA_DIR / "airzone_weather_cache.json"
except ImportError:
    pass

# Pi-specific default: poll every hour
PI_DEFAULT_CONFIG = {**DEFAULT_CONFIG, "poll_interval_seconds": 3600}
PI_CONFIG_PATH = PI_DIR / "airzone_pi_config.json"

log = logging.getLogger("airzone")


# ── Action detection ─────────────────────────────────────────────────────────

def detect_actions(state_before: dict, state_after: dict,
                   db: HistoryDB, cfg: dict):
    """Compare state before/after check_and_control to log actions."""
    act_before = set(state_before.get("zones_we_activated", {}).keys())
    act_after = set(state_after.get("zones_we_activated", {}).keys())
    pend_before = set(state_before.get("zones_pending_warm", {}).keys())
    pend_after = set(state_after.get("zones_pending_warm", {}).keys())

    on_thresh = cfg["humidity_on_threshold"]
    emergency = cfg.get("emergency_humidity_threshold", 88)

    # Newly activated
    for dev_id in act_after - act_before:
        info = state_after["zones_we_activated"][dev_id]
        label = info.get("label", dev_id)
        hum = info.get("humidity_at_activation")
        if hum and hum >= emergency:
            db.log_action(label, dev_id, "emergency_on", hum,
                          f"Emergency: humidity {hum}% >= {emergency}%")
        else:
            db.log_action(label, dev_id, "heating_on", hum,
                          f"Humidity {hum}% >= {on_thresh}%")

    # Deactivated
    for dev_id in act_before - act_after:
        info = state_before["zones_we_activated"][dev_id]
        label = info.get("label", dev_id)
        db.log_action(label, dev_id, "heating_off", None,
                      f"Humidity recovered <= {cfg['humidity_off_threshold']}%")

    # Newly deferred
    for dev_id in pend_after - pend_before:
        info = state_after["zones_pending_warm"][dev_id]
        label = info.get("label", dev_id)
        hum = info.get("humidity_at_trigger")
        db.log_action(label, dev_id, "deferred", hum,
                      f"Deferred to warm window (humidity {hum}%)")

    # Cancelled pending
    for dev_id in pend_before - pend_after:
        if dev_id not in act_after:  # wasn't activated, was cancelled
            info = state_before["zones_pending_warm"][dev_id]
            label = info.get("label", dev_id)
            db.log_action(label, dev_id, "cancelled", None,
                          "Humidity recovered while pending")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Airzone Pi Daemon — headless humidity controller")
    parser.add_argument("--config", type=Path, default=PI_CONFIG_PATH,
                        help="Path to config JSON (default: airzone_pi_config.json)")
    parser.add_argument("--status", action="store_true",
                        help="Print all zone data and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run without sending any commands")
    parser.add_argument("--once", action="store_true",
                        help="Check once and exit (instead of looping)")
    args = parser.parse_args()

    # Load config (fall back to parent config if Pi config doesn't exist)
    if args.config.exists():
        cfg = load_config(args.config)
    elif (PARENT_DIR / "airzone_config.json").exists():
        log.info("No Pi config found, using parent airzone_config.json")
        cfg = load_config(PARENT_DIR / "airzone_config.json")
    else:
        cfg = load_config(args.config)  # will create default and exit

    # Apply Pi defaults
    for k, v in PI_DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)

    setup_logging(str(DATA_DIR / cfg.get("log_file", "airzone_daemon.log"))
                  if not args.status else "")

    if args.dry_run:
        cfg["dry_run"] = True

    email = cfg.get("email", "")
    password = cfg.get("password", "")
    if not email or not password:
        print("Error: 'email' and 'password' must be set in config")
        print("       (or via AIRZONE_EMAIL / AIRZONE_PASSWORD env vars)\n")
        sys.exit(1)

    # Initialize API + DB
    api = AirzoneCloudAPI()
    db = HistoryDB()

    if not api.load_cached_tokens():
        api.login(email, password)

    # ── Status mode
    if args.status:
        zones = api.get_all_zones()
        print_status(zones)
        return

    # ── Daemon loop
    log.info("Airzone Pi Daemon starting")
    log.info("  Thresholds: ON >= %d%%,  OFF <= %d%%",
             cfg["humidity_on_threshold"], cfg["humidity_off_threshold"])
    log.info("  Poll interval: %ds (%s)",
             cfg["poll_interval_seconds"],
             f"{cfg['poll_interval_seconds'] // 3600}h"
             if cfg["poll_interval_seconds"] >= 3600
             else f"{cfg['poll_interval_seconds']}s")
    if cfg["dry_run"]:
        log.info("  *** DRY RUN — no commands will be sent ***")

    state = _ctrl.load_state()
    running = True

    def shutdown(sig, frame):
        nonlocal running
        log.info("Shutting down (signal %s)", sig)
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while running:
        try:
            api.ensure_token(email, password)
        except Exception as e:
            log.error("Authentication error: %s — retrying next cycle", e)
            if args.once:
                break
            time.sleep(60)
            continue

        # 1. Fetch zones and log readings
        weather_info = None
        outdoor_temp = None
        outdoor_dew_point = None
        outdoor_humidity = None
        try:
            zones = api.get_all_zones()
            if zones:
                # Fetch weather for outdoor temp + dew point + humidity
                if cfg.get("weather_optimization", False):
                    try:
                        from airzone_weather import (
                            get_forecast, compute_warm_window, estimate_cop_savings,
                        )
                        forecast = get_forecast(
                            cfg.get("latitude", 44.07),
                            cfg.get("longitude", -1.26))
                        weather_info = compute_warm_window(
                            forecast, cfg.get("warm_hours_count", 6))
                        outdoor_temp = weather_info.get("current_outdoor_temp")
                        outdoor_dew_point = weather_info.get(
                            "current_outdoor_dew_point")
                        outdoor_humidity = weather_info.get(
                            "current_outdoor_humidity")
                    except Exception as e:
                        log.error("Weather fetch failed: %s — proceeding without", e)

                # Log readings to history DB
                db.log_readings(zones, outdoor_temp,
                                outdoor_dew_point=outdoor_dew_point,
                                outdoor_humidity=outdoor_humidity)
                log.info("Logged readings for %d zone(s) to history DB", len(zones))
            else:
                log.warning("No zones returned from API")
        except Exception as e:
            log.error("Failed to fetch zones: %s", e)

        # 2. Run control logic
        if HAS_CONTROL_BRAIN and cfg.get("dp_spread_control", True):
            # ── New DP-spread predictive controller ──
            try:
                brain = ControlBrain(db.conn)
                # Build weather_info for the brain
                brain_weather = None
                if weather_info and cfg.get("weather_optimization", False):
                    try:
                        from airzone_weather import get_forecast
                        forecast = get_forecast(
                            cfg.get("latitude", 44.07),
                            cfg.get("longitude", -1.26))
                        brain_weather = build_weather_info(forecast)
                    except Exception as e:
                        log.error("Brain weather build error: %s", e)

                # Gather Netatmo module data for sensor fusion
                netatmo_modules = []
                if cfg.get("netatmo_enabled", False):
                    try:
                        from airzone_netatmo import get_access_token, get_stations
                        client_id = cfg.get("netatmo_client_id", "")
                        client_secret = cfg.get("netatmo_client_secret", "")
                        token = get_access_token(client_id, client_secret)
                        if token:
                            stations = get_stations(token)
                            for mod in stations:
                                dash = mod.get("dashboard", {})
                                if dash:
                                    netatmo_modules.append({
                                        "name": mod.get("module_name", ""),
                                        "module_type": mod.get("module_type", ""),
                                        **dash,
                                    })
                    except Exception as e:
                        log.error("Netatmo fetch for brain: %s", e)

                result = brain.run_cycle(
                    api, cfg,
                    weather_info=brain_weather,
                    netatmo_modules=netatmo_modules,
                    dry_run=cfg.get("dry_run", False),
                )
                zone_decisions = result.get("zones", [])
                log.info("Control brain: %d zone decisions", len(zone_decisions))
                for zd in zone_decisions:
                    if zd.get("action") != "no_change":
                        log.info("  %s: %s", zd["zone_name"], zd["action"])
            except Exception as e:
                log.error("Control brain error: %s — falling back to legacy", e)
                # Fallback to legacy control
                state_before = deepcopy(state)
                state = check_and_control(api, cfg, state,
                                         weather_info=weather_info,
                                         analytics_conn=db.conn)
                detect_actions(state_before, state, db, cfg)
        else:
            # ── Legacy humidity-threshold controller ──
            try:
                state_before = deepcopy(state)
                state = check_and_control(api, cfg, state,
                                         weather_info=weather_info,
                                         analytics_conn=db.conn)
                detect_actions(state_before, state, db, cfg)
            except Exception as e:
                log.error("Control loop error: %s", e)

        # 3. Run analytics (detect heating cycles, compute profiles)
        try:
            result = run_full_analysis(db.conn, cfg, days=30)
            rec = result.get("warm_hours_recommendation")

            # Auto-optimize warm hours if enabled
            if (rec and cfg.get("auto_optimize_warm_hours", False)
                    and rec["confidence"] >= 0.70
                    and abs(rec["recommended"] - rec["current"]) >= 1):
                old_val = cfg["warm_hours_count"]
                cfg["warm_hours_count"] = rec["recommended"]
                log.info("Auto-optimized warm_hours_count: %d → %d "
                         "(confidence %.0f%%)",
                         old_val, rec["recommended"],
                         rec["confidence"] * 100)

                # Log to optimization_log
                now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
                db.conn.execute(
                    "INSERT INTO optimization_log "
                    "(timestamp, metric, current_value, recommended_value, "
                    " confidence, reasoning, applied) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (now, "warm_hours_count", str(old_val),
                     str(rec["recommended"]), rec["confidence"],
                     rec["reasoning"]))
                db.conn.commit()

                # Save updated config
                if args.config.exists():
                    with open(args.config, "r") as f:
                        saved_cfg = json.load(f)
                    saved_cfg["warm_hours_count"] = rec["recommended"]
                    with open(args.config, "w") as f:
                        json.dump(saved_cfg, f, indent=2)
            elif rec:
                log.info("Analytics: recommend %dh warm window "
                         "(currently %dh, confidence %.0f%%)",
                         rec["recommended"], rec["current"],
                         rec["confidence"] * 100)
        except Exception as e:
            log.error("Analytics error: %s", e)

        # 4. Fetch Linky energy data (once per day, after 7 AM)
        if cfg.get("linky_enabled", False):
            try:
                last_fetch = state.get("last_linky_fetch", "")
                today_str = str(date.today())
                now_hour = datetime.now().hour

                if last_fetch != today_str and now_hour >= 7:
                    from airzone_linky import (
                        fetch_load_curve, run_energy_analysis,
                    )
                    token = cfg["linky_token"]
                    prm = cfg["linky_prm"]
                    if token and prm:
                        yesterday = date.today() - timedelta(days=1)
                        readings = fetch_load_curve(
                            token, prm, yesterday,
                            yesterday + timedelta(days=1))
                        if readings:
                            db.log_linky_readings(readings)
                            log.info("Linky: stored %d readings for %s",
                                     len(readings), yesterday)
                            # Run energy analysis
                            summary = run_energy_analysis(db.conn, days=30)
                            if summary.get("savings"):
                                s = summary["savings"]
                                log.info("Linky: %s", s["reasoning"])
                        state["last_linky_fetch"] = today_str
                        _ctrl.save_state(state)
                    else:
                        log.warning("Linky enabled but token/prm not set")
            except Exception as e:
                log.error("Linky fetch error: %s", e)

        # 5. Fetch Netatmo current readings (every poll cycle)
        if cfg.get("netatmo_enabled", False):
            try:
                last_netatmo = state.get("last_netatmo_fetch", "")
                now_str = datetime.utcnow().strftime("%Y-%m-%dT%H")

                # Fetch current station data every poll (quick API call)
                if last_netatmo != now_str:
                    from airzone_netatmo import (
                        get_access_token, get_stations, store_readings,
                        create_netatmo_tables,
                    )
                    client_id = cfg.get("netatmo_client_id", "")
                    client_secret = cfg.get("netatmo_client_secret", "")
                    token = get_access_token(client_id, client_secret)
                    if token:
                        create_netatmo_tables(db.conn)
                        stations = get_stations(token)
                        if stations:
                            readings = []
                            now_ts = datetime.utcnow().strftime(
                                "%Y-%m-%dT%H:%M:%SZ")
                            for mod in stations:
                                dash = mod.get("dashboard", {})
                                if not dash:
                                    continue
                                readings.append({
                                    "timestamp": now_ts,
                                    "module_mac": mod.get("module_id")
                                        or mod.get("device_id", ""),
                                    "module_name": mod.get("module_name", ""),
                                    "Temperature": dash.get("Temperature"),
                                    "Humidity": dash.get("Humidity"),
                                    "CO2": dash.get("CO2"),
                                    "Noise": dash.get("Noise"),
                                    "Pressure": dash.get("Pressure"),
                                })
                            stored = store_readings(db.conn, readings)
                            log.info("Netatmo: stored %d readings from %d modules",
                                     stored, len(readings))
                            state["last_netatmo_fetch"] = now_str
                            _ctrl.save_state(state)
                    else:
                        log.warning("Netatmo: no valid token — run --auth first")
            except Exception as e:
                log.error("Netatmo fetch error: %s", e)

        # 6. Netatmo historical backfill (once on startup, then weekly)
        if cfg.get("netatmo_enabled", False):
            try:
                last_backfill = state.get("last_netatmo_backfill", "")
                today_str_bf = datetime.utcnow().strftime("%Y-%U")  # year-week
                if last_backfill != today_str_bf:
                    from airzone_netatmo import backfill_history
                    client_id = cfg.get("netatmo_client_id", "")
                    client_secret = cfg.get("netatmo_client_secret", "")
                    if client_id and client_secret:
                        result = backfill_history(
                            client_id, client_secret, db.conn, days=365)
                        total = result.get("total_new_readings", 0)
                        if total > 0:
                            log.info("Netatmo backfill: %d new readings", total)
                        state["last_netatmo_backfill"] = today_str_bf
                        _ctrl.save_state(state)
            except Exception as e:
                log.error("Netatmo backfill error: %s", e)

        # 7. Thermal model learning + predictions (every 6 hours)
        try:
            last_predict = state.get("last_prediction_cycle", "")
            predict_key = datetime.utcnow().strftime("%Y-%m-%dT") + \
                str(datetime.utcnow().hour // 6 * 6)
            if last_predict != predict_key:
                from airzone_thermal_model import (
                    run_prediction_cycle, create_prediction_tables,
                )
                create_prediction_tables(db.conn)

                # Build weather forecast for predictions
                pred_weather = None
                try:
                    from airzone_weather import get_forecast
                    raw = get_forecast(
                        cfg.get("latitude", 44.07),
                        cfg.get("longitude", -1.26))
                    if raw and raw.get("hourly"):
                        hourly = raw["hourly"]
                        idx3 = min(3, len(hourly.get("temperature_2m", [])) - 1)
                        idx24 = min(24, len(hourly.get("temperature_2m", [])) - 1)
                        pred_weather = {
                            "current_temp": raw.get("current", {}).get("temperature_2m"),
                            "current_hum": raw.get("current", {}).get("relative_humidity_2m"),
                            "temp_3h": hourly["temperature_2m"][idx3] if idx3 >= 0 else None,
                            "hum_3h": hourly["relative_humidity_2m"][idx3] if idx3 >= 0 else None,
                            "temp_24h": hourly["temperature_2m"][idx24] if idx24 >= 0 else None,
                            "hum_24h": hourly["relative_humidity_2m"][idx24] if idx24 >= 0 else None,
                        }
                except Exception:
                    pass

                result = run_prediction_cycle(db.conn, pred_weather)
                preds = result.get("predictions_3h", {})
                models = result.get("thermal_models", {})
                if preds:
                    log.info("Predictions: %d zones, models: %s",
                             len(preds),
                             {z: f"{m['samples']}obs/{m['confidence']:.0%}"
                              for z, m in models.items()})
                state["last_prediction_cycle"] = predict_key
                _ctrl.save_state(state)
        except Exception as e:
            log.error("Prediction cycle error: %s", e)

        # 8. Energy baseline learning (once per day, after 8 AM)
        if cfg.get("linky_enabled", False):
            try:
                last_baseline = state.get("last_baseline_learn", "")
                today_str_bl = datetime.utcnow().strftime("%Y-%m-%d")
                hour_now = datetime.utcnow().hour
                if last_baseline != today_str_bl and hour_now >= 8:
                    from airzone_baseline import (
                        learn_baseline, create_baseline_tables,
                        check_experiment_eligibility, schedule_experiment,
                        get_active_experiment, complete_experiment,
                    )
                    create_baseline_tables(db.conn)
                    result = learn_baseline(db.conn, days=7)
                    if result.get("success"):
                        log.info("Baseline: updated %d hours, "
                                 "excluded %d heating hours",
                                 result.get("hours_updated", 0),
                                 result.get("heating_hours_excluded", 0))

                    # Check/manage experiments
                    active = get_active_experiment(db.conn)
                    if active:
                        if str(date.today()) > active["end_date"]:
                            comp = complete_experiment(db.conn, active["id"])
                            log.info("Experiment completed: %s",
                                     comp.get("summary", ""))
                    elif cfg.get("auto_experiments", False):
                        elig = check_experiment_eligibility(db.conn)
                        if elig.get("eligible"):
                            exp = schedule_experiment(db.conn)
                            log.info("Experiment scheduled: %s to %s",
                                     exp["start_date"], exp["end_date"])

                    state["last_baseline_learn"] = today_str_bl
                    _ctrl.save_state(state)
            except Exception as e:
                log.error("Baseline learning error: %s", e)

        # 9. Optional Supabase sync (every poll cycle if enabled)
        if cfg.get("supabase_sync", False):
            try:
                from airzone_supabase import SupabaseSync
                sync = SupabaseSync(db.conn)
                if sync.enabled:
                    summary = sync.run_full_sync()
                    total_synced = sum(v for k, v in summary.items()
                                       if isinstance(v, int))
                    if total_synced > 0:
                        log.info("Supabase sync: %d entries", total_synced)
            except Exception as e:
                log.error("Supabase sync error: %s", e)

        if args.once:
            break

        # Sleep until next poll
        deadline = time.time() + cfg["poll_interval_seconds"]
        while running and time.time() < deadline:
            time.sleep(1)

    db.close()
    log.info("Airzone Pi Daemon stopped")


if __name__ == "__main__":
    main()

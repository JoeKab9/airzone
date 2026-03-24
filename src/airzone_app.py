#!/usr/bin/env python3
"""
Airzone Humidity Controller — macOS GUI
========================================
Shows live temperature, humidity and on/off status per zone.
Optionally auto-enables heating when humidity exceeds the threshold.

Requires:  pip install PyQt5 requests
Run:       python3 airzone_app.py
"""

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
    from PyQt5.QtGui import QBrush, QColor, QFont
    from PyQt5.QtWidgets import (
        QApplication, QDialog, QDialogButtonBox, QDoubleSpinBox,
        QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
        QMainWindow, QPushButton, QSpinBox, QStatusBar,
        QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
        QCheckBox, QHeaderView, QFrame, QSizePolicy, QScrollArea,
        QFileDialog,
    )
except ImportError:
    print("Missing dependency:  python3 -m pip install PyQt5")
    sys.exit(1)

try:
    import requests as _requests_lib
except ImportError:
    _requests_lib = None

try:
    import matplotlib
    matplotlib.use("Qt5Agg")  # force Qt5 backend before any other import
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
    from matplotlib.figure import Figure
    import matplotlib.dates as mdates
    HAS_MATPLOTLIB = True
except Exception:
    HAS_MATPLOTLIB = False

# Import from sibling module (source files live together in src/)
SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))
from airzone_humidity_controller import (
    AirzoneCloudAPI, load_config, load_state, save_state,
    check_and_control, check_and_control_dhw, CONFIG_PATH, MODE_NAMES,
    dhw_get_status, dhw_set,
)
from airzone_analytics import (
    create_analytics_tables, migrate_analytics_tables,
    run_full_analysis, get_recent_cycles,
    compute_zone_profile, compute_optimal_warm_hours,
)
from airzone_linky import (
    create_linky_tables, fetch_load_curve, store_load_curve,
    run_energy_analysis, get_energy_readings, get_energy_analysis,
    get_temp_band_efficiency, import_enedis_file,
)


def _fmt_date(iso: str) -> str:
    """Convert ISO date string (YYYY-MM-DD) to '07 Mar 2026' format."""
    try:
        d = date.fromisoformat(iso[:10])
        return d.strftime("%d %b %Y")
    except (ValueError, TypeError):
        return iso


# ── Secure config saving ────────────────────────────────────────────────────
# Sensitive keys are stored in .env (project-isolated); the config file only
# keeps non-sensitive settings.  When reading, load_config() merges both.

_SENSITIVE_CONFIG_KEYS = {
    "email", "password", "linky_token", "linky_prm",
    "netatmo_client_id", "netatmo_client_secret",
}


def _save_config_secure(cfg: dict):
    """Save config: sensitive values → .env, rest → JSON file."""
    try:
        from airzone_secrets import secrets
        for key in _SENSITIVE_CONFIG_KEYS:
            val = cfg.get(key, "")
            if val:
                secrets.set(key, str(val))
    except Exception:
        pass  # Secrets module unavailable — everything stays in config file

    # Write config file with sensitive values blanked out
    file_cfg = dict(cfg)
    for key in _SENSITIVE_CONFIG_KEYS:
        if key in file_cfg and file_cfg[key]:
            file_cfg[key] = ""

    CONFIG_PATH.write_text(json.dumps(file_cfg, indent=2) + "\n")


# ── Local history database ────────────────────────────────────────────────────
# Use CONFIG_PATH.parent so the DB is placed next to the config file —
# this works correctly inside a PyInstaller bundle (frozen-aware).

LOCAL_DB_PATH = CONFIG_PATH.parent / "airzone_history.db"


class LocalHistoryDB:
    """Lightweight SQLite store so the macOS app can graph its own readings."""

    def __init__(self, path: Path = LOCAL_DB_PATH):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS zone_readings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT    NOT NULL,
                zone_name    TEXT    NOT NULL,
                device_id    TEXT    NOT NULL,
                temperature  REAL,
                humidity     INTEGER,
                power        INTEGER NOT NULL DEFAULT 0,
                outdoor_temp REAL
            );
            CREATE INDEX IF NOT EXISTS idx_readings_zone_ts
                ON zone_readings(zone_name, timestamp);
        """)
        self.conn.commit()
        # Analytics tables (heating cycles, zone analytics, optimization log)
        create_analytics_tables(self.conn)
        migrate_analytics_tables(self.conn)
        # Linky energy tables
        create_linky_tables(self.conn)
        # Control brain tables (DP spread predictions, daily assessments, etc.)
        try:
            from airzone_control_brain import create_brain_tables
            create_brain_tables(self.conn)
        except ImportError:
            pass
        # Thermal model & prediction tables
        try:
            from airzone_thermal_model import create_prediction_tables
            create_prediction_tables(self.conn)
        except ImportError:
            pass
        # Baseline & experiment tables
        try:
            from airzone_baseline import create_baseline_tables
            create_baseline_tables(self.conn)
        except ImportError:
            pass
        # Migration: add missing columns
        cursor = self.conn.execute("PRAGMA table_info(zone_readings)")
        existing = {row[1] for row in cursor.fetchall()}
        if "outdoor_dew_point" not in existing:
            self.conn.execute(
                "ALTER TABLE zone_readings ADD COLUMN outdoor_dew_point REAL")
        if "outdoor_humidity" not in existing:
            self.conn.execute(
                "ALTER TABLE zone_readings ADD COLUMN outdoor_humidity REAL")
        self.conn.commit()

    def log_readings(self, zones: list, outdoor_temp=None,
                     outdoor_dew_point=None, outdoor_humidity=None):
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        rows = []
        for z in zones:
            dev_id = z.get("_device_id", "")
            inst = z.get("_installation_name", "")
            name = z.get("name", dev_id)
            zone_name = f"{inst}/{name}" if inst else name
            temp = z.get("local_temp")
            if isinstance(temp, dict):
                temp = temp.get("celsius")
            humidity = z.get("humidity")
            power = 1 if z.get("power") else 0
            rows.append((now, zone_name, dev_id, temp, humidity, power,
                         outdoor_temp, outdoor_dew_point, outdoor_humidity))
        self.conn.executemany(
            "INSERT INTO zone_readings "
            "(timestamp, zone_name, device_id, temperature, humidity, power, "
            " outdoor_temp, outdoor_dew_point, outdoor_humidity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.conn.commit()

    def get_readings(self, zone_name: str, hours: int = 168) -> list:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat(
            timespec="seconds") + "Z"
        rows = self.conn.execute(
            "SELECT * FROM zone_readings "
            "WHERE zone_name = ? AND timestamp >= ? ORDER BY timestamp",
            (zone_name, cutoff)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()


# ── Background workers ────────────────────────────────────────────────────────

class LoginWorker(QThread):
    """Attempts login in a background thread."""
    success = pyqtSignal()
    failure = pyqtSignal(str)

    def __init__(self, api: AirzoneCloudAPI, email: str, password: str):
        super().__init__()
        self.api = api
        self.email = email
        self.password = password

    def run(self):
        try:
            self.api.login(self.email, self.password)
            self.success.emit()
        except Exception as e:
            self.failure.emit(str(e))


class ZoneControlWorker(QThread):
    """Sends a power on/off command to a single zone in the background."""
    done = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, api: AirzoneCloudAPI, dev_id: str, inst_id: str, power: bool):
        super().__init__()
        self.api = api
        self.dev_id = dev_id
        self.inst_id = inst_id
        self.power = power

    def run(self):
        try:
            self.api.set_zone(self.dev_id, installation_id=self.inst_id, power=self.power)
            self.done.emit()
        except Exception as e:
            self.error.emit(str(e))


class HistoryFetchWorker(QThread):
    """Fetches zone history from the Pi dashboard API in the background."""
    data_ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, base_url: str, zone_name: str, hours: int = 168):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.zone_name = zone_name
        self.hours = hours

    def run(self):
        try:
            readings = _requests_lib.get(
                f"{self.base_url}/api/readings",
                params={"zone": self.zone_name, "hours": self.hours},
                timeout=15,
            ).json()
            actions = _requests_lib.get(
                f"{self.base_url}/api/actions",
                params={"hours": self.hours},
                timeout=15,
            ).json()
            # Filter actions to this zone only
            actions = [a for a in actions if a.get("zone_name") == self.zone_name]
            self.data_ready.emit({
                "zone_name": self.zone_name,
                "readings": readings,
                "actions": actions,
                "hours": self.hours,
            })
        except Exception as e:
            self.error.emit(str(e))


class AnalyticsWorker(QThread):
    """Runs analytics in background: detect cycles, compute profiles."""
    # Use 'analysis_done' instead of 'finished' to avoid shadowing
    # QThread's built-in finished signal (which caused deleteLater crashes).
    analysis_done = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, db_conn, cfg: dict, days: int = 30):
        super().__init__()
        self.db_conn = db_conn
        self.cfg = cfg
        self.days = days

    def run(self):
        try:
            result = run_full_analysis(self.db_conn, self.cfg, self.days)
            self.analysis_done.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class PollWorker(QThread):
    """Fetches zone data (and optionally runs control logic) in background."""
    zones_ready = pyqtSignal(list)
    state_updated = pyqtSignal(dict)
    weather_ready = pyqtSignal(dict)
    brain_result_ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, api: AirzoneCloudAPI, cfg: dict, state: dict,
                 control_enabled: bool, analytics_conn=None):
        super().__init__()
        self.api = api
        self.cfg = cfg
        self.state = state
        self.control_enabled = control_enabled
        self.analytics_conn = analytics_conn

    def run(self):
        try:
            self.api.ensure_token(self.cfg["email"], self.cfg["password"])
            zones = self.api.get_all_zones()

            # Weather forecast — emit BEFORE zones so outdoor temp is
            # available when _on_zones logs readings to the history DB.
            weather_info = None
            forecast_raw = None
            if self.cfg.get("weather_optimization", False):
                try:
                    from airzone_weather import (
                        get_forecast, compute_warm_window, estimate_cop_savings,
                    )
                    forecast_raw = get_forecast(
                        self.cfg.get("latitude", 44.07),
                        self.cfg.get("longitude", -1.26))
                    weather_info = compute_warm_window(
                        forecast_raw, self.cfg.get("warm_hours_count", 6))
                    t_now = weather_info.get("current_outdoor_temp")
                    t_warm = weather_info.get("avg_warm_temp")
                    if t_now is not None and t_warm is not None:
                        weather_info["cop_info"] = estimate_cop_savings(t_now, t_warm)
                    self.weather_ready.emit(weather_info)
                except Exception:
                    pass

            self.zones_ready.emit(zones)

            new_state = dict(self.state)
            state_changed = False

            if self.control_enabled:
                # Try DP-spread brain first, fall back to legacy
                brain_used = False
                if self.cfg.get("dp_spread_control", True) and self.analytics_conn:
                    try:
                        from airzone_control_brain import (
                            ControlBrain, build_weather_info as build_brain_weather,
                        )
                        brain = ControlBrain(self.analytics_conn)

                        # Build weather info for the brain
                        brain_weather = None
                        if forecast_raw:
                            try:
                                brain_weather = build_brain_weather(forecast_raw)
                            except Exception:
                                pass

                        # Gather Netatmo modules for sensor fusion
                        netatmo_modules = []
                        if self.cfg.get("netatmo_enabled", False):
                            try:
                                from airzone_netatmo import (
                                    get_access_token, get_stations,
                                )
                                cid = self.cfg.get("netatmo_client_id", "")
                                csec = self.cfg.get("netatmo_client_secret", "")
                                nt_token = get_access_token(cid, csec)
                                if nt_token:
                                    stations = get_stations(nt_token)
                                    for mod in stations:
                                        dash = mod.get("dashboard", {})
                                        if dash:
                                            netatmo_modules.append({
                                                "name": mod.get("module_name", ""),
                                                "module_type": mod.get("module_type", ""),
                                                **dash,
                                            })
                            except Exception:
                                pass

                        result = brain.run_cycle(
                            self.api, self.cfg,
                            weather_info=brain_weather,
                            netatmo_modules=netatmo_modules,
                            dry_run=self.cfg.get("dry_run", False),
                        )
                        self.brain_result_ready.emit(result)
                        brain_used = True
                    except Exception as e:
                        log.error("Control brain error, falling back to legacy: %s", e)

                if not brain_used:
                    new_state = check_and_control(
                        self.api, self.cfg, new_state, weather_info=weather_info,
                        analytics_conn=self.analytics_conn)
                    state_changed = True

            # DHW auto-control runs independently (not gated by main toggle)
            if self.cfg.get("dhw_enabled"):
                try:
                    new_state = check_and_control_dhw(
                        self.cfg, new_state,
                        weather_info=weather_info, api=self.api)
                    state_changed = True
                except Exception:
                    pass

            if state_changed:
                self.state_updated.emit(new_state)
        except Exception as e:
            self.error.emit(str(e))


# ── Settings dialog ───────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(380)
        self.cfg = dict(cfg)

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 8, 0)
        scroll.setWidget(container)
        outer.addWidget(scroll)

        thresh = QGroupBox("Humidity Thresholds")
        tform = QFormLayout(thresh)
        self.on_spin = QSpinBox()
        self.on_spin.setRange(30, 100)
        self.on_spin.setSuffix(" %")
        self.on_spin.setValue(cfg.get("humidity_on_threshold", 60))
        self.off_spin = QSpinBox()
        self.off_spin.setRange(20, 99)
        self.off_spin.setSuffix(" %")
        self.off_spin.setValue(cfg.get("humidity_off_threshold", 57))
        self.setpt_spin = QDoubleSpinBox()
        self.setpt_spin.setRange(10.0, 30.0)
        self.setpt_spin.setSingleStep(0.5)
        self.setpt_spin.setSuffix(" °C")
        self.setpt_spin.setValue(cfg.get("heating_setpoint", 22.0))
        self.max_temp_spin = QDoubleSpinBox()
        self.max_temp_spin.setRange(14.0, 25.0)
        self.max_temp_spin.setSingleStep(0.5)
        self.max_temp_spin.setSuffix(" °C")
        self.max_temp_spin.setValue(cfg.get("max_indoor_temp", 18.0))
        self.max_temp_spin.setToolTip(
            "Safety cap: stop heating if room reaches this temperature,\n"
            "even if humidity is still above the OFF threshold.\n"
            "Prevents overheating with underfloor heating.")
        tform.addRow("Turn heating ON above:", self.on_spin)
        tform.addRow("Turn heating OFF below:", self.off_spin)
        tform.addRow("Heating setpoint:", self.setpt_spin)
        tform.addRow("Max room temperature:", self.max_temp_spin)
        layout.addWidget(thresh)

        poll = QGroupBox("Polling")
        pform = QFormLayout(poll)
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(30, 3600)
        self.poll_spin.setSuffix(" s")
        self.poll_spin.setValue(cfg.get("poll_interval_seconds", 300))
        pform.addRow("Check every:", self.poll_spin)
        layout.addWidget(poll)

        weather = QGroupBox("Weather Optimization")
        wform = QFormLayout(weather)
        self.weather_cb = QCheckBox("Enable weather-based scheduling")
        self.weather_cb.setChecked(cfg.get("weather_optimization", False))
        wform.addRow(self.weather_cb)
        self.lat_spin = QDoubleSpinBox()
        self.lat_spin.setRange(-90.0, 90.0)
        self.lat_spin.setDecimals(4)
        self.lat_spin.setValue(cfg.get("latitude", 44.07))
        wform.addRow("Latitude:", self.lat_spin)
        self.lon_spin = QDoubleSpinBox()
        self.lon_spin.setRange(-180.0, 180.0)
        self.lon_spin.setDecimals(4)
        self.lon_spin.setValue(cfg.get("longitude", -1.26))
        wform.addRow("Longitude:", self.lon_spin)
        self.warm_spin = QSpinBox()
        self.warm_spin.setRange(1, 12)
        self.warm_spin.setSuffix(" hours")
        self.warm_spin.setValue(cfg.get("warm_hours_count", 6))
        wform.addRow("Warm window size:", self.warm_spin)
        self.emerg_spin = QSpinBox()
        self.emerg_spin.setRange(50, 100)
        self.emerg_spin.setSuffix(" %")
        self.emerg_spin.setValue(cfg.get("emergency_humidity_threshold", 88))
        wform.addRow("Emergency override above:", self.emerg_spin)
        self.defer_spin = QSpinBox()
        self.defer_spin.setRange(1, 48)
        self.defer_spin.setSuffix(" hours")
        self.defer_spin.setValue(cfg.get("max_defer_hours", 18))
        self.defer_spin.setToolTip(
            "Maximum hours to defer heating for warm window.\n"
            "18h = defer from evening to next afternoon (max savings).\n"
            "6h = only defer within same day.\n"
            "Emergency humidity always overrides.")
        wform.addRow("Max deferral time:", self.defer_spin)
        self.auto_opt_cb = QCheckBox("Auto-optimize warm hours (from analytics)")
        self.auto_opt_cb.setChecked(cfg.get("auto_optimize_warm_hours", False))
        wform.addRow(self.auto_opt_cb)
        self.smart_off_cb = QCheckBox("Smart early-off (learned runoff model)")
        self.smart_off_cb.setChecked(cfg.get("smart_early_off", True))
        self.smart_off_cb.setToolTip(
            "Learns how much humidity keeps dropping after heating stops\n"
            "(runoff effect) and turns off heating earlier to save energy.\n"
            "Uses outdoor dew point to predict runoff per weather condition.")
        wform.addRow(self.smart_off_cb)
        self.smart_off_max_spin = QSpinBox()
        self.smart_off_max_spin.setRange(1, 10)
        self.smart_off_max_spin.setSuffix(" %")
        self.smart_off_max_spin.setValue(cfg.get("smart_early_off_max", 5))
        self.smart_off_max_spin.setToolTip(
            "Safety cap: maximum % above off-threshold to stop early.\n"
            "E.g. 5% means if off-threshold is 65%, heating stops at 70% max.")
        wform.addRow("Max early-off adjustment:", self.smart_off_max_spin)
        self.dew_relax_cb = QCheckBox("Relax ON threshold when dew point is low")
        self.dew_relax_cb.setChecked(cfg.get("dew_point_relax_enabled", True))
        self.dew_relax_cb.setToolTip(
            "When outdoor air is dry (low dew point), moisture ingress is slow.\n"
            "Raise the ON threshold to save energy — less urgency to dehumidify.")
        wform.addRow(self.dew_relax_cb)
        self.dew_relax_spin = QSpinBox()
        self.dew_relax_spin.setRange(1, 10)
        self.dew_relax_spin.setSuffix(" %")
        self.dew_relax_spin.setValue(cfg.get("dew_point_relax_amount", 3))
        self.dew_relax_spin.setToolTip(
            "How much to raise the ON threshold when dew point < 5°C.\n"
            "E.g. 3% means ON threshold 70%→73% (less eager to heat).")
        wform.addRow("Dry air threshold boost:", self.dew_relax_spin)
        layout.addWidget(weather)

        pi_grp = QGroupBox("Pi Dashboard (History Graphs)")
        pform = QFormLayout(pi_grp)
        self.pi_url_edit = QLineEdit()
        self.pi_url_edit.setPlaceholderText("http://192.168.1.50:5000")
        self.pi_url_edit.setText(cfg.get("pi_dashboard_url", ""))
        pform.addRow("Pi URL:", self.pi_url_edit)

        self.pi_test_btn = QPushButton("Test Connection")
        self.pi_test_lbl = QLabel("")
        self.pi_test_lbl.setStyleSheet("font-size: 11px;")
        test_row = QHBoxLayout()
        test_row.addWidget(self.pi_test_btn)
        test_row.addWidget(self.pi_test_lbl)
        test_row.addStretch()
        pform.addRow(test_row)
        self.pi_test_btn.clicked.connect(self._test_pi_connection)
        layout.addWidget(pi_grp)

        # DHW (Hot Water) settings
        dhw_grp = QGroupBox("Hot Water (DHW)")
        dform = QFormLayout(dhw_grp)
        self.dhw_cb = QCheckBox("Enable DHW control")
        self.dhw_cb.setChecked(cfg.get("dhw_enabled", False))
        dform.addRow(self.dhw_cb)

        dhw_note = QLabel("Uses your Airzone Cloud account (works from anywhere)")
        dhw_note.setStyleSheet("color: #888; font-size: 11px; font-style: italic;")
        dform.addRow(dhw_note)

        self.dhw_ip_edit = QLineEdit()
        self.dhw_ip_edit.setPlaceholderText("optional — for local network fallback")
        self.dhw_ip_edit.setText(cfg.get("dhw_local_api", ""))
        dform.addRow("Local API IP:", self.dhw_ip_edit)

        self.dhw_setpt_spin = QDoubleSpinBox()
        self.dhw_setpt_spin.setRange(25.0, 65.0)
        self.dhw_setpt_spin.setSingleStep(0.5)
        self.dhw_setpt_spin.setSuffix(" °C")
        self.dhw_setpt_spin.setValue(cfg.get("dhw_setpoint", 20.0))
        dform.addRow("DHW setpoint:", self.dhw_setpt_spin)
        self.dhw_warm_cb = QCheckBox("Only heat during warmest hours")
        self.dhw_warm_cb.setChecked(cfg.get("dhw_warm_hours_only", True))
        dform.addRow(self.dhw_warm_cb)
        self.dhw_hours_spin = QSpinBox()
        self.dhw_hours_spin.setRange(1, 12)
        self.dhw_hours_spin.setSuffix(" hours")
        self.dhw_hours_spin.setValue(cfg.get("dhw_warm_hours_count", 3))
        dform.addRow("DHW warm window:", self.dhw_hours_spin)

        self.dhw_test_btn = QPushButton("Test Connection")
        self.dhw_test_lbl = QLabel("")
        self.dhw_test_lbl.setStyleSheet("font-size: 11px;")
        dhw_test_row = QHBoxLayout()
        dhw_test_row.addWidget(self.dhw_test_btn)
        dhw_test_row.addWidget(self.dhw_test_lbl)
        dhw_test_row.addStretch()
        dform.addRow(dhw_test_row)
        self.dhw_test_btn.clicked.connect(self._test_dhw_connection)
        layout.addWidget(dhw_grp)

        # Linky energy monitoring
        # ── Netatmo ──
        netatmo_grp = QGroupBox("Netatmo Weather Station")
        nform = QFormLayout(netatmo_grp)
        self.netatmo_cb = QCheckBox("Enable Netatmo integration")
        self.netatmo_cb.setChecked(cfg.get("netatmo_enabled", False))
        nform.addRow(self.netatmo_cb)

        netatmo_note = QLabel(
            "Create an app at dev.netatmo.com to get Client ID/Secret.\n"
            "Then run: python src/airzone_netatmo.py --auth")
        netatmo_note.setStyleSheet(
            "color: #888; font-size: 11px; font-style: italic;")
        netatmo_note.setWordWrap(True)
        nform.addRow(netatmo_note)

        self.netatmo_id_edit = QLineEdit()
        self.netatmo_id_edit.setPlaceholderText("Client ID from dev.netatmo.com")
        self.netatmo_id_edit.setText(cfg.get("netatmo_client_id", ""))
        nform.addRow("Client ID:", self.netatmo_id_edit)

        self.netatmo_secret_edit = QLineEdit()
        self.netatmo_secret_edit.setPlaceholderText("Client Secret")
        self.netatmo_secret_edit.setText(cfg.get("netatmo_client_secret", ""))
        self.netatmo_secret_edit.setEchoMode(QLineEdit.Password)
        nform.addRow("Client Secret:", self.netatmo_secret_edit)

        self.netatmo_test_btn = QPushButton("Test Connection")
        self.netatmo_test_lbl = QLabel("")
        self.netatmo_test_lbl.setStyleSheet("font-size: 11px;")
        netatmo_test_row = QHBoxLayout()
        netatmo_test_row.addWidget(self.netatmo_test_btn)
        netatmo_test_row.addWidget(self.netatmo_test_lbl)
        netatmo_test_row.addStretch()
        nform.addRow(netatmo_test_row)
        self.netatmo_test_btn.clicked.connect(self._test_netatmo_connection)
        layout.addWidget(netatmo_grp)

        # ── Linky ──
        linky_grp = QGroupBox("Energy Monitoring (Linky)")
        lform = QFormLayout(linky_grp)
        self.linky_cb = QCheckBox("Enable Linky energy monitoring")
        self.linky_cb.setChecked(cfg.get("linky_enabled", False))
        lform.addRow(self.linky_cb)

        linky_note = QLabel(
            "Get your token at conso.boris.sh → Enedis login → copy token")
        linky_note.setStyleSheet(
            "color: #888; font-size: 11px; font-style: italic;")
        linky_note.setWordWrap(True)
        lform.addRow(linky_note)

        self.linky_token_edit = QLineEdit()
        self.linky_token_edit.setPlaceholderText("Bearer token from conso.boris.sh")
        self.linky_token_edit.setText(cfg.get("linky_token", ""))
        self.linky_token_edit.setEchoMode(QLineEdit.Password)
        lform.addRow("Token:", self.linky_token_edit)

        self.linky_prm_edit = QLineEdit()
        self.linky_prm_edit.setPlaceholderText("14-digit meter ID (PRM)")
        self.linky_prm_edit.setText(cfg.get("linky_prm", ""))
        self.linky_prm_edit.setMaxLength(14)
        lform.addRow("PRM:", self.linky_prm_edit)

        self.linky_test_btn = QPushButton("Test Connection")
        self.linky_test_lbl = QLabel("")
        self.linky_test_lbl.setStyleSheet("font-size: 11px;")
        linky_test_row = QHBoxLayout()
        linky_test_row.addWidget(self.linky_test_btn)
        linky_test_row.addWidget(self.linky_test_lbl)
        linky_test_row.addStretch()
        lform.addRow(linky_test_row)
        self.linky_test_btn.clicked.connect(self._test_linky_connection)
        layout.addWidget(linky_grp)

        # Secure storage indicator
        try:
            from airzone_secrets import secrets as _sec
            backend = _sec.backend_name
            sec_lbl = QLabel(f"📁 Credentials stored in: {backend}")
            sec_lbl.setStyleSheet(
                "color: #27ae60; font-size: 11px; padding: 4px;")
            layout.addWidget(sec_lbl)
        except Exception:
            pass

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)  # outside scroll area — always visible

    def _test_pi_connection(self):
        url = self.pi_url_edit.text().strip().rstrip("/")
        if not url:
            self.pi_test_lbl.setText("Enter a URL first")
            self.pi_test_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")
            return
        self.pi_test_lbl.setText("Testing...")
        self.pi_test_lbl.setStyleSheet("color: grey; font-size: 11px;")
        QApplication.processEvents()
        try:
            resp = _requests_lib.get(f"{url}/api/stats", timeout=5)
            resp.raise_for_status()
            stats = resp.json()
            n = stats.get("total_readings", 0)
            self.pi_test_lbl.setText(f"Connected ({n} readings in DB)")
            self.pi_test_lbl.setStyleSheet("color: #27ae60; font-size: 11px;")
        except Exception as e:
            self.pi_test_lbl.setText(f"Failed: {e}")
            self.pi_test_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")

    def _test_dhw_connection(self):
        ip = self.dhw_ip_edit.text().strip()
        self.dhw_test_lbl.setText("Testing...")
        self.dhw_test_lbl.setStyleSheet("color: grey; font-size: 11px;")
        QApplication.processEvents()

        # Try local API first if IP is provided
        if ip:
            try:
                status = dhw_get_status(ip, timeout=5)
                tank = status.get("acs_temp")
                power = "ON" if status.get("acs_power") else "OFF"
                self.dhw_test_lbl.setText(f"Local API OK (tank {tank}°C, {power})")
                self.dhw_test_lbl.setStyleSheet("color: #27ae60; font-size: 11px;")
                return
            except Exception as e:
                self.dhw_test_lbl.setText(f"Local failed: {e} — trying Cloud...")
                self.dhw_test_lbl.setStyleSheet("color: #e67e22; font-size: 11px;")
                QApplication.processEvents()

        # Try Cloud API
        try:
            api = self.parent()
            while api and not isinstance(api, MainWindow):
                api = api.parent()
            if api:
                status = api.api.get_dhw_status()
                if status:
                    tank = status.get("acs_temp")
                    power = "ON" if status.get("acs_power") else "OFF"
                    tank_s = f"{tank}°C" if tank is not None else "?"
                    self.dhw_test_lbl.setText(f"Cloud API OK (tank {tank_s}, {power})")
                    self.dhw_test_lbl.setStyleSheet("color: #27ae60; font-size: 11px;")
                else:
                    self.dhw_test_lbl.setText("No DHW device found in Cloud account")
                    self.dhw_test_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")
            else:
                self.dhw_test_lbl.setText("Not logged in yet")
                self.dhw_test_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")
        except Exception as e:
            self.dhw_test_lbl.setText(f"Cloud API failed: {e}")
            self.dhw_test_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")

    def _test_netatmo_connection(self):
        client_id = self.netatmo_id_edit.text().strip()
        client_secret = self.netatmo_secret_edit.text().strip()
        if not client_id or not client_secret:
            self.netatmo_test_lbl.setText("Enter Client ID and Secret first")
            self.netatmo_test_lbl.setStyleSheet(
                "color: #e74c3c; font-size: 11px;")
            return
        self.netatmo_test_lbl.setText("Testing...")
        self.netatmo_test_lbl.setStyleSheet("color: grey; font-size: 11px;")
        QApplication.processEvents()
        try:
            from airzone_netatmo import get_access_token, get_stations
            token = get_access_token(client_id, client_secret)
            if not token:
                self.netatmo_test_lbl.setText(
                    "No token — run: python src/airzone_netatmo.py --auth")
                self.netatmo_test_lbl.setStyleSheet(
                    "color: #e67e22; font-size: 11px;")
                return
            stations = get_stations(token)
            if stations:
                names = [s["module_name"] for s in stations]
                self.netatmo_test_lbl.setText(
                    f"Connected ({len(stations)} modules: {', '.join(names)})")
                self.netatmo_test_lbl.setStyleSheet(
                    "color: #27ae60; font-size: 11px;")
            else:
                self.netatmo_test_lbl.setText("Connected but no stations found")
                self.netatmo_test_lbl.setStyleSheet(
                    "color: #e67e22; font-size: 11px;")
        except Exception as e:
            self.netatmo_test_lbl.setText(f"Failed: {e}")
            self.netatmo_test_lbl.setStyleSheet(
                "color: #e74c3c; font-size: 11px;")

    def _test_linky_connection(self):
        token = self.linky_token_edit.text().strip()
        prm = self.linky_prm_edit.text().strip()
        if not token or not prm:
            self.linky_test_lbl.setText("Enter token and PRM first")
            self.linky_test_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")
            return
        self.linky_test_lbl.setText("Testing...")
        self.linky_test_lbl.setStyleSheet("color: grey; font-size: 11px;")
        QApplication.processEvents()
        try:
            from datetime import date, timedelta
            yesterday = date.today() - timedelta(days=2)
            today = date.today()
            readings = fetch_load_curve(token, prm, yesterday, today)
            if readings:
                self.linky_test_lbl.setText(
                    f"Connected ({len(readings)} readings)")
                self.linky_test_lbl.setStyleSheet(
                    "color: #27ae60; font-size: 11px;")
            else:
                self.linky_test_lbl.setText(
                    "Connected but no recent data (may need 24h)")
                self.linky_test_lbl.setStyleSheet(
                    "color: #e67e22; font-size: 11px;")
        except Exception as e:
            self.linky_test_lbl.setText(f"Failed: {e}")
            self.linky_test_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")

    def result_config(self) -> dict:
        return {
            **self.cfg,
            "humidity_on_threshold": self.on_spin.value(),
            "humidity_off_threshold": self.off_spin.value(),
            "heating_setpoint": self.setpt_spin.value(),
            "poll_interval_seconds": self.poll_spin.value(),
            "weather_optimization": self.weather_cb.isChecked(),
            "latitude": self.lat_spin.value(),
            "longitude": self.lon_spin.value(),
            "warm_hours_count": self.warm_spin.value(),
            "emergency_humidity_threshold": self.emerg_spin.value(),
            "max_defer_hours": self.defer_spin.value(),
            "pi_dashboard_url": self.pi_url_edit.text().strip(),
            "dhw_enabled": self.dhw_cb.isChecked(),
            "dhw_local_api": self.dhw_ip_edit.text().strip(),
            "dhw_setpoint": self.dhw_setpt_spin.value(),
            "dhw_warm_hours_only": self.dhw_warm_cb.isChecked(),
            "dhw_warm_hours_count": self.dhw_hours_spin.value(),
            "auto_optimize_warm_hours": self.auto_opt_cb.isChecked(),
            "max_indoor_temp": self.max_temp_spin.value(),
            "smart_early_off": self.smart_off_cb.isChecked(),
            "smart_early_off_max": self.smart_off_max_spin.value(),
            "dew_point_relax_enabled": self.dew_relax_cb.isChecked(),
            "dew_point_relax_amount": self.dew_relax_spin.value(),
            "netatmo_enabled": self.netatmo_cb.isChecked(),
            "netatmo_client_id": self.netatmo_id_edit.text().strip(),
            "netatmo_client_secret": self.netatmo_secret_edit.text().strip(),
            "linky_enabled": self.linky_cb.isChecked(),
            "linky_token": self.linky_token_edit.text().strip(),
            "linky_prm": self.linky_prm_edit.text().strip(),
        }


# ── Zone history panel ────────────────────────────────────────────────────────

class ZoneHistoryPanel(QWidget):
    """Matplotlib graphs showing zone history fetched from the Pi dashboard."""
    fetch_requested = pyqtSignal(str, int)  # zone_name, hours

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVisible(False)
        self._current_zone = ""
        self._current_hours = 168

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)

        # Header bar
        hdr = QHBoxLayout()
        self.zone_label = QLabel("")
        f = self.zone_label.font()
        f.setBold(True)
        f.setPointSize(16)
        self.zone_label.setFont(f)
        hdr.addWidget(self.zone_label)
        hdr.addStretch()

        self._range_btns = []
        for label, hours in [("24h", 24), ("3 days", 72), ("7 days", 168), ("30 days", 720)]:
            btn = QPushButton(label)
            btn.setFixedHeight(24)
            btn.setCheckable(True)
            btn.setStyleSheet(
                "QPushButton { border: 1px solid #bdc3c7; border-radius: 3px; "
                "padding: 2px 8px; font-size: 11px; }"
                "QPushButton:checked { background-color: #2980b9; color: white; "
                "border-color: #2980b9; }"
            )
            btn.clicked.connect(lambda _, h=hours, b=btn: self._on_range(h, b))
            hdr.addWidget(btn)
            self._range_btns.append((btn, hours))
        # Default to 7 days
        self._range_btns[2][0].setChecked(True)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet(
            "QPushButton { border: none; font-size: 14px; color: #888; }"
            "QPushButton:hover { color: #e74c3c; }"
        )
        close_btn.clicked.connect(lambda: self.setVisible(False))
        hdr.addWidget(close_btn)
        layout.addLayout(hdr)

        # Status label
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: grey; font-size: 11px;")
        layout.addWidget(self.status_label)

        # Matplotlib canvas
        if HAS_MATPLOTLIB:
            self.fig = Figure(figsize=(8, 3), dpi=100)
            self.fig.set_facecolor("#fafafa")
            self.canvas = FigureCanvasQTAgg(self.fig)
            self.canvas.setFixedHeight(250)
            layout.addWidget(self.canvas)
        else:
            layout.addWidget(QLabel("matplotlib not installed — cannot show graphs"))

    def _on_range(self, hours: int, btn):
        for b, _ in self._range_btns:
            b.setChecked(False)
        btn.setChecked(True)
        self._current_hours = hours
        if self._current_zone:
            self.fetch_requested.emit(self._current_zone, hours)

    def show_zone(self, zone_name: str):
        """Show the panel for a zone and request data fetch."""
        if self._current_zone == zone_name and self.isVisible():
            self.setVisible(False)
            self._current_zone = ""
            return
        self._current_zone = zone_name
        short = zone_name.split("/")[-1] if "/" in zone_name else zone_name
        self.zone_label.setText(f"History: {short}")
        self.status_label.setText("Loading...")
        self.setVisible(True)
        if HAS_MATPLOTLIB:
            self.fig.clear()
            self.canvas.draw()
        self.fetch_requested.emit(zone_name, self._current_hours)

    def update_data(self, data: dict):
        """Redraw the combined graph with all data series."""
        if not HAS_MATPLOTLIB:
            return
        readings = data.get("readings", [])
        if not readings:
            self.status_label.setText("No history data available for this zone.")
            self.fig.clear()
            self.canvas.draw()
            return

        self.status_label.setText(
            f"{len(readings)} readings over {data.get('hours', '?')} hours")

        # Parse timestamps and values
        times = []
        temps = []
        hums = []
        powers = []
        outdoors = []
        dew_pts = []
        out_hums = []
        for r in readings:
            try:
                t = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            except (ValueError, KeyError):
                continue
            times.append(t)
            temps.append(r.get("temperature"))
            hums.append(r.get("humidity"))
            powers.append(r.get("power", 0))
            outdoors.append(r.get("outdoor_temp"))
            dew_pts.append(r.get("outdoor_dew_point"))
            out_hums.append(r.get("outdoor_humidity"))

        # Calculate indoor DP spread: indoor_temp − dewpoint(indoor_temp, indoor_rh)
        import math as _math
        dp_spreads = []
        for t_v, h_v in zip(temps, hums):
            if t_v is not None and h_v is not None and h_v > 0:
                try:
                    g = _math.log(h_v / 100) + (17.625 * t_v) / (243.04 + t_v)
                    dp = (243.04 * g) / (17.625 - g)
                    dp_spreads.append(round(t_v - dp, 1))
                except Exception:
                    dp_spreads.append(None)
            else:
                dp_spreads.append(None)

        self.fig.clear()
        ax_temp = self.fig.add_subplot(111)
        ax_hum = ax_temp.twinx()

        # Background shading for power ON periods
        for i in range(len(times) - 1):
            if powers[i]:
                ax_temp.axvspan(times[i], times[i + 1],
                                alpha=0.10, color="#e74c3c")

        # Left axis: temperatures (°C)
        valid = [(t, v) for t, v in zip(times, temps) if v is not None]
        if valid:
            ln1 = ax_temp.plot([t for t, _ in valid], [v for _, v in valid],
                               color="#2980b9", linewidth=1.5, label="Indoor temp")
        else:
            ln1 = []

        valid = [(t, v) for t, v in zip(times, outdoors) if v is not None]
        if valid:
            ln2 = ax_temp.plot([t for t, _ in valid], [v for _, v in valid],
                               color="#f39c12", linewidth=1.5, alpha=0.8,
                               label="Outdoor temp")
        else:
            ln2 = []

        valid = [(t, v) for t, v in zip(times, dew_pts) if v is not None]
        if valid:
            ln4 = ax_temp.plot([t for t, _ in valid], [v for _, v in valid],
                               color="#00bcd4", linewidth=1.2, alpha=0.7,
                               linestyle="--", label="Dew point")
        else:
            ln4 = []

        # DP Spread line — the core control metric (orange, left axis)
        valid = [(t, v) for t, v in zip(times, dp_spreads) if v is not None]
        if valid:
            ln6 = ax_temp.plot([t for t, _ in valid], [v for _, v in valid],
                               color="#e59230", linewidth=2.0, alpha=0.95,
                               label="DP Spread", zorder=5)
            # Threshold reference lines: 4°C = ON zone, 6°C = safe zone
            ax_temp.axhline(y=4, color="#e74c3c", linestyle=":", linewidth=1.0,
                            alpha=0.55, zorder=4)
            ax_temp.axhline(y=6, color="#f39c12", linestyle=":", linewidth=1.0,
                            alpha=0.55, zorder=4)
        else:
            ln6 = []

        ax_temp.set_ylabel("Temperature / DP Spread (°C)", fontsize=9, color="#2980b9")
        ax_temp.tick_params(axis="y", labelsize=8, labelcolor="#2980b9")

        # Right axis: humidity (%)
        valid = [(t, v) for t, v in zip(times, hums) if v is not None]
        if valid:
            ln3 = ax_hum.plot([t for t, _ in valid], [v for _, v in valid],
                              color="#27ae60", linewidth=1.5, label="Humidity")
        else:
            ln3 = []

        # Outdoor humidity (right axis, same scale)
        valid = [(t, v) for t, v in zip(times, out_hums) if v is not None]
        if valid:
            ln5 = ax_hum.plot([t for t, _ in valid], [v for _, v in valid],
                              color="#9b59b6", linewidth=1.2, alpha=0.7,
                              linestyle=":", label="Outdoor humidity")
        else:
            ln5 = []

        # Threshold lines (use config values)
        ax_hum.axhline(y=70, color="#e74c3c", linestyle="--", linewidth=0.8,
                        alpha=0.5)
        ax_hum.axhline(y=65, color="#27ae60", linestyle="--", linewidth=0.8,
                        alpha=0.5)
        ax_hum.set_ylabel("Humidity (%)", fontsize=9, color="#27ae60")
        ax_hum.set_ylim(60, 100)
        ax_hum.tick_params(axis="y", labelsize=8, labelcolor="#27ae60")

        # Combined legend
        lines = ((ln1 or []) + (ln2 or []) + (ln4 or []) + (ln6 or [])
                 + (ln3 or []) + (ln5 or []))
        if lines:
            import matplotlib.patches as mpatches
            heating_patch = mpatches.Patch(color="#e74c3c", alpha=0.15,
                                           label="Heating ON")
            labels = [l.get_label() for l in lines]
            ax_temp.legend(lines + [heating_patch], labels + ["Heating ON"],
                           fontsize=7, loc="upper left")

        # X-axis formatting: "07 Mar 2026 14:00"
        ax_temp.xaxis.set_major_formatter(
            mdates.DateFormatter("%d %b %Y %H:%M"))
        ax_temp.tick_params(axis="x", labelsize=7)
        ax_temp.grid(True, alpha=0.2)
        self.fig.autofmt_xdate(rotation=30, ha="right")
        self.fig.tight_layout()
        self.canvas.draw()

    def show_error(self, msg: str):
        self.status_label.setText(f"Error: {msg}")


# ── DHW background worker ─────────────────────────────────────────────────────

class DHWStatusWorker(QThread):
    """Fetches DHW status — prefers Cloud API, falls back to local API."""
    status_ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, api: AirzoneCloudAPI = None, ip: str = ""):
        super().__init__()
        self.api = api
        self.ip = ip

    def run(self):
        try:
            if self.api is not None:
                status = self.api.get_dhw_status()
                if not status:
                    self.error.emit("No DHW device found in your Airzone Cloud account")
                    return
                self.status_ready.emit(status)
            elif self.ip:
                status = dhw_get_status(self.ip)
                self.status_ready.emit(status)
            else:
                self.error.emit("DHW not configured")
        except Exception as e:
            self.error.emit(str(e))


class DHWCommandWorker(QThread):
    """Sends a DHW power/setpoint command — prefers Cloud API."""
    done = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, api: AirzoneCloudAPI = None, ip: str = "",
                 device_id: str = "", installation_id: str = "",
                 power: int = None, setpoint: float = None):
        super().__init__()
        self.api = api
        self.ip = ip
        self.device_id = device_id
        self.installation_id = installation_id
        self.power = power
        self.setpoint = setpoint

    def run(self):
        try:
            if self.api is not None and self.device_id:
                p = True if self.power == 1 else (False if self.power == 0 else None)
                sp = int(self.setpoint) if self.setpoint is not None else None
                self.api.set_dhw(self.device_id, self.installation_id,
                                 power=p, setpoint=sp)
            elif self.ip:
                dhw_set(self.ip, power=self.power, setpoint=self.setpoint)
            else:
                self.error.emit("DHW not configured")
                return
            self.done.emit()
        except Exception as e:
            self.error.emit(str(e))


# ── DHW tab ───────────────────────────────────────────────────────────────────

class DHWTab(QWidget):
    """Hot Water control tab with status display, on/off, and setpoint."""
    status_message = pyqtSignal(str)

    def __init__(self, cfg: dict, state: dict, api: AirzoneCloudAPI = None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.state = state
        self.api = api
        self._worker = None
        self._cmd_workers = []
        self._dhw_device_id = ""
        self._dhw_installation_id = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(12)

        # ── Status section ──
        status_box = QGroupBox("Current Status")
        status_lay = QVBoxLayout(status_box)

        # Connection status
        self.conn_lbl = QLabel("Not yet refreshed")
        self.conn_lbl.setStyleSheet("color: grey; font-size: 11px;")
        status_lay.addWidget(self.conn_lbl)

        # Tank temperature (big display)
        tank_row = QHBoxLayout()
        tank_title = QLabel("Tank Temperature")
        tank_title.setStyleSheet("font-size: 13px; color: #666;")
        tank_row.addWidget(tank_title)
        tank_row.addStretch()
        self.tank_temp_lbl = QLabel("-- °C")
        f = self.tank_temp_lbl.font()
        f.setPointSize(28)
        f.setBold(True)
        self.tank_temp_lbl.setFont(f)
        self.tank_temp_lbl.setStyleSheet("color: #2980b9;")
        tank_row.addWidget(self.tank_temp_lbl)
        status_lay.addLayout(tank_row)

        # Power and setpoint row
        info_row = QHBoxLayout()
        self.power_lbl = QLabel("Power: --")
        self.power_lbl.setStyleSheet("font-size: 13px;")
        info_row.addWidget(self.power_lbl)
        info_row.addStretch()
        self.setpoint_lbl = QLabel("Setpoint: -- °C")
        self.setpoint_lbl.setStyleSheet("font-size: 13px;")
        info_row.addWidget(self.setpoint_lbl)
        status_lay.addLayout(info_row)

        layout.addWidget(status_box)

        # ── Controls section ──
        ctrl_box = QGroupBox("Controls")
        ctrl_lay = QVBoxLayout(ctrl_box)

        # Power buttons
        pwr_row = QHBoxLayout()
        pwr_label = QLabel("DHW Power:")
        pwr_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        pwr_row.addWidget(pwr_label)
        pwr_row.addStretch()

        self.btn_on = QPushButton("ON")
        self.btn_on.setFixedSize(80, 32)
        self.btn_on.setStyleSheet(
            "QPushButton { border: 1px solid #bdc3c7; border-radius: 4px; "
            "font-weight: bold; font-size: 13px; }"
            "QPushButton:hover { background-color: #eafaf1; }"
        )
        self.btn_on.clicked.connect(lambda: self._send_power(1))
        pwr_row.addWidget(self.btn_on)

        self.btn_off = QPushButton("OFF")
        self.btn_off.setFixedSize(80, 32)
        self.btn_off.setStyleSheet(
            "QPushButton { border: 1px solid #bdc3c7; border-radius: 4px; "
            "font-weight: bold; font-size: 13px; }"
            "QPushButton:hover { background-color: #fdecea; }"
        )
        self.btn_off.clicked.connect(lambda: self._send_power(0))
        pwr_row.addWidget(self.btn_off)

        ctrl_lay.addLayout(pwr_row)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        ctrl_lay.addWidget(sep)

        # Setpoint control
        sp_row = QHBoxLayout()
        sp_label = QLabel("Target Temperature:")
        sp_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        sp_row.addWidget(sp_label)
        sp_row.addStretch()

        self.setpoint_spin = QDoubleSpinBox()
        self.setpoint_spin.setRange(25.0, 65.0)
        self.setpoint_spin.setSingleStep(0.5)
        self.setpoint_spin.setSuffix(" °C")
        self.setpoint_spin.setValue(cfg.get("dhw_setpoint", 20.0))
        self.setpoint_spin.setFixedWidth(100)
        self.setpoint_spin.setStyleSheet("font-size: 13px;")
        sp_row.addWidget(self.setpoint_spin)

        self.set_btn = QPushButton("Set")
        self.set_btn.setFixedSize(60, 28)
        self.set_btn.setStyleSheet(
            "QPushButton { background-color: #2980b9; color: white; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #3498db; }"
        )
        self.set_btn.clicked.connect(self._send_setpoint)
        sp_row.addWidget(self.set_btn)

        ctrl_lay.addLayout(sp_row)

        # Note about 40°C minimum
        note = QLabel("Note: The heat pump may enforce its own minimum setpoint.")
        note.setStyleSheet("color: #888; font-size: 11px; font-style: italic;")
        ctrl_lay.addWidget(note)

        layout.addWidget(ctrl_box)

        # ── Schedule section ──
        sched_box = QGroupBox("Weather-Based Schedule")
        sched_lay = QVBoxLayout(sched_box)

        self.sched_enabled_lbl = QLabel("")
        self.sched_enabled_lbl.setStyleSheet("font-size: 12px;")
        sched_lay.addWidget(self.sched_enabled_lbl)

        sched_info = QHBoxLayout()
        self.outdoor_lbl = QLabel("Outdoor: -- °C")
        self.outdoor_lbl.setStyleSheet("font-size: 13px;")
        sched_info.addWidget(self.outdoor_lbl)
        sched_info.addStretch()
        self.window_lbl = QLabel("Warm window: --")
        self.window_lbl.setStyleSheet("font-size: 13px;")
        sched_info.addWidget(self.window_lbl)
        sched_lay.addLayout(sched_info)

        self.sched_status_lbl = QLabel("")
        self.sched_status_lbl.setStyleSheet(
            "font-size: 13px; font-weight: bold; padding: 4px;")
        sched_lay.addWidget(self.sched_status_lbl)

        layout.addWidget(sched_box)

        # ── Refresh button ──
        bot = QHBoxLayout()
        bot.addStretch()
        self.refresh_btn = QPushButton("⟳  Refresh")
        self.refresh_btn.clicked.connect(self.refresh_status)
        bot.addWidget(self.refresh_btn)
        layout.addLayout(bot)

        layout.addStretch()

        # Initial state
        self._update_schedule_display()

    def refresh_status(self):
        """Fetch current DHW status — Cloud API preferred, local API fallback."""
        ip = self.cfg.get("dhw_local_api", "")
        use_cloud = self.api is not None

        if not use_cloud and not ip:
            self.conn_lbl.setText("Not configured — enable DHW in Settings")
            self.conn_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")
            return

        self.refresh_btn.setEnabled(False)
        source = "Airzone Cloud" if use_cloud else ip
        self.conn_lbl.setText(f"Connecting to {source}...")
        self.conn_lbl.setStyleSheet("color: grey; font-size: 11px;")

        self._worker = DHWStatusWorker(api=self.api if use_cloud else None,
                                        ip=ip if not use_cloud else "")
        self._worker.status_ready.connect(self._on_status)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(lambda: self.refresh_btn.setEnabled(True))
        self._worker.start()

    def _on_status(self, status: dict):
        now = datetime.now().strftime("%H:%M:%S")
        # Store device IDs for Cloud API commands
        if status.get("_device_id"):
            self._dhw_device_id = status["_device_id"]
            self._dhw_installation_id = status.get("_installation_id", "")
        source = "Airzone Cloud" if self.api else self.cfg.get('dhw_local_api', '')
        self.conn_lbl.setText(f"Connected via {source}  (updated {now})")
        self.conn_lbl.setStyleSheet("color: #27ae60; font-size: 11px;")

        tank = status.get("acs_temp")
        power = status.get("acs_power")
        setpt = status.get("acs_setpoint")

        # Tank temperature
        if tank is not None:
            self.tank_temp_lbl.setText(f"{tank:.1f} °C")
            if tank >= 45:
                self.tank_temp_lbl.setStyleSheet("color: #e74c3c; font-size: 28px;")
            elif tank >= 30:
                self.tank_temp_lbl.setStyleSheet("color: #e67e22; font-size: 28px;")
            else:
                self.tank_temp_lbl.setStyleSheet("color: #2980b9; font-size: 28px;")
        else:
            self.tank_temp_lbl.setText("-- °C")
            self.tank_temp_lbl.setStyleSheet("color: #2980b9; font-size: 28px;")

        # Power
        if power is not None:
            is_on = bool(power)
            self.power_lbl.setText(f"Power: {'ON' if is_on else 'OFF'}")
            self.power_lbl.setStyleSheet(
                f"font-size: 13px; color: {'#27ae60' if is_on else '#e74c3c'}; font-weight: bold;")
            # Highlight the active button
            if is_on:
                self.btn_on.setStyleSheet(
                    "QPushButton { background-color: #27ae60; color: white; "
                    "border-radius: 4px; font-weight: bold; font-size: 13px; }")
                self.btn_off.setStyleSheet(
                    "QPushButton { border: 1px solid #bdc3c7; border-radius: 4px; "
                    "font-weight: bold; font-size: 13px; }"
                    "QPushButton:hover { background-color: #fdecea; }")
            else:
                self.btn_off.setStyleSheet(
                    "QPushButton { background-color: #c0392b; color: white; "
                    "border-radius: 4px; font-weight: bold; font-size: 13px; }")
                self.btn_on.setStyleSheet(
                    "QPushButton { border: 1px solid #bdc3c7; border-radius: 4px; "
                    "font-weight: bold; font-size: 13px; }"
                    "QPushButton:hover { background-color: #eafaf1; }")
        else:
            self.power_lbl.setText("Power: --")

        # Setpoint
        if setpt is not None:
            self.setpoint_lbl.setText(f"Setpoint: {setpt:.1f} °C")
        else:
            self.setpoint_lbl.setText("Setpoint: -- °C")

        self.status_message.emit(f"DHW status refreshed — tank {tank:.1f}°C" if tank else "DHW status refreshed")

    def _on_error(self, msg: str):
        self.conn_lbl.setText(f"Error: {msg}")
        self.conn_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")
        self.status_message.emit(f"DHW error: {msg}")

    def _send_power(self, power: int):
        ip = self.cfg.get("dhw_local_api", "")
        use_cloud = self.api is not None
        if not use_cloud and not ip:
            self.status_message.emit("DHW not configured — enable in Settings")
            return
        action = "ON" if power else "OFF"
        self.status_message.emit(f"Turning DHW {action}...")
        w = DHWCommandWorker(
            api=self.api if use_cloud else None,
            ip=ip if not use_cloud else "",
            device_id=self._dhw_device_id,
            installation_id=self._dhw_installation_id,
            power=power,
        )
        w.done.connect(self.refresh_status)
        w.done.connect(lambda: self.status_message.emit(f"DHW switched {action}"))
        w.error.connect(lambda m: self.status_message.emit(f"DHW command error: {m}"))
        w.start()
        self._cmd_workers = [x for x in self._cmd_workers if x.isRunning()]
        self._cmd_workers.append(w)

    def _send_setpoint(self):
        ip = self.cfg.get("dhw_local_api", "")
        use_cloud = self.api is not None
        if not use_cloud and not ip:
            self.status_message.emit("DHW not configured — enable in Settings")
            return
        temp = self.setpoint_spin.value()
        self.status_message.emit(f"Setting DHW to {temp:.1f} °C...")
        w = DHWCommandWorker(
            api=self.api if use_cloud else None,
            ip=ip if not use_cloud else "",
            device_id=self._dhw_device_id,
            installation_id=self._dhw_installation_id,
            power=1, setpoint=temp,
        )
        w.done.connect(self.refresh_status)
        w.done.connect(lambda: self.status_message.emit(f"DHW setpoint changed to {temp:.1f}°C"))
        w.error.connect(lambda m: self.status_message.emit(f"DHW command error: {m}"))
        w.start()
        self._cmd_workers = [x for x in self._cmd_workers if x.isRunning()]
        self._cmd_workers.append(w)

    def _update_schedule_display(self):
        """Update the schedule section from current state and config."""
        dhw_enabled = self.cfg.get("dhw_enabled", False)
        warm_only = self.cfg.get("dhw_warm_hours_only", True)
        hours_count = self.cfg.get("dhw_warm_hours_count", 3)

        if not dhw_enabled:
            self.sched_enabled_lbl.setText("Auto-schedule: DISABLED (enable in Settings)")
            self.sched_enabled_lbl.setStyleSheet("color: #888; font-size: 12px;")
            self.sched_status_lbl.setText("")
            return

        if not warm_only:
            self.sched_enabled_lbl.setText("Mode: Always ON (weather scheduling disabled)")
            self.sched_enabled_lbl.setStyleSheet("color: #e67e22; font-size: 12px;")
            self.sched_status_lbl.setText("")
            return

        self.sched_enabled_lbl.setText(
            f"Mode: Warmest {hours_count} hours only (weather-optimized)")
        self.sched_enabled_lbl.setStyleSheet("color: #27ae60; font-size: 12px;")

    def update_weather(self, dhw_weather: dict):
        """Update schedule display with weather data from poll worker."""
        if not dhw_weather:
            return

        outdoor = dhw_weather.get("outdoor_temp")
        if outdoor is not None:
            self.outdoor_lbl.setText(f"Outdoor: {outdoor:.1f} °C")
        else:
            self.outdoor_lbl.setText("Outdoor: -- °C")

        start = dhw_weather.get("next_warm_start")
        end = dhw_weather.get("next_warm_end")
        is_warm = dhw_weather.get("is_warm_now", False)

        if start and end:
            try:
                s = datetime.fromisoformat(start).strftime("%H:%M")
                e = datetime.fromisoformat(end).strftime("%H:%M")
            except (ValueError, TypeError):
                s, e = "?", "?"
            prefix = "NOW" if is_warm else "Next"
            self.window_lbl.setText(f"Warm window ({prefix}): {s}–{e}")
        else:
            self.window_lbl.setText("Warm window: --")

        action = dhw_weather.get("dhw_last_action")
        overrides = dhw_weather.get("dhw_override_count", 0)

        if is_warm:
            self.sched_status_lbl.setText("HEATING — warm window active")
            self.sched_status_lbl.setStyleSheet(
                "font-size: 13px; font-weight: bold; color: #27ae60; padding: 4px;")
        elif action == "off_backed_off":
            self.sched_status_lbl.setText(
                "BACKED OFF — heat pump FTC overrides app control")
            self.sched_status_lbl.setStyleSheet(
                "font-size: 13px; font-weight: bold; color: #e74c3c; padding: 4px;")
        else:
            suffix = f" (FTC overrode {overrides}×)" if overrides else ""
            self.sched_status_lbl.setText(
                f"STANDBY — waiting for warm window{suffix}")
            self.sched_status_lbl.setStyleSheet(
                "font-size: 13px; font-weight: bold; color: #e67e22; padding: 4px;")

    def update_config(self, cfg: dict):
        """Called when settings change."""
        self.cfg = cfg
        self.setpoint_spin.setValue(cfg.get("dhw_setpoint", 20.0))
        self._update_schedule_display()


# ── Login window ──────────────────────────────────────────────────────────────

class LoginWindow(QWidget):
    logged_in = pyqtSignal(str, str)   # email, password

    def __init__(self, api: AirzoneCloudAPI, cfg: dict):
        super().__init__()
        self.api = api
        self.cfg = cfg
        self.worker = None

        self.setWindowTitle("Airzone — Sign In")
        self.setFixedSize(360, 280)

        root = QVBoxLayout(self)
        root.setContentsMargins(40, 30, 40, 30)
        root.setSpacing(14)

        title = QLabel("Airzone Cloud")
        f = title.font()
        f.setPointSize(18)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)

        sub = QLabel("Sign in with your Airzone account")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet("color: grey; font-size: 12px;")
        root.addWidget(sub)

        root.addSpacing(6)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("Email address")
        self.email_edit.setText(cfg.get("email", ""))
        self.email_edit.setMinimumHeight(32)
        root.addWidget(self.email_edit)

        self.pass_edit = QLineEdit()
        self.pass_edit.setPlaceholderText("Password")
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.setMinimumHeight(32)
        self.pass_edit.returnPressed.connect(self._do_login)
        root.addWidget(self.pass_edit)

        self.remember_cb = QCheckBox("Remember credentials")
        self.remember_cb.setChecked(bool(cfg.get("email")))
        root.addWidget(self.remember_cb)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #e74c3c; font-size: 12px;")
        self.error_label.setAlignment(Qt.AlignCenter)
        self.error_label.setWordWrap(True)
        root.addWidget(self.error_label)

        self.login_btn = QPushButton("Sign In")
        self.login_btn.setMinimumHeight(36)
        self.login_btn.setDefault(True)
        self.login_btn.setStyleSheet(
            "QPushButton { background-color: #2980b9; color: white; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #3498db; }"
            "QPushButton:disabled { background-color: #bdc3c7; }"
        )
        self.login_btn.clicked.connect(self._do_login)
        root.addWidget(self.login_btn)

    def _do_login(self):
        email = self.email_edit.text().strip()
        password = self.pass_edit.text()
        if not email or not password:
            self.error_label.setText("Please enter your email and password.")
            return

        self.error_label.setText("")
        self.login_btn.setEnabled(False)
        self.login_btn.setText("Signing in…")

        self.worker = LoginWorker(self.api, email, password)
        self.worker.success.connect(lambda: self._on_success(email, password))
        self.worker.failure.connect(self._on_failure)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def _on_success(self, email: str, password: str):
        self.login_btn.setEnabled(True)
        self.login_btn.setText("Sign In")
        if self.remember_cb.isChecked():
            self.cfg["email"] = email
            self.cfg["password"] = password
            _save_config_secure(self.cfg)
        self.logged_in.emit(email, password)

    def _on_failure(self, msg: str):
        self.login_btn.setEnabled(True)
        self.login_btn.setText("Sign In")
        self.error_label.setText(f"Login failed: {msg}")


# ── Analytics tab ─────────────────────────────────────────────────────────────

class AnalyticsTab(QWidget):
    """Shows zone drying profiles, heating cycles, and warm hours recommendation."""
    apply_warm_hours = pyqtSignal(int)  # emitted when user clicks Apply

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # ── Warm hours recommendation ──
        rec_box = QGroupBox("Warm Hours Recommendation")
        rec_lay = QHBoxLayout(rec_box)
        rec_lay.setContentsMargins(10, 4, 10, 4)
        self.rec_lbl = QLabel("Analyzing data…")
        self.rec_lbl.setWordWrap(True)
        rec_lay.addWidget(self.rec_lbl, 1)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setFixedWidth(80)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._on_apply)
        rec_lay.addWidget(self.apply_btn)
        layout.addWidget(rec_box)

        self._recommended_hours = None

        # ── Zone profiles table ──
        prof_box = QGroupBox("Zone Profiles (last 30 days)")
        prof_lay = QVBoxLayout(prof_box)
        self.profile_table = QTableWidget(0, 8)
        self.profile_table.setHorizontalHeaderLabels([
            "Zone", "Drying Rate\n(%/hr)", "Recovery\n(hours)",
            "Rebound\n(%/hr)", "Runoff\n(%)", "Early-Off\n(%)",
            "Cycles", "Status",
        ])
        hdr = self.profile_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 8):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.profile_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.profile_table.setAlternatingRowColors(True)
        self.profile_table.verticalHeader().setVisible(False)
        prof_lay.addWidget(self.profile_table)
        layout.addWidget(prof_box)

        # ── Recent heating cycles ──
        cyc_box = QGroupBox("Recent Heating Cycles (last 7 days)")
        cyc_lay = QVBoxLayout(cyc_box)
        self.cycles_table = QTableWidget(0, 9)
        self.cycles_table.setHorizontalHeaderLabels([
            "Zone", "Start", "Duration\n(hours)", "Humidity\nStart→End",
            "Drying Rate\n(%/hr)", "Runoff\n(%)", "Outdoor\n°C",
            "Dew Pt\n°C", "Reached\nThreshold",
        ])
        hdr2 = self.cycles_table.horizontalHeader()
        hdr2.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 9):
            hdr2.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.cycles_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.cycles_table.setAlternatingRowColors(True)
        self.cycles_table.verticalHeader().setVisible(False)
        cyc_lay.addWidget(self.cycles_table)
        layout.addWidget(cyc_box)

        # Status label
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color: grey; font-size: 11px;")
        layout.addWidget(self.status_lbl)

    def _on_apply(self):
        if self._recommended_hours is not None:
            self.apply_warm_hours.emit(self._recommended_hours)
            self.apply_btn.setEnabled(False)
            self.apply_btn.setText("Applied")

    def update_results(self, result: dict):
        """Called with the output of run_full_analysis()."""
        rec = result.get("warm_hours_recommendation")
        if rec:
            self._recommended_hours = rec["recommended"]
            conf_pct = rec["confidence"] * 100
            self.rec_lbl.setText(
                f"Current: {rec['current']}h  →  "
                f"Recommended: {rec['recommended']}h  "
                f"(confidence {conf_pct:.0f}%)\n"
                f"{rec['reasoning']}"
            )
            if rec["recommended"] != rec["current"]:
                self.apply_btn.setEnabled(True)
                self.apply_btn.setText("Apply")
            else:
                self.apply_btn.setEnabled(False)
                self.apply_btn.setText("Current is optimal")
        else:
            self.rec_lbl.setText(
                "Not enough data yet — need at least 7 days and 5 heating cycles.")
            self.apply_btn.setEnabled(False)

        # Zone profiles
        profiles = result.get("zone_profiles", {})
        self.profile_table.setRowCount(len(profiles))
        for row, (zone, p) in enumerate(sorted(profiles.items())):
            # Runoff display
            runoff = p.get("avg_runoff_drop")
            runoff_str = f"{runoff:.1f}" if runoff is not None else "—"

            # Early-off: show current learned adjustment if available
            early_off_str = "—"
            runoff_by_band = p.get("runoff_by_band", {})
            if runoff_by_band:
                # Show summary: bands with data
                bands_with_data = sum(
                    1 for b in runoff_by_band.values() if b.get("n", 0) >= 3)
                total_n = p.get("runoff_data_points", 0)
                if total_n >= 5:
                    early_off_str = f"{runoff:.1f}" if runoff is not None else "learning"
                else:
                    early_off_str = f"learning ({total_n}/5)"

            items = [
                QTableWidgetItem(zone),
                QTableWidgetItem(
                    f"{p['avg_drying_rate']}" if p.get("avg_drying_rate") else "—"),
                QTableWidgetItem(
                    f"{p['median_recovery_time']}" if p.get("median_recovery_time") else "—"),
                QTableWidgetItem(
                    f"{p['avg_rebound_rate']}" if p.get("avg_rebound_rate") else "—"),
                QTableWidgetItem(runoff_str),
                QTableWidgetItem(early_off_str),
                QTableWidgetItem(str(p.get("cycles_30d", 0))),
                QTableWidgetItem(p.get("status", "—")),
            ]
            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignCenter)
                self.profile_table.setItem(row, col, item)
            items[0].setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

            # Color runoff column
            if runoff is not None and runoff >= 2.0:
                items[4].setForeground(QBrush(QColor("#27ae60")))  # good runoff
            elif runoff is not None and runoff >= 1.0:
                items[4].setForeground(QBrush(QColor("#e67e22")))

            # Color early-off column
            if "learning" in early_off_str:
                items[5].setForeground(QBrush(QColor("#3498db")))
            elif early_off_str != "—":
                items[5].setForeground(QBrush(QColor("#27ae60")))

            status = p.get("status", "")
            if "Good" in status:
                items[7].setForeground(QBrush(QColor("#27ae60")))
            elif "Slow" in status:
                items[7].setForeground(QBrush(QColor("#e74c3c")))
            elif "Moderate" in status:
                items[7].setForeground(QBrush(QColor("#e67e22")))
            if "High rebound" in status:
                items[3].setForeground(QBrush(QColor("#e74c3c")))

        ts = result.get("analyzed_at", "")
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                self.status_lbl.setText(
                    f"Last analyzed: {dt.strftime('%d %b %Y %H:%M')}")
            except ValueError:
                self.status_lbl.setText(f"Last analyzed: {ts}")

    def update_cycles(self, cycles: list):
        """Populate recent cycles table."""
        self.cycles_table.setRowCount(len(cycles))
        for row, c in enumerate(cycles):
            start = c.get("start_ts", "")
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                start_str = dt.strftime("%d %b %Y %H:%M")
            except (ValueError, AttributeError):
                start_str = start[:16] if start else "—"

            hum_start = c.get("humidity_start")
            hum_end = c.get("humidity_end")
            hum_str = (f"{hum_start}→{hum_end}%"
                       if hum_start is not None and hum_end is not None
                       else "—")

            runoff = c.get("runoff_drop")
            dew_pt = c.get("avg_outdoor_dew_point")
            items = [
                QTableWidgetItem(c.get("zone_name", "—")),
                QTableWidgetItem(start_str),
                QTableWidgetItem(
                    f"{c['duration_hours']:.1f}" if c.get("duration_hours") else "—"),
                QTableWidgetItem(hum_str),
                QTableWidgetItem(
                    f"{c['drying_rate']:.1f}" if c.get("drying_rate") else "—"),
                QTableWidgetItem(
                    f"{runoff:.1f}" if runoff is not None else "—"),
                QTableWidgetItem(
                    f"{c['avg_outdoor_temp']:.1f}" if c.get("avg_outdoor_temp") else "—"),
                QTableWidgetItem(
                    f"{dew_pt:.1f}" if dew_pt is not None else "—"),
                QTableWidgetItem(
                    "Yes" if c.get("reached_threshold") else "No"),
            ]
            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignCenter)
                self.cycles_table.setItem(row, col, item)
            items[0].setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

            # Color runoff
            if runoff is not None and runoff >= 2.0:
                items[5].setForeground(QBrush(QColor("#27ae60")))

            if c.get("reached_threshold"):
                items[8].setForeground(QBrush(QColor("#27ae60")))
            else:
                items[8].setForeground(QBrush(QColor("#e67e22")))


# ── Energy tab (Linky) ────────────────────────────────────────────────────────

class LinkyWorker(QThread):
    """Fetches Linky data and runs energy analysis in background."""
    linky_done = pyqtSignal(dict)  # energy analysis result
    error = pyqtSignal(str)

    def __init__(self, db_conn, cfg: dict, days: int = 7):
        super().__init__()
        self.db_conn = db_conn
        self.cfg = cfg
        self.days = days

    def run(self):
        try:
            from datetime import date
            token = self.cfg.get("linky_token", "")
            prm = self.cfg.get("linky_prm", "")
            if not token or not prm:
                self.error.emit("Linky token or PRM not configured")
                return

            # Fetch last N days of load curve data
            end = date.today()
            start = end - timedelta(days=self.days)
            # API max 7 days per request — batch if needed
            cursor = start
            total_stored = 0
            while cursor < end:
                batch_end = min(cursor + timedelta(days=7), end)
                readings = fetch_load_curve(token, prm, cursor, batch_end)
                if readings:
                    total_stored += store_load_curve(self.db_conn, readings)
                cursor = batch_end

            # Run analysis (correlate with heating states)
            result = run_energy_analysis(self.db_conn, days=30)
            result["readings_fetched"] = total_stored
            self.linky_done.emit(result)
        except Exception as e:
            self.error.emit(str(e))


# ── Netatmo tab ──────────────────────────────────────────────────────────────

class NetatmoWorker(QThread):
    """Fetches current Netatmo station data in background."""
    netatmo_done = pyqtSignal(list)  # list of module dicts with dashboard data
    error = pyqtSignal(str)

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg

    def run(self):
        try:
            from airzone_netatmo import get_access_token, get_stations
            client_id = self.cfg.get("netatmo_client_id", "")
            client_secret = self.cfg.get("netatmo_client_secret", "")
            token = get_access_token(client_id, client_secret)
            if not token:
                self.error.emit("Netatmo: no token — run --auth first")
                return
            stations = get_stations(token)
            self.netatmo_done.emit(stations)
        except Exception as e:
            self.error.emit(f"Netatmo: {e}")


class NetatmoTab(QWidget):
    """Shows Netatmo weather station data alongside zone readings."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Header row
        hdr = QHBoxLayout()
        self.status_lbl = QLabel("Netatmo not configured")
        self.status_lbl.setStyleSheet("color: grey; font-style: italic;")
        hdr.addWidget(self.status_lbl)
        hdr.addStretch()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setFixedWidth(80)
        hdr.addWidget(self.refresh_btn)
        layout.addLayout(hdr)

        # Module cards
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "Module", "Type", "Temp", "Humidity", "CO2", "Pressure",
        ])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        # Note
        note = QLabel(
            "Netatmo data is stored alongside zone readings for "
            "cross-referencing humidity measurements.\n"
            "Run 'python src/airzone_netatmo.py --fetch --days 365' "
            "to import historical data.")
        note.setStyleSheet("color: #888; font-size: 11px; font-style: italic;")
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addStretch()

        self._refresh_callback = None
        self.refresh_btn.clicked.connect(self._on_refresh)

    def _on_refresh(self):
        if self._refresh_callback:
            self._refresh_callback()

    def show_not_configured(self):
        self.status_lbl.setText("Netatmo not configured — enable in Settings")
        self.status_lbl.setStyleSheet("color: #e67e22; font-style: italic;")
        self.table.setRowCount(0)

    def show_error(self, msg: str):
        self.status_lbl.setText(msg)
        self.status_lbl.setStyleSheet("color: #e74c3c;")

    def update_stations(self, stations: list):
        """Update the table with current Netatmo module data."""
        from airzone_netatmo import MODULE_TYPES

        self.table.setRowCount(len(stations))
        for row, mod in enumerate(stations):
            dash = mod.get("dashboard", {})
            mtype = MODULE_TYPES.get(mod.get("module_type", ""),
                                     mod.get("module_type", ""))

            temp = dash.get("Temperature")
            hum = dash.get("Humidity")
            co2 = dash.get("CO2")
            pressure = dash.get("Pressure")

            items = [
                QTableWidgetItem(mod.get("module_name", "?")),
                QTableWidgetItem(mtype),
                QTableWidgetItem(f"{temp:.1f} C" if temp is not None else "—"),
                QTableWidgetItem(f"{hum}%" if hum is not None else "—"),
                QTableWidgetItem(f"{co2} ppm" if co2 is not None else "—"),
                QTableWidgetItem(
                    f"{pressure:.1f} hPa" if pressure is not None else "—"),
            ]
            for col, item in enumerate(items):
                if col >= 2:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, item)

            # Color humidity
            if hum is not None:
                if hum >= 70:
                    items[3].setForeground(QBrush(QColor("#e74c3c")))
                elif hum >= 60:
                    items[3].setForeground(QBrush(QColor("#e67e22")))

            # Color CO2
            if co2 is not None:
                if co2 >= 1000:
                    items[4].setForeground(QBrush(QColor("#e74c3c")))
                elif co2 >= 800:
                    items[4].setForeground(QBrush(QColor("#e67e22")))

        now = datetime.now().strftime("%H:%M:%S")
        self.status_lbl.setText(f"Updated {now} — {len(stations)} modules")
        self.status_lbl.setStyleSheet("color: #27ae60;")


class EnergyTab(QWidget):
    """Shows Linky energy consumption, heat pump inference, and learned efficiency."""
    status_message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # ── Summary cards ──
        summary_box = QGroupBox("Energy Summary (last 7 days)")
        slay = QHBoxLayout(summary_box)
        slay.setContentsMargins(10, 4, 10, 4)
        self.total_lbl = QLabel("Total: --")
        self.total_lbl.setStyleSheet("font-weight: bold;")
        slay.addWidget(self.total_lbl)
        self.base_lbl = QLabel("Base load: --")
        slay.addWidget(self.base_lbl)
        self.hp_lbl = QLabel("Heat pump: --")
        self.hp_lbl.setStyleSheet("color: #e67e22; font-weight: bold;")
        slay.addWidget(self.hp_lbl)
        self.eff_lbl = QLabel("Efficiency: --")
        self.eff_lbl.setStyleSheet("color: #3498db; font-weight: bold;")
        slay.addWidget(self.eff_lbl)

        self.import_btn = QPushButton("📂 Import")
        self.import_btn.setFixedWidth(80)
        self.import_btn.setToolTip(
            "Import Enedis 'courbe de charge' export file\n"
            "(Excel .xlsx or CSV from your Enedis account)")
        self.import_btn.clicked.connect(self._on_import)
        slay.addWidget(self.import_btn)

        self.refresh_btn = QPushButton("⟳ Fetch")
        self.refresh_btn.setFixedWidth(70)
        self.refresh_btn.clicked.connect(self._on_refresh)
        slay.addWidget(self.refresh_btn)
        layout.addWidget(summary_box)

        self._refresh_callback = None  # set by MainWindow
        self._import_callback = None   # set by MainWindow

        # ── Chart ──
        if HAS_MATPLOTLIB:
            self.figure = Figure(figsize=(8, 2.5), dpi=100)
            self.figure.patch.set_facecolor("#f8f8f8")
            self.canvas = FigureCanvasQTAgg(self.figure)
            self.canvas.setMinimumHeight(180)
            layout.addWidget(self.canvas)
        else:
            self.canvas = None

        # ── Daily breakdown table ──
        daily_box = QGroupBox("Daily Breakdown")
        dlay = QVBoxLayout(daily_box)
        self.daily_table = QTableWidget(0, 7)
        self.daily_table.setHorizontalHeaderLabels([
            "Date", "Total\nkWh", "Base\nkWh", "Heat Pump\nkWh",
            "Heating\nhours", "Outdoor\n°C", "kWh/h\n(HP)",
        ])
        hdr = self.daily_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 7):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.daily_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.daily_table.setAlternatingRowColors(True)
        self.daily_table.verticalHeader().setVisible(False)
        dlay.addWidget(self.daily_table)
        layout.addWidget(daily_box)

        # ── Temperature band efficiency (self-learning) ──
        band_box = QGroupBox("Heat Pump Efficiency by Outdoor Temperature (Learned)")
        blay = QVBoxLayout(band_box)
        self.band_table = QTableWidget(0, 4)
        self.band_table.setHorizontalHeaderLabels([
            "Outdoor Temp", "kWh per hour\n(heat pump)", "Total Heating\nhours", "Days",
        ])
        hdr2 = self.band_table.horizontalHeader()
        hdr2.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 4):
            hdr2.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.band_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.band_table.setAlternatingRowColors(True)
        self.band_table.verticalHeader().setVisible(False)
        blay.addWidget(self.band_table)

        self.savings_lbl = QLabel("")
        self.savings_lbl.setWordWrap(True)
        self.savings_lbl.setStyleSheet("color: #27ae60; font-size: 11px; padding: 4px;")
        blay.addWidget(self.savings_lbl)
        layout.addWidget(band_box)

        # Status
        self.status_lbl = QLabel("Waiting for first data fetch…")
        self.status_lbl.setStyleSheet("color: grey; font-size: 11px;")
        layout.addWidget(self.status_lbl)

    def _on_refresh(self):
        if self._refresh_callback:
            self.refresh_btn.setEnabled(False)
            self.refresh_btn.setText("⟳ …")
            self._refresh_callback()

    def _on_import(self):
        if self._import_callback:
            self._import_callback()

    def update_results(self, result: dict):
        """Called with the output of run_energy_analysis()."""
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("⟳ Fetch")

        daily = result.get("daily_stats", [])
        bands = result.get("temp_band_efficiency", [])
        savings = result.get("savings")
        fetched = result.get("readings_fetched", 0)

        # ── Summary cards (last 7 days) ──
        recent = daily[:7]
        if recent:
            total = sum(d["total_kwh"] for d in recent if d.get("total_kwh"))
            base = sum(d["base_kwh"] for d in recent if d.get("base_kwh"))
            hp = sum(d["heatpump_kwh"] for d in recent if d.get("heatpump_kwh"))
            hrs = sum(d["heating_hours"] for d in recent if d.get("heating_hours"))
            self.total_lbl.setText(f"Total: {total:.1f} kWh")
            self.base_lbl.setText(f"Base load: {base:.1f} kWh")
            self.hp_lbl.setText(f"Heat pump: {hp:.1f} kWh")
            if hrs > 0:
                self.eff_lbl.setText(f"Efficiency: {hp / hrs:.2f} kWh/h")
            else:
                self.eff_lbl.setText("Efficiency: --")

        # ── Chart ──
        if self.canvas and daily:
            self._draw_chart(daily[:14])

        # ── Daily table ──
        self.daily_table.setRowCount(len(daily))
        for row, d in enumerate(daily):
            items = [
                QTableWidgetItem(_fmt_date(d.get("date", "—"))),
                QTableWidgetItem(
                    f"{d['total_kwh']:.1f}" if d.get("total_kwh") is not None else "—"),
                QTableWidgetItem(
                    f"{d['base_kwh']:.1f}" if d.get("base_kwh") is not None else "—"),
                QTableWidgetItem(
                    f"{d['heatpump_kwh']:.1f}" if d.get("heatpump_kwh") is not None else "—"),
                QTableWidgetItem(
                    f"{d['heating_hours']:.1f}" if d.get("heating_hours") is not None else "—"),
                QTableWidgetItem(
                    f"{d['avg_outdoor_temp']:.1f}" if d.get("avg_outdoor_temp") is not None else "—"),
                QTableWidgetItem(
                    f"{d['kwh_per_heating_hr']:.2f}" if d.get("kwh_per_heating_hr") is not None else "—"),
            ]
            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignCenter)
                self.daily_table.setItem(row, col, item)
            items[0].setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

            # Color heat pump column
            hp_kwh = d.get("heatpump_kwh")
            if hp_kwh is not None and hp_kwh > 0:
                items[3].setForeground(QBrush(QColor("#e67e22")))

        # ── Temperature band table (learned efficiency) ──
        self.band_table.setRowCount(len(bands))
        for row, b in enumerate(bands):
            items = [
                QTableWidgetItem(b.get("temp_band", "—")),
                QTableWidgetItem(
                    f"{b['avg_kwh_per_hr']:.2f}" if b.get("avg_kwh_per_hr") is not None else "—"),
                QTableWidgetItem(
                    f"{b['total_heating_hours']:.1f}" if b.get("total_heating_hours") is not None else "—"),
                QTableWidgetItem(str(b.get("days", 0))),
            ]
            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignCenter)
                self.band_table.setItem(row, col, item)
            items[0].setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

            # Color efficiency: lower is better (heat pump uses less per hour)
            kwh = b.get("avg_kwh_per_hr")
            if kwh is not None:
                if kwh < 1.0:
                    items[1].setForeground(QBrush(QColor("#27ae60")))  # great
                elif kwh < 2.0:
                    items[1].setForeground(QBrush(QColor("#e67e22")))  # ok
                else:
                    items[1].setForeground(QBrush(QColor("#e74c3c")))  # poor

        # ── Savings summary ──
        if savings:
            self.savings_lbl.setText(
                f"Warm vs cold comparison: {savings['reasoning']}\n"
                f"Based on {savings['warm_days']} warm days and "
                f"{savings['cold_days']} cold days.")
            self.savings_lbl.setVisible(True)
        else:
            self.savings_lbl.setVisible(False)

        ts = result.get("analyzed_at", "")
        self.status_lbl.setText(
            f"Last updated: {ts[:16]}  |  "
            f"{fetched} new readings fetched")

    def _draw_chart(self, daily: list):
        """Draw stacked bar chart: base load + heat pump per day."""
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.set_facecolor("#fafafa")

        dates = [_fmt_date(d.get("date", "")) for d in reversed(daily)]
        base = [d.get("base_kwh", 0) or 0 for d in reversed(daily)]
        hp = [d.get("heatpump_kwh", 0) or 0 for d in reversed(daily)]

        x = range(len(dates))
        ax.bar(x, base, color="#3498db", alpha=0.7, label="Base load")
        ax.bar(x, hp, bottom=base, color="#e67e22", alpha=0.8, label="Heat pump")

        ax.set_xticks(list(x))
        ax.set_xticklabels(dates, fontsize=7, rotation=45)
        ax.set_ylabel("kWh", fontsize=8)
        ax.legend(fontsize=7, loc="upper left")
        ax.tick_params(axis="y", labelsize=7)
        self.figure.tight_layout()
        self.canvas.draw()

    def show_error(self, msg: str):
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("⟳ Fetch")
        self.status_lbl.setText(f"Error: {msg}")
        self.status_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")


# ── Predictions tab ──────────────────────────────────────────────────────────

class PredictionsTab(QWidget):
    """Shows DP spread predictions and thermal model status."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>DP Spread Predictions</b>"))
        header.addStretch()
        self.refresh_btn = QPushButton("⟳ Update")
        self.refresh_btn.clicked.connect(self._refresh)
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        self.status_lbl = QLabel("Click Update to run prediction cycle")
        self.status_lbl.setStyleSheet("color: grey; font-size: 11px;")
        layout.addWidget(self.status_lbl)

        # Predictions table
        self.pred_table = QTableWidget(0, 6)
        self.pred_table.setHorizontalHeaderLabels(
            ["Zone", "Current", "3h Pred", "24h Pred", "Trend", "Confidence"])
        self.pred_table.horizontalHeader().setStretchLastSection(True)
        self.pred_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.pred_table)

        # Thermal models section
        layout.addWidget(QLabel("<b>Learned Thermal Models</b>"))
        self.model_table = QTableWidget(0, 5)
        self.model_table.setHorizontalHeaderLabels(
            ["Zone", "Samples", "Decay", "Confidence", "Data Days"])
        self.model_table.horizontalHeader().setStretchLastSection(True)
        self.model_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.model_table)

        self._refresh_callback = None

    def _refresh(self):
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Running…")
        self.status_lbl.setText("Learning thermal models and computing predictions…")
        if self._refresh_callback:
            self._refresh_callback()

    def show_predictions(self, result: dict):
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("⟳ Update")

        preds_3h = result.get("predictions_3h", {})
        preds_24h = result.get("predictions_24h", {})
        models = result.get("thermal_models", {})

        # Populate predictions table
        zones = sorted(set(list(preds_3h.keys()) + list(preds_24h.keys())))
        self.pred_table.setRowCount(len(zones))
        for i, zone in enumerate(zones):
            self.pred_table.setItem(i, 0, QTableWidgetItem(zone))

            p3 = preds_3h.get(zone, {})
            p24 = preds_24h.get(zone, {})

            current = p3.get("current_dp_spread")
            if current is not None:
                item = QTableWidgetItem(f"{current:.1f} °C")
                if current <= 2:
                    item.setForeground(QBrush(QColor("#e74c3c")))
                elif current < 4:
                    item.setForeground(QBrush(QColor("#e67e22")))
                elif current < 6:
                    item.setForeground(QBrush(QColor("#f39c12")))
                else:
                    item.setForeground(QBrush(QColor("#27ae60")))
                self.pred_table.setItem(i, 1, item)

            if "predicted_dp_spread" in p3:
                self.pred_table.setItem(
                    i, 2, QTableWidgetItem(f"{p3['predicted_dp_spread']:.1f} °C"))
            if "predicted_dp_spread" in p24:
                self.pred_table.setItem(
                    i, 3, QTableWidgetItem(f"{p24['predicted_dp_spread']:.1f} °C"))

            # Trend arrow
            trend = p3.get("trend", 0)
            arrows = {2: "↑↑", 1: "↑", 0: "→", -1: "↓", -2: "↓↓"}
            self.pred_table.setItem(i, 4, QTableWidgetItem(arrows.get(trend, "?")))

            conf = p3.get("confidence", 0)
            item = QTableWidgetItem(f"{conf:.0%}")
            if p3.get("is_learning"):
                item.setForeground(QBrush(QColor("#95a5a6")))
            self.pred_table.setItem(i, 5, item)

        # Populate models table
        model_zones = sorted(models.keys())
        self.model_table.setRowCount(len(model_zones))
        for i, zone in enumerate(model_zones):
            m = models[zone]
            self.model_table.setItem(i, 0, QTableWidgetItem(zone))
            self.model_table.setItem(i, 1, QTableWidgetItem(str(m.get("samples", 0))))
            self.model_table.setItem(
                i, 2, QTableWidgetItem(f"{m.get('decay_coeff', 0):.5f}"))
            self.model_table.setItem(
                i, 3, QTableWidgetItem(f"{m.get('confidence', 0):.0%}"))
            self.model_table.setItem(
                i, 4, QTableWidgetItem(f"{m.get('data_days', 0):.0f}"))

        n = len(zones)
        self.status_lbl.setText(
            f"Predictions for {n} zones, {len(model_zones)} thermal models learned")
        self.status_lbl.setStyleSheet("color: #27ae60; font-size: 11px;")


# ── Tariffs tab ──────────────────────────────────────────────────────────────

class TariffsTab(QWidget):
    """Shows EDF tariff comparison based on Linky data."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>EDF Tariff Comparison</b>"))
        header.addStretch()
        self.refresh_btn = QPushButton("⟳ Analyze")
        self.refresh_btn.clicked.connect(self._refresh)
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        self.status_lbl = QLabel("Click Analyze to compare tariffs")
        self.status_lbl.setStyleSheet("color: grey; font-size: 11px;")
        layout.addWidget(self.status_lbl)

        # Breakdown summary
        self.breakdown_lbl = QLabel("")
        self.breakdown_lbl.setWordWrap(True)
        self.breakdown_lbl.setStyleSheet("font-size: 11px; padding: 4px;")
        layout.addWidget(self.breakdown_lbl)

        # Offers table
        self.offers_table = QTableWidget(0, 4)
        self.offers_table.setHorizontalHeaderLabels(
            ["Offer", "Annual Cost", "Monthly", "Savings vs Best"])
        self.offers_table.horizontalHeader().setStretchLastSection(True)
        self.offers_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.offers_table)

        self._refresh_callback = None

    def _refresh(self):
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Analyzing…")
        self.status_lbl.setText("Fetching consumption data and calculating costs…")
        if self._refresh_callback:
            self._refresh_callback()

    def show_tariffs(self, result: dict):
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("⟳ Analyze")

        if "error" in result:
            self.status_lbl.setText(f"Error: {result['error']}")
            self.status_lbl.setStyleSheet("color: #e74c3c; font-size: 11px;")
            return

        breakdown = result.get("breakdown", {})
        offers = result.get("offers", [])
        cheapest = result.get("cheapest", "")

        # Show breakdown
        annual = breakdown.get("annual_kwh", 0)
        hp_pct = breakdown.get("hp_pct", 0)
        hc_pct = breakdown.get("hc_pct", 0)
        days = breakdown.get("data_days", 0)
        self.breakdown_lbl.setText(
            f"Annual consumption: ~{annual:,.0f} kWh "
            f"(HP: {hp_pct:.0f}%, HC: {hc_pct:.0f}%) — "
            f"based on {days} days of data")

        # Populate offers table
        self.offers_table.setRowCount(len(offers))
        for i, offer in enumerate(offers):
            name_item = QTableWidgetItem(offer["name"])
            if offer["name"] == cheapest:
                name_item.setForeground(QBrush(QColor("#27ae60")))
                font = name_item.font()
                font.setBold(True)
                name_item.setFont(font)
            self.offers_table.setItem(i, 0, name_item)
            self.offers_table.setItem(
                i, 1, QTableWidgetItem(f"{offer['annual_cost']:,.2f} €"))
            self.offers_table.setItem(
                i, 2, QTableWidgetItem(f"{offer['monthly_cost']:,.2f} €"))

            savings = offer.get("savings_vs_cheapest", 0)
            sav_item = QTableWidgetItem(
                f"+{savings:,.2f} €" if savings > 0 else "— Best")
            if savings == 0:
                sav_item.setForeground(QBrush(QColor("#27ae60")))
            self.offers_table.setItem(i, 3, sav_item)

        self.status_lbl.setText(f"Cheapest: {cheapest}")
        self.status_lbl.setStyleSheet("color: #27ae60; font-size: 11px;")


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    COLS = ["Zone", "Temperature", "Humidity", "DP Spread", "Mode", "Status", "Auto", "Control"]

    def __init__(self, api: AirzoneCloudAPI, cfg: dict):
        super().__init__()
        self.api = api
        self.cfg = cfg
        self.state = load_state()
        self.worker = None
        self.control_enabled = False
        self.last_zones = []
        self._control_workers = []  # keep refs so threads aren't GC'd
        self._history_worker = None
        self._analytics_worker = None
        self._outdoor_temp = None
        self._outdoor_dew_point = None
        self._outdoor_humidity = None
        self._local_db = LocalHistoryDB()

        self.setWindowTitle("Airzone Humidity Controller")
        self.setMinimumSize(780, 420)

        self._build_ui()

        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(self.cfg["poll_interval_seconds"] * 1000)

        self._linky_worker = None
        self._netatmo_worker = None

        self._poll()
        # Run initial analytics after a short delay (let first poll populate data)
        QTimer.singleShot(5000, self._run_analytics)
        # Re-run analytics every 30 minutes
        self._analytics_timer = QTimer()
        self._analytics_timer.timeout.connect(self._run_analytics)
        self._analytics_timer.start(1800 * 1000)  # 30 min

        # Linky: initial fetch after 10s, then every hour
        if self.cfg.get("linky_enabled"):
            QTimer.singleShot(10000, self._fetch_linky)
            self._linky_timer = QTimer()
            self._linky_timer.timeout.connect(self._fetch_linky)
            self._linky_timer.start(3600 * 1000)  # every hour

        # Netatmo: initial fetch after 8s, then every poll cycle
        if self.cfg.get("netatmo_enabled"):
            QTimer.singleShot(8000, self._fetch_netatmo)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(8)

        # Top bar
        top = QHBoxLayout()
        title = QLabel("Airzone")
        f = title.font()
        f.setPointSize(14)
        f.setBold(True)
        title.setFont(f)
        top.addWidget(title)
        top.addStretch()

        self.last_lbl = QLabel("Not yet refreshed")
        self.last_lbl.setStyleSheet("color: grey; font-size: 11px;")
        top.addWidget(self.last_lbl)

        self.refresh_btn = QPushButton("⟳  Refresh")
        self.refresh_btn.clicked.connect(self._poll)
        top.addWidget(self.refresh_btn)

        self.settings_btn = QPushButton("⚙  Settings")
        self.settings_btn.clicked.connect(self._open_settings)
        top.addWidget(self.settings_btn)

        root.addLayout(top)

        # Tab widget
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        # ── Zones tab ──
        zones_widget = QWidget()
        zones_lay = QVBoxLayout(zones_widget)
        zones_lay.setContentsMargins(0, 8, 0, 0)
        zones_lay.setSpacing(8)

        # Zone table
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(self.COLS)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.cellClicked.connect(self._on_zone_clicked)
        zones_lay.addWidget(self.table)

        # History panel (hidden until a zone is clicked)
        if HAS_MATPLOTLIB:
            self.history_panel = ZoneHistoryPanel()
            self.history_panel.fetch_requested.connect(self._fetch_history)
            zones_lay.addWidget(self.history_panel)
        else:
            self.history_panel = None

        # Weather info panel
        self.weather_box = QGroupBox("Weather Optimization")
        wlay = QHBoxLayout(self.weather_box)
        wlay.setContentsMargins(10, 4, 10, 4)
        self.outdoor_lbl = QLabel("Outdoor: --")
        self.outdoor_lbl.setStyleSheet("font-weight: bold;")
        wlay.addWidget(self.outdoor_lbl)
        self.warm_win_lbl = QLabel("Warm window: --")
        wlay.addWidget(self.warm_win_lbl)
        self.savings_lbl = QLabel("Est. savings: --")
        self.savings_lbl.setStyleSheet("color: #27ae60; font-weight: bold;")
        wlay.addWidget(self.savings_lbl)
        self.defer_lbl = QLabel("")
        self.defer_lbl.setStyleSheet("color: #e67e22; font-weight: bold;")
        wlay.addWidget(self.defer_lbl)
        zones_lay.addWidget(self.weather_box)
        self.weather_box.setVisible(self.cfg.get("weather_optimization", False))

        self.tabs.addTab(zones_widget, "Zones")

        # ── Hot Water tab ──
        self.dhw_tab = DHWTab(self.cfg, self.state, api=self.api)
        self.dhw_tab.status_message.connect(
            lambda msg: self.status_bar.showMessage(msg, 5000))
        self.tabs.addTab(self.dhw_tab, "Hot Water")

        # ── Analytics tab ──
        self.analytics_tab = AnalyticsTab()
        self.analytics_tab.apply_warm_hours.connect(self._apply_warm_hours)
        self.tabs.addTab(self.analytics_tab, "Analytics")

        # ── Netatmo tab ──
        self.netatmo_tab = NetatmoTab()
        self.netatmo_tab._refresh_callback = self._fetch_netatmo
        self.tabs.addTab(self.netatmo_tab, "Netatmo")

        # ── Energy tab ──
        self.energy_tab = EnergyTab()
        self.energy_tab._refresh_callback = self._fetch_linky
        self.energy_tab._import_callback = self._import_enedis_file
        self.tabs.addTab(self.energy_tab, "Energy")

        # ── Predictions tab ──
        self.predictions_tab = PredictionsTab()
        self.predictions_tab._refresh_callback = self._run_predictions
        self.tabs.addTab(self.predictions_tab, "Predictions")

        # ── Tariffs tab ──
        self.tariffs_tab = TariffsTab()
        self.tariffs_tab._refresh_callback = self._run_tariff_analysis
        self.tabs.addTab(self.tariffs_tab, "Tariffs")

        # Bottom bar
        bot = QHBoxLayout()
        self.ctrl_btn = QPushButton("▶  Enable Auto-Control")
        self.ctrl_btn.setCheckable(True)
        self.ctrl_btn.setMinimumHeight(30)
        self.ctrl_btn.setStyleSheet(
            "QPushButton { border: 1px solid #bdc3c7; border-radius: 4px; padding: 4px 10px; }"
            "QPushButton:checked { background-color: #27ae60; color: white; "
            "font-weight: bold; border-color: #27ae60; }"
        )
        self.ctrl_btn.clicked.connect(self._toggle_control)
        bot.addWidget(self.ctrl_btn)
        bot.addStretch()

        self.thresh_lbl = QLabel(self._thresh_text())
        self.thresh_lbl.setStyleSheet("color: grey; font-size: 11px;")
        bot.addWidget(self.thresh_lbl)
        root.addLayout(bot)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    def _thresh_text(self) -> str:
        return (
            f"Heat ON ≥ {self.cfg['humidity_on_threshold']}%  |  "
            f"OFF ≤ {self.cfg['humidity_off_threshold']}%  |  "
            f"Setpoint {self.cfg['heating_setpoint']} °C"
        )

    def _toggle_control(self, checked: bool):
        self.control_enabled = checked
        if checked:
            self.ctrl_btn.setText("⏹  Auto-Control ON")
            self.status_bar.showMessage(
                "Auto-control enabled — heating will switch based on humidity")
        else:
            self.ctrl_btn.setText("▶  Enable Auto-Control")
            self.status_bar.showMessage("Monitoring only — auto-control is off")

    def _poll(self):
        try:
            self._poll_inner()
        except Exception as e:
            log.error("Error in _poll: %s", e, exc_info=True)
            self.status_bar.showMessage(f"Poll error: {e}")

    def _poll_inner(self):
        if self.worker and self.worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.status_bar.showMessage("Fetching zone data…")
        # Keep reference to old worker until it is safely cleaned up by Qt
        old_worker = self.worker
        self.worker = PollWorker(self.api, self.cfg, self.state, self.control_enabled,
                                analytics_conn=self._local_db.conn)
        self.worker.zones_ready.connect(self._on_zones)
        self.worker.state_updated.connect(self._on_state)
        self.worker.weather_ready.connect(self._on_weather)
        self.worker.brain_result_ready.connect(self._on_brain_result)
        self.worker.error.connect(self._on_error)
        self.worker.finished.connect(lambda: self.refresh_btn.setEnabled(True))
        # Clear Python reference then schedule C++ deletion — prevents
        # RuntimeError when _poll() checks self.worker.isRunning() after
        # deleteLater has freed the C++ object.
        self.worker.finished.connect(lambda: setattr(self, "worker", None))
        self.worker.start()
        # Clean up old worker safely (deleteLater ensures C++ side is freed
        # only after pending events are processed)
        if old_worker is not None:
            old_worker.deleteLater()
        # Also refresh DHW status (works via Cloud API or local API)
        if self.cfg.get("dhw_enabled") or self.cfg.get("dhw_local_api"):
            self.dhw_tab.refresh_status()

    def _on_zones(self, zones: list):
        try:
            now = datetime.now().strftime("%H:%M:%S")
            self.last_lbl.setText(f"Updated {now}")
            if not zones:
                self.status_bar.showMessage("No zones returned — keeping previous data")
                return
            self.last_zones = zones
            self._populate_table(zones)
            self._local_db.log_readings(zones, self._outdoor_temp,
                                        outdoor_dew_point=self._outdoor_dew_point,
                                        outdoor_humidity=self._outdoor_humidity)
            mode = "auto-control ON" if self.control_enabled else "monitoring"
            self.status_bar.showMessage(f"Connected — {len(zones)} zone(s) — {mode}")
        except Exception as e:
            log.error("Error in _on_zones: %s", e, exc_info=True)
            self.status_bar.showMessage(f"Error updating zones: {e}")

    def _on_state(self, new_state: dict):
        try:
            self.state = new_state
            if self.last_zones:
                self._populate_table(self.last_zones)
            # Update DHW tab with weather schedule info
            dhw_weather = new_state.get("dhw_weather")
            if dhw_weather:
                dhw_weather["dhw_last_action"] = new_state.get("dhw_last_action")
                dhw_weather["dhw_override_count"] = new_state.get("dhw_override_count", 0)
                self.dhw_tab.update_weather(dhw_weather)
            # Re-run analytics if a heating cycle just completed
            if new_state.get("completed_cycles"):
                new_state["completed_cycles"] = []  # clear so we don't re-trigger
                self._run_analytics()
        except Exception as e:
            log.error("Error in _on_state: %s", e, exc_info=True)

    def _on_weather(self, winfo: dict):
        try:
            self._on_weather_inner(winfo)
        except Exception as e:
            log.error("Error in _on_weather: %s", e, exc_info=True)

    def _on_weather_inner(self, winfo: dict):
        self.weather_box.setVisible(True)
        temp = winfo.get("current_outdoor_temp")
        self._outdoor_temp = temp
        self._outdoor_dew_point = winfo.get("current_outdoor_dew_point")
        self._outdoor_humidity = winfo.get("current_outdoor_humidity")
        dew = self._outdoor_dew_point
        out_hum = self._outdoor_humidity
        parts = []
        if temp is not None:
            parts.append(f"Outdoor: {temp:.1f} °C")
        if dew is not None:
            parts.append(f"dew pt {dew:.1f} °C")
        if out_hum is not None:
            parts.append(f"hum {out_hum:.0f}%")
        if parts:
            first = parts[0]
            rest = ", ".join(parts[1:])
            self.outdoor_lbl.setText(f"{first}  ({rest})" if rest else first)
        else:
            self.outdoor_lbl.setText("Outdoor: --")

        start = winfo.get("next_warm_start")
        end = winfo.get("next_warm_end")
        if start and end:
            try:
                s = datetime.fromisoformat(start).strftime("%H:%M")
                e = datetime.fromisoformat(end).strftime("%H:%M")
            except (ValueError, TypeError):
                s, e = "?", "?"
            prefix = "NOW" if winfo.get("is_warm_now") else "Next"
            self.warm_win_lbl.setText(f"Warm window ({prefix}): {s}–{e}")
        else:
            self.warm_win_lbl.setText("Warm window: --")

        cop = winfo.get("cop_info", {})
        pct = cop.get("savings_pct")
        if pct is not None:
            self.savings_lbl.setText(f"Est. savings: {pct:.0f}%")
            self.savings_lbl.setToolTip(
                f"COP now: {cop.get('cop_now')} | "
                f"COP warm: {cop.get('cop_warm')}")
        else:
            self.savings_lbl.setText("Est. savings: --")

        pending = self.state.get("zones_pending_warm", {})
        if pending and not winfo.get("is_warm_now"):
            n = len(pending)
            self.defer_lbl.setText(
                f"Waiting for warm window ({n} zone{'s' if n > 1 else ''})")
        else:
            self.defer_lbl.setText("")

        # Update DHW tab weather (uses cached forecast, no network call)
        if self.cfg.get("dhw_enabled") and self.cfg.get("dhw_warm_hours_only", True):
            try:
                from airzone_weather import get_forecast, compute_warm_window
                forecast = get_forecast(
                    self.cfg.get("latitude", 44.07),
                    self.cfg.get("longitude", -1.26))
                dhw_hours = self.cfg.get("dhw_warm_hours_count", 3)
                dhw_weather = compute_warm_window(forecast, dhw_hours)
                self.dhw_tab.update_weather({
                    "warm_hours": dhw_weather.get("warm_hours", []),
                    "is_warm_now": dhw_weather.get("is_warm_now", False),
                    "next_warm_start": dhw_weather.get("next_warm_start"),
                    "next_warm_end": dhw_weather.get("next_warm_end"),
                    "outdoor_temp": dhw_weather.get("current_outdoor_temp"),
                })
            except Exception:
                pass

    def _on_brain_result(self, result: dict):
        """Handle result from the DP-spread control brain."""
        try:
            zones = result.get("zones", [])
            actions = [z for z in zones if z.get("action") != "no_change"]
            if actions:
                summaries = [f"{z['zone_name']}: {z['action']}" for z in actions]
                self.status_bar.showMessage(
                    f"Brain: {', '.join(summaries)}", 10000)
            else:
                pred = result.get("prediction_model", {})
                trust = "✓ trusted" if pred.get("trusted") else "learning"
                self.status_bar.showMessage(
                    f"Brain: all zones stable ({trust})", 5000)
        except Exception as e:
            log.error("Error in _on_brain_result: %s", e, exc_info=True)

    def _on_error(self, msg: str):
        self.status_bar.showMessage(f"Error: {msg}")

    def _populate_table(self, zones: list):
        self.table.setRowCount(len(zones))
        activated = self.state.get("zones_we_activated", {})
        pending = self.state.get("zones_pending_warm", {})

        for row, zone in enumerate(zones):
            inst = zone.get("_installation_name", "")
            name = zone.get("name", "?")
            label = f"{inst} / {name}" if inst else name

            local_temp = zone.get("local_temp")
            if isinstance(local_temp, dict):
                temp = local_temp.get("celsius")
            else:
                temp = local_temp
            humidity = zone.get("humidity")
            mode_id = zone.get("mode")
            mode_name = MODE_NAMES.get(mode_id, str(mode_id) if mode_id is not None else "—")
            is_on = bool(zone.get("power", False))
            dev_id = zone.get("_device_id", "")
            inst_id = zone.get("_installation_id", "")
            auto = dev_id in activated

            temp_s = f"{temp} °C" if temp is not None else "—"
            hum_s = f"{humidity} %" if humidity is not None else "—"

            # Calculate DP spread for display
            dp_spread_val = None
            dp_spread_s = "—"
            if temp is not None and humidity is not None:
                try:
                    from airzone_control_brain import calc_dewpoint
                    dp = calc_dewpoint(temp, humidity)
                    dp_spread_val = round(temp - dp, 1)
                    dp_spread_s = f"{dp_spread_val} °C"
                except ImportError:
                    pass

            items = [
                QTableWidgetItem(label),
                QTableWidgetItem(temp_s),
                QTableWidgetItem(hum_s),
                QTableWidgetItem(dp_spread_s),
                QTableWidgetItem(mode_name.title()),
                QTableWidgetItem("ON" if is_on else "OFF"),
                QTableWidgetItem(
                    "● active" if auto else
                    "● deferred" if dev_id in pending else ""),
            ]

            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, item)
            items[0].setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

            # Humidity colour
            if humidity is not None:
                on_t = self.cfg["humidity_on_threshold"]
                if humidity >= on_t:
                    items[2].setBackground(QBrush(QColor("#e74c3c")))
                    items[2].setForeground(QBrush(QColor("white")))
                elif humidity >= on_t - 5:
                    items[2].setBackground(QBrush(QColor("#e67e22")))
                    items[2].setForeground(QBrush(QColor("white")))
                else:
                    items[2].setBackground(QBrush(QColor("#eafaf1")))

            # DP Spread colour coding
            if dp_spread_val is not None:
                if dp_spread_val <= 2:
                    # Critical — condensation imminent
                    items[3].setBackground(QBrush(QColor("#e74c3c")))
                    items[3].setForeground(QBrush(QColor("white")))
                elif dp_spread_val < 4:
                    # Warning — below heating threshold
                    items[3].setBackground(QBrush(QColor("#e67e22")))
                    items[3].setForeground(QBrush(QColor("white")))
                elif dp_spread_val < 6:
                    # In-band — heating may be active
                    items[3].setBackground(QBrush(QColor("#f9e79f")))
                else:
                    # Safe
                    items[3].setBackground(QBrush(QColor("#eafaf1")))

            # Status colour
            if is_on:
                items[5].setForeground(QBrush(QColor("#27ae60")))
                f = items[5].font()
                f.setBold(True)
                items[5].setFont(f)
            else:
                items[5].setForeground(QBrush(QColor("#aaaaaa")))

            # Auto column colour
            if auto:
                items[6].setForeground(QBrush(QColor("#2980b9")))
            elif dev_id in pending:
                items[6].setForeground(QBrush(QColor("#e67e22")))

            # Control column — ON / OFF buttons
            ctrl_w = QWidget()
            ctrl_l = QHBoxLayout(ctrl_w)
            ctrl_l.setContentsMargins(4, 2, 4, 2)
            ctrl_l.setSpacing(4)

            btn_on = QPushButton("ON")
            btn_on.setFixedHeight(22)
            btn_off = QPushButton("OFF")
            btn_off.setFixedHeight(22)

            if is_on:
                btn_on.setStyleSheet(
                    "QPushButton { background-color: #27ae60; color: white; "
                    "font-weight: bold; border-radius: 3px; }"
                )
                btn_off.setStyleSheet("QPushButton { border-radius: 3px; }")
            else:
                btn_on.setStyleSheet("QPushButton { border-radius: 3px; }")
                btn_off.setStyleSheet(
                    "QPushButton { background-color: #c0392b; color: white; "
                    "font-weight: bold; border-radius: 3px; }"
                )

            btn_on.clicked.connect(
                lambda _, d=dev_id, i=inst_id: self._set_zone_power(d, i, True)
            )
            btn_off.clicked.connect(
                lambda _, d=dev_id, i=inst_id: self._set_zone_power(d, i, False)
            )

            ctrl_l.addWidget(btn_on)
            ctrl_l.addWidget(btn_off)
            self.table.setCellWidget(row, len(self.COLS) - 1, ctrl_w)

    def _set_zone_power(self, dev_id: str, inst_id: str, power: bool):
        action = "ON" if power else "OFF"
        self.status_bar.showMessage(f"Switching zone {action}…")
        w = ZoneControlWorker(self.api, dev_id, inst_id, power)
        w.done.connect(self._poll)
        w.done.connect(lambda: self.status_bar.showMessage(f"Zone switched {action}."))
        w.error.connect(lambda msg: self.status_bar.showMessage(f"Control error: {msg}"))
        w.start()
        # Keep reference so the thread isn't garbage-collected mid-run
        self._control_workers = [x for x in self._control_workers if x.isRunning()]
        self._control_workers.append(w)

    def _on_zone_clicked(self, row: int, col: int):
        """Show history graphs for the clicked zone."""
        if not self.history_panel or not self.last_zones:
            return
        if row >= len(self.last_zones):
            return
        zone = self.last_zones[row]
        inst = zone.get("_installation_name", "")
        name = zone.get("name", "?")
        zone_name = f"{inst}/{name}" if inst else name
        self.history_panel.show_zone(zone_name)

    def _fetch_history(self, zone_name: str, hours: int):
        """Fetch history from Pi dashboard API, or fall back to local DB."""
        pi_url = self.cfg.get("pi_dashboard_url", "").strip()
        if pi_url:
            if self._history_worker and self._history_worker.isRunning():
                return
            self.status_bar.showMessage(f"Fetching history for {zone_name}...")
            self._history_worker = HistoryFetchWorker(pi_url, zone_name, hours)
            self._history_worker.data_ready.connect(self._on_history_data)
            self._history_worker.error.connect(self._on_history_error)
            self._history_worker.finished.connect(
                lambda: setattr(self, "_history_worker", None))
            self._history_worker.start()
        else:
            # Use local DB
            readings = self._local_db.get_readings(zone_name, hours)
            self._on_history_data({
                "zone_name": zone_name,
                "readings": readings,
                "actions": [],
                "hours": hours,
            })

    def _on_history_data(self, data: dict):
        if self.history_panel:
            self.history_panel.update_data(data)
        self.status_bar.showMessage("History loaded.", 3000)

    def _on_history_error(self, msg: str):
        if self.history_panel:
            self.history_panel.show_error(msg)
        self.status_bar.showMessage(f"History error: {msg}")

    def _open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec() == QDialog.Accepted:
            self.cfg = dlg.result_config()
            # Store sensitive values in .env, strip from config file
            _save_config_secure(self.cfg)
            self.poll_timer.setInterval(self.cfg["poll_interval_seconds"] * 1000)
            self.thresh_lbl.setText(self._thresh_text())
            self.weather_box.setVisible(self.cfg.get("weather_optimization", False))
            self.dhw_tab.update_config(self.cfg)
            # Enable/disable Energy tab based on Linky config
            energy_idx = self.tabs.indexOf(self.energy_tab)
            linky_on = bool(self.cfg.get("linky_enabled"))
            self.tabs.setTabEnabled(energy_idx, linky_on)
            if linky_on and not hasattr(self, "_linky_timer"):
                self._linky_timer = QTimer()
                self._linky_timer.timeout.connect(self._fetch_linky)
                self._linky_timer.start(3600 * 1000)
                QTimer.singleShot(2000, self._fetch_linky)
            self._poll()

    # ── Analytics ──

    def _run_analytics(self):
        """Run full analytics in background thread."""
        try:
            self._run_analytics_inner()
        except Exception as e:
            log.error("Error starting analytics: %s", e, exc_info=True)

    def _run_analytics_inner(self):
        if self._analytics_worker and self._analytics_worker.isRunning():
            return
        self._analytics_worker = AnalyticsWorker(
            self._local_db.conn, self.cfg)
        self._analytics_worker.analysis_done.connect(self._on_analytics_done)
        self._analytics_worker.error.connect(
            lambda msg: self.status_bar.showMessage(f"Analytics error: {msg}"))
        # Clear reference when thread finishes so isRunning() check works
        self._analytics_worker.finished.connect(
            lambda: setattr(self, "_analytics_worker", None))
        self._analytics_worker.start()

    def _on_analytics_done(self, result: dict):
        """Handle analytics results."""
        self.analytics_tab.update_results(result)

        # Load recent cycles for display
        try:
            cycles = get_recent_cycles(self._local_db.conn, days=7)
            self.analytics_tab.update_cycles(cycles)
        except Exception:
            pass

        # Auto-optimize if enabled
        rec = result.get("warm_hours_recommendation")
        if (rec and self.cfg.get("auto_optimize_warm_hours")
                and rec["confidence"] >= 0.7
                and abs(rec["recommended"] - rec["current"]) >= 1):
            self._apply_warm_hours(rec["recommended"])

        # Update warm window hint in weather panel
        if rec and rec["recommended"] != rec["current"]:
            current_text = self.warm_win_lbl.text()
            if "(suggested:" not in current_text:
                self.warm_win_lbl.setText(
                    f"{current_text}  (suggested: {rec['recommended']}h)")

    def _apply_warm_hours(self, hours: int):
        """Apply a new warm_hours_count from analytics recommendation."""
        self.cfg["warm_hours_count"] = hours
        _save_config_secure(self.cfg)
        self.status_bar.showMessage(
            f"Warm hours updated to {hours}h (from analytics)")

    # ── Netatmo ──

    def _fetch_netatmo(self):
        """Fetch current Netatmo station data in background."""
        try:
            self._fetch_netatmo_inner()
        except Exception as e:
            log.error("Error starting Netatmo fetch: %s", e, exc_info=True)

    def _fetch_netatmo_inner(self):
        if not self.cfg.get("netatmo_enabled"):
            self.netatmo_tab.show_not_configured()
            return
        if hasattr(self, "_netatmo_worker") and self._netatmo_worker and self._netatmo_worker.isRunning():
            return
        self._netatmo_worker = NetatmoWorker(self.cfg)
        self._netatmo_worker.netatmo_done.connect(self._on_netatmo_done)
        self._netatmo_worker.error.connect(self.netatmo_tab.show_error)
        self._netatmo_worker.finished.connect(
            lambda: setattr(self, "_netatmo_worker", None))
        self._netatmo_worker.start()

    def _on_netatmo_done(self, stations: list):
        """Handle Netatmo station data."""
        self.netatmo_tab.update_stations(stations)
        # Also store current readings in the local DB
        if stations:
            try:
                from airzone_netatmo import store_readings, create_netatmo_tables
                create_netatmo_tables(self._local_db.conn)
                now_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                readings = []
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
                store_readings(self._local_db.conn, readings)
            except Exception as e:
                log.error("Failed to store Netatmo readings: %s", e)

    # ── Linky Energy ──

    def _fetch_linky(self):
        """Fetch Linky data and run energy analysis in background."""
        try:
            self._fetch_linky_inner()
        except Exception as e:
            log.error("Error starting Linky fetch: %s", e, exc_info=True)

    def _fetch_linky_inner(self):
        if not self.cfg.get("linky_enabled"):
            self.energy_tab.show_error("Linky not enabled — configure in Settings")
            return
        if hasattr(self, "_linky_worker") and self._linky_worker and self._linky_worker.isRunning():
            return
        self._linky_worker = LinkyWorker(self._local_db.conn, self.cfg, days=7)
        self._linky_worker.linky_done.connect(self._on_linky_done)
        self._linky_worker.error.connect(self.energy_tab.show_error)
        self._linky_worker.finished.connect(
            lambda: setattr(self, "_linky_worker", None))
        self._linky_worker.start()

    def _on_linky_done(self, result: dict):
        """Handle Linky energy results."""
        self.energy_tab.update_results(result)
        self.status_bar.showMessage(
            f"Energy data updated ({result.get('readings_fetched', 0)} readings)", 5000)

    def _run_predictions(self):
        """Run thermal model learning and DP predictions in background."""
        class PredictWorker(QThread):
            result_ready = pyqtSignal(dict)
            error = pyqtSignal(str)

            def __init__(self, db_conn, cfg):
                super().__init__()
                self.db_conn = db_conn
                self.cfg = cfg

            def run(self):
                try:
                    from airzone_thermal_model import run_prediction_cycle
                    # Build weather forecast
                    weather = None
                    try:
                        from airzone_weather import get_forecast
                        raw = get_forecast(
                            self.cfg.get("latitude", 44.07),
                            self.cfg.get("longitude", -1.26))
                        if raw and raw.get("hourly"):
                            hourly = raw["hourly"]
                            idx3 = min(3, len(hourly.get("temperature_2m", [])) - 1)
                            idx24 = min(24, len(hourly.get("temperature_2m", [])) - 1)
                            weather = {
                                "current_temp": raw.get("current", {}).get("temperature_2m"),
                                "current_hum": raw.get("current", {}).get("relative_humidity_2m"),
                                "temp_3h": hourly["temperature_2m"][idx3] if idx3 >= 0 else None,
                                "hum_3h": hourly["relative_humidity_2m"][idx3] if idx3 >= 0 else None,
                                "temp_24h": hourly["temperature_2m"][idx24] if idx24 >= 0 else None,
                                "hum_24h": hourly["relative_humidity_2m"][idx24] if idx24 >= 0 else None,
                            }
                    except Exception:
                        pass
                    result = run_prediction_cycle(self.db_conn, weather)
                    self.result_ready.emit(result)
                except Exception as e:
                    self.error.emit(str(e))

        self._predict_worker = PredictWorker(self._local_db.conn, self.cfg)
        self._predict_worker.result_ready.connect(
            self.predictions_tab.show_predictions)
        self._predict_worker.error.connect(
            lambda msg: self.predictions_tab.status_lbl.setText(f"Error: {msg}"))
        self._predict_worker.start()

    def _run_tariff_analysis(self):
        """Run tariff comparison in background."""
        class TariffWorker(QThread):
            result_ready = pyqtSignal(dict)
            error = pyqtSignal(str)

            def __init__(self, db_conn, cfg):
                super().__init__()
                self.db_conn = db_conn
                self.cfg = cfg

            def run(self):
                try:
                    from airzone_best_price import BestPriceAnalyzer
                    analyzer = BestPriceAnalyzer(
                        token=self.cfg.get("linky_token", ""),
                        prm=self.cfg.get("linky_prm", ""),
                        kva=self.cfg.get("kva", 9),
                        hc_schedule=self.cfg.get("hc_schedule", "22-6"),
                    )
                    result = analyzer.run_analysis(days=365, conn=self.db_conn)
                    self.result_ready.emit(result)
                except Exception as e:
                    self.error.emit(str(e))

        self._tariff_worker = TariffWorker(self._local_db.conn, self.cfg)
        self._tariff_worker.result_ready.connect(
            self.tariffs_tab.show_tariffs)
        self._tariff_worker.error.connect(
            lambda msg: self.tariffs_tab.status_lbl.setText(f"Error: {msg}"))
        self._tariff_worker.start()

    def _import_enedis_file(self):
        """Open file dialog to import Enedis export (Excel/CSV)."""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Import Enedis Export",
            str(Path.home() / "Downloads"),
            "Enedis exports (*.xlsx *.xls *.csv);;All files (*)",
        )
        if not filepath:
            return

        self.energy_tab.status_lbl.setText("Importing…")
        self.energy_tab.status_lbl.setStyleSheet("color: grey; font-size: 11px;")
        QApplication.processEvents()

        try:
            result = import_enedis_file(self._local_db.conn, filepath)
            n = result["imported"]
            skip = result["skipped"]
            start = result["start_date"] or "?"
            end = result["end_date"] or "?"

            if n == 0:
                self.energy_tab.status_lbl.setText(
                    "No readings imported — file may be empty or already imported")
                self.energy_tab.status_lbl.setStyleSheet(
                    "color: #e67e22; font-size: 11px;")
                return

            self.status_bar.showMessage(
                f"Imported {n} readings ({start} to {end})", 8000)

            # Run analysis on the imported data
            analysis = run_energy_analysis(self._local_db.conn, days=30)
            analysis["readings_fetched"] = n
            self.energy_tab.update_results(analysis)
            self.energy_tab.status_lbl.setText(
                f"Imported {n} readings ({start} to {end})"
                + (f", {skip} skipped" if skip else ""))
            self.energy_tab.status_lbl.setStyleSheet(
                "color: #27ae60; font-size: 11px;")

            # Enable the Energy tab now that we have data
            energy_idx = self.tabs.indexOf(self.energy_tab)
            self.tabs.setTabEnabled(energy_idx, True)

        except Exception as e:
            self.energy_tab.show_error(f"Import failed: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Airzone Humidity Controller")

    cfg = load_config(CONFIG_PATH)
    api = AirzoneCloudAPI()

    # Load credentials from .env (project-isolated)
    try:
        from airzone_secrets import secrets
        for key in ("email", "password"):
            stored = secrets.get(key)
            if stored:
                cfg[key] = stored
    except Exception:
        pass  # Secrets module unavailable — rely on config file values

    has_creds = bool(cfg.get("email")) and bool(cfg.get("password"))

    if has_creds and api.load_cached_tokens():
        # Tokens still valid — go straight to main window
        win = MainWindow(api, cfg)
        win.show()
    elif has_creds:
        # Have credentials but tokens expired — try login, then show main
        try:
            api.ensure_token(cfg["email"], cfg["password"])
        except Exception:
            pass  # will retry on first poll
        win = MainWindow(api, cfg)
        win.show()
    else:
        # No stored credentials — show login dialog first
        login = LoginWindow(api, cfg)
        def _on_login(email, password):
            cfg["email"] = email
            cfg["password"] = password
            login.hide()
            main_win = MainWindow(api, cfg)
            main_win.show()
            # Keep reference so it isn't garbage-collected
            app._main_win = main_win
        login.logged_in.connect(_on_login)
        login.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

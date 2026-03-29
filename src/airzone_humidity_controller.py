#!/usr/bin/env python3
"""
Airzone Cloud Humidity Controller
===================================
Connects to the Airzone Cloud API (same backend as the mobile app),
monitors humidity across all zones, and automatically switches
heating on/off based on configurable thresholds.

Works with Airzone Aidoo / Easyzone systems connected to Airzone Cloud.

Requires:
    pip install requests

Setup:
    1. Copy airzone_config.json, fill in your email/password.
    2. Test with:  python3 airzone_humidity_controller.py --status
    3. Explore raw API data:  python3 airzone_humidity_controller.py --dump
    4. Dry-run:  python3 airzone_humidity_controller.py --dry-run --once
    5. Run:  python3 airzone_humidity_controller.py

Credentials can also be supplied via environment variables:
    AIRZONE_EMAIL=...  AIRZONE_PASSWORD=...
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency:  pip install requests")
    sys.exit(1)


# ── Constants ─────────────────────────────────────────────────────────────────

CLOUD_BASE = "https://m.airzonecloud.com"
LOGIN_PATH = "/api/v1/auth/login"
REFRESH_PATH = "/api/v1/auth/refreshToken"
INSTALLATIONS_PATH = "/api/v1/installations"

# Airzone mode IDs (same across local and cloud API)
MODE_NAMES = {1: "stop", 2: "cool", 3: "heat", 4: "fan", 5: "dry", 7: "auto"}

if getattr(sys, "frozen", False):
    # Running inside a PyInstaller .app bundle.
    # Use macOS standard location so data survives app rebuilds.
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Airzone"
else:
    # Source lives in src/, data files live in data/ under the project root.
    DATA_DIR = Path(__file__).parent.parent / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "airzone_config.json"
STATE_PATH = DATA_DIR / "airzone_state.json"
TOKEN_PATH = DATA_DIR / ".airzone_tokens.json"   # gitignored auth cache

DEFAULT_CONFIG = {
    "email": "",
    "password": "",
    "poll_interval_seconds": 300,
    "humidity_on_threshold": 70,
    "humidity_off_threshold": 65,
    "heating_mode": 3,
    "heating_setpoint": 22.0,
    "log_file": "airzone_humidity.log",
    "dry_run": False,
    "weather_optimization": True,
    "latitude": 44.07,
    "longitude": -1.26,
    "warm_hours_count": 6,
    "emergency_humidity_threshold": 88,
    "max_defer_hours": 18,              # max hours to defer heating for warm window
    # Smart early-off: learned from heating cycle runoff data
    "smart_early_off": True,             # use learned runoff to stop heating early
    "smart_early_off_max": 5,            # max % above off_threshold (safety cap)
    "smart_early_off_min_cycles": 5,     # minimum cycles before applying
    "max_indoor_temp": 18.0,             # safety cap: stop heating above this room temp
    # Dew point threshold adjustment
    "dew_point_relax_enabled": True,     # relax ON threshold when outdoor dew point is low
    "dew_point_relax_below": 5.0,        # dew point °C below which to relax
    "dew_point_relax_amount": 3,         # % to add to ON threshold (e.g. 70→73%)
    # DHW (Domestic Hot Water) control via Airzone local API
    "dhw_enabled": False,
    "dhw_setpoint": 20.0,           # target water temperature when heating
    "dhw_local_api": "",             # Airzone webserver IP, e.g. "192.168.1.25"
    "dhw_warm_hours_only": True,     # only heat DHW during warmest hours
    "dhw_warm_hours_count": 3,       # how many of the warmest hours to use for DHW
    "auto_optimize_warm_hours": False,  # auto-apply analytics warm hours recommendation
    # Linky energy monitoring (Conso API)
    "linky_enabled": False,
    "linky_token": "",                   # Bearer token from https://conso.boris.sh/
    "linky_prm": "",                     # 14-digit meter ID (PRM)
}


# ── Logging ───────────────────────────────────────────────────────────────────

log = logging.getLogger("airzone")


def setup_logging(log_file: str = ""):
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)


# ── Config & State ────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    if not path.exists():
        log.info("Creating default config at %s", path)
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        print(f"\n  Created {path}")
        print("  Fill in 'email' and 'password', then re-run.\n")
        sys.exit(0)

    with open(path) as f:
        cfg = json.load(f)

    merged = {**DEFAULT_CONFIG, **cfg}

    # ── Credential loading from .env ─────────────────────────────────
    # Migrate plaintext secrets from config to .env on first run, then
    # always load credentials from .env (project-isolated).
    try:
        from airzone_secrets import secrets
        secrets.migrate_from_config(merged, config_path=path)
        # .env values take precedence over config file
        for key in ("email", "password", "linky_token", "linky_prm",
                    "netatmo_client_id", "netatmo_client_secret"):
            val = secrets.get(key)
            if val:
                merged[key] = val
    except Exception as e:
        log.debug("Secrets module not available: %s", e)

    return merged


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"zones_we_activated": {}}


def save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ── Airzone Cloud API ─────────────────────────────────────────────────────────

class AirzoneCloudAPI:
    """
    Thin wrapper around the Airzone Cloud REST API.

    The API is used by the official Airzone mobile app. Endpoints below
    were identified from the aioairzone-cloud open-source library (used
    by Home Assistant). If Airzone changes their API, use --dump to
    inspect the raw responses and update the paths accordingly.
    """

    def __init__(self, timeout: int = 15):
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.timeout = timeout
        self.token: str = ""
        self.refresh_token: str = ""
        self.token_expiry: datetime = datetime.min

    # ── Auth ──────────────────────────────────────────────────────────────────

    def login(self, email: str, password: str):
        """Authenticate and store JWT token."""
        resp = self.session.post(
            CLOUD_BASE + LOGIN_PATH,
            json={"email": email, "password": password},
            timeout=self.timeout,
        )
        if resp.status_code == 401:
            raise RuntimeError("Login failed: check your email and password.")
        resp.raise_for_status()

        data = resp.json()
        # Response shape: {"token": "...", "refreshToken": "..."} (top-level)
        self._store_tokens(data.get("token", ""), data.get("refreshToken", ""))
        log.info("Logged in to Airzone Cloud")

    def _store_tokens(self, token: str, refresh: str):
        self.token = token
        self.refresh_token = refresh
        # Library uses TOKEN_REFRESH_PERIOD = timedelta(hours=12)
        self.token_expiry = datetime.now() + timedelta(hours=12)
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        # Cache tokens to .env or fallback file
        try:
            from airzone_secrets import secrets
            secrets.set("airzone_token", token)
            secrets.set("airzone_refresh_token", refresh)
            secrets.set("airzone_token_expiry", self.token_expiry.isoformat())
        except Exception:
            # Fallback: write to disk (old behaviour)
            TOKEN_PATH.write_text(json.dumps({
                "token": token,
                "refreshToken": refresh,
                "expiry": self.token_expiry.isoformat(),
            }))
            os.chmod(str(TOKEN_PATH), 0o600)

    def load_cached_tokens(self) -> bool:
        """Try to reuse a previously saved token. Returns True if valid."""
        # Try .env storage first
        try:
            from airzone_secrets import secrets
            token = secrets.get("airzone_token")
            refresh = secrets.get("airzone_refresh_token")
            expiry_str = secrets.get("airzone_token_expiry")
            if token and refresh and expiry_str:
                expiry = datetime.fromisoformat(expiry_str)
                if datetime.now() < expiry:
                    self._store_tokens(token, refresh)
                    log.debug("Reusing cached auth token from .env (expires %s)", expiry)
                    return True
            # Migrate old token file if it exists
            if TOKEN_PATH.exists():
                secrets.migrate_tokens(TOKEN_PATH, prefix="airzone")
                return self.load_cached_tokens()  # Retry from .env
        except Exception:
            pass
        # Fallback: read from plaintext file
        if not TOKEN_PATH.exists():
            return False
        try:
            data = json.loads(TOKEN_PATH.read_text())
            expiry = datetime.fromisoformat(data["expiry"])
            if datetime.now() < expiry:
                self._store_tokens(data["token"], data["refreshToken"])
                log.debug("Reusing cached auth token (expires %s)", expiry)
                return True
        except Exception:
            pass
        return False

    def ensure_token(self, email: str, password: str):
        """Ensure we have a valid token, refreshing or re-logging as needed."""
        if self.token and datetime.now() < self.token_expiry:
            return  # Still valid

        if self.refresh_token:
            try:
                resp = self.session.post(
                    CLOUD_BASE + REFRESH_PATH,
                    json={"token": self.token, "refreshToken": self.refresh_token},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                self._store_tokens(data.get("token", ""), data.get("refreshToken", ""))
                log.debug("Token refreshed")
                return
            except Exception as e:
                log.debug("Token refresh failed (%s), re-logging", e)

        self.login(email, password)

    # ── Data ──────────────────────────────────────────────────────────────────

    def get_installations(self) -> list[dict]:
        """Return list of all installations on this account."""
        resp = self.session.get(
            CLOUD_BASE + INSTALLATIONS_PATH,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # Shape: {"installations": [...]}  or just a list
        if isinstance(data, list):
            return data
        return data.get("installations", [])

    def get_installation_detail(self, installation_id: str) -> dict:
        """Return full detail for one installation (includes groups and zones)."""
        resp = self.session.get(
            f"{CLOUD_BASE}{INSTALLATIONS_PATH}/{installation_id}",
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("installation", data)

    def get_device_status(self, device_id: str, installation_id: str) -> dict:
        """Fetch live status (temp, humidity, power, mode) for one zone device."""
        url_id = urllib.parse.quote(device_id)
        resp = self.session.get(
            f"{CLOUD_BASE}/api/v1/devices/{url_id}/status",
            params={"installation_id": installation_id},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_all_zones(self) -> list[dict]:
        """
        Walk all installations → groups → devices, collect az_zone devices,
        fetch live status for each, and return a flat list.
        """
        zones = []
        for inst in self.get_installations():
            inst_id = inst.get("installation_id") or inst.get("id", "")
            detail = self.get_installation_detail(inst_id)

            groups = detail.get("groups", [])
            for group in groups:
                for device in group.get("devices", []):
                    if device.get("type") != "az_zone":
                        continue
                    dev_id = device.get("device_id") or device.get("id", "")
                    try:
                        status = self.get_device_status(dev_id, inst_id)
                    except Exception as e:
                        log.warning("Could not fetch status for %s: %s", dev_id, e)
                        status = {}
                    zones.append({
                        **device,
                        **status,
                        "_installation_id": inst_id,
                        "_installation_name": inst.get("name", ""),
                        "_device_id": dev_id,
                    })

        return zones

    def set_zone(self, device_id: str, installation_id: str = "", **params):
        """
        Send control commands to a zone.

        Each keyword argument becomes a separate PATCH request using
        the Airzone Cloud envelope format::

            {"param": "<name>", "value": <val>, "installation_id": "..."}

        Supported params: power (bool), mode (int),
                          setpoint_air_heat (float or {"celsius": N}).
        """
        url_id = urllib.parse.quote(device_id)
        url = f"{CLOUD_BASE}/api/v1/devices/{url_id}"

        for param_name, value in params.items():
            body = {
                "param": param_name,
                "value": value,
                "installation_id": installation_id,
            }
            resp = self.session.patch(url, json=body, timeout=self.timeout)
            resp.raise_for_status()

    # ── DHW (Cloud API) ──────────────────────────────────────────────────────

    def get_dhw_devices(self) -> list[dict]:
        """
        Find DHW (hot water) devices across all installations.
        DHW devices have type "az_acs" or "aidoo_acs".
        Returns a list with device info and live status merged in.
        """
        dhw_devices = []
        for inst in self.get_installations():
            inst_id = inst.get("installation_id") or inst.get("id", "")
            detail = self.get_installation_detail(inst_id)

            for group in detail.get("groups", []):
                for device in group.get("devices", []):
                    dev_type = device.get("type", "")
                    if dev_type not in ("az_acs", "aidoo_acs"):
                        continue
                    dev_id = device.get("device_id") or device.get("id", "")
                    try:
                        status = self.get_device_status(dev_id, inst_id)
                    except Exception as e:
                        log.warning("Could not fetch DHW status for %s: %s",
                                    dev_id, e)
                        status = {}
                    dhw_devices.append({
                        **device,
                        **status,
                        "_installation_id": inst_id,
                        "_installation_name": inst.get("name", ""),
                        "_device_id": dev_id,
                    })
        return dhw_devices

    def get_dhw_status(self) -> dict:
        """
        Get DHW status via Cloud API.
        Returns dict with power, setpoint, tank_temp, active, and device IDs.
        """
        devices = self.get_dhw_devices()
        if not devices:
            return {}
        d = devices[0]  # Use first DHW device found

        # Extract setpoint/tank_temp — Cloud API nests them in {"celsius": N}
        setpoint = d.get("setpoint")
        if isinstance(setpoint, dict):
            setpoint = setpoint.get("celsius")
        tank_temp = d.get("tank_temp")
        if isinstance(tank_temp, dict):
            tank_temp = tank_temp.get("celsius")

        return {
            "acs_power": 1 if d.get("power") else 0,
            "acs_setpoint": setpoint,
            "acs_temp": tank_temp,
            "active": d.get("active"),
            "powerful_mode": d.get("powerful_mode"),
            "_device_id": d.get("_device_id", ""),
            "_installation_id": d.get("_installation_id", ""),
        }

    def set_dhw(self, device_id: str, installation_id: str = "",
                power: bool = None, setpoint: int = None,
                powerful_mode: bool = None):
        """
        Control DHW via Cloud API (same PATCH envelope as zones).

        Args:
            device_id:      DHW device ID from get_dhw_devices()
            installation_id: Installation ID
            power:          True/False to turn DHW on/off
            setpoint:       Target water temperature in °C (integer)
            powerful_mode:  True/False for boost mode
        """
        params = {}
        if power is not None:
            params["power"] = power
        if setpoint is not None:
            params["setpoint"] = setpoint
        if powerful_mode is not None:
            params["powerful_mode"] = powerful_mode

        if not params:
            return

        url_id = urllib.parse.quote(device_id)
        url = f"{CLOUD_BASE}/api/v1/devices/{url_id}"

        for param_name, value in params.items():
            body = {
                "param": param_name,
                "value": value,
                "installation_id": installation_id,
            }
            resp = self.session.patch(url, json=body, timeout=self.timeout)
            resp.raise_for_status()


# ── DHW Control (Local API) ───────────────────────────────────────────────────

def dhw_get_status(ip: str, timeout: int = 10) -> dict:
    """
    Query DHW status from the Airzone local API (webserver port 3000).
    Returns dict with acs_power, acs_setpoint, and acs_temp (tank temp).
    """
    url = f"http://{ip}:3000/api/v1/hvac"
    resp = requests.post(url, json={"systemID": 0, "zoneID": 0}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # The local API returns data inside a "data" array
    systems = data.get("data", [data])
    if not systems:
        return {}
    sys_data = systems[0] if isinstance(systems, list) else systems
    return {
        "acs_power": sys_data.get("acs_power"),
        "acs_setpoint": sys_data.get("acs_setpoint"),
        "acs_temp": sys_data.get("acs_temp"),
    }


def dhw_set(ip: str, power: int = None, setpoint: float = None,
            dry_run: bool = False, timeout: int = 10):
    """
    Control DHW via the Airzone local API.

    Args:
        ip:       Airzone webserver IP address
        power:    0 = OFF, 1 = ON
        setpoint: Target water temperature in °C
        dry_run:  If True, log but don't send
    """
    payload = {"systemID": 0, "zoneID": 0}
    if power is not None:
        payload["acs_power"] = power
    if setpoint is not None:
        payload["acs_setpoint"] = setpoint

    if len(payload) <= 2:
        return  # Nothing to change

    if dry_run:
        log.info("  [DRY RUN] Would send DHW command: %s", payload)
        return

    url = f"http://{ip}:3000/api/v1/hvac"
    resp = requests.put(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    log.debug("DHW command sent: %s → %s", payload, resp.json())


# ── Core Logic ────────────────────────────────────────────────────────────────

def _log_decision(analytics_conn, zone_name, humidity, room_temp,
                  weather_info, on_thresh, effective_on_thresh,
                  off_thresh, effective_off, is_warm,
                  action, reason, dew_point_decision):
    """Log a control decision to the analytics DB."""
    if analytics_conn is None:
        return
    try:
        from airzone_analytics import log_control_decision
        outdoor_temp = weather_info.get("current_outdoor_temp") if weather_info else None
        outdoor_dew = weather_info.get("current_outdoor_dew_point") if weather_info else None
        log_control_decision(
            analytics_conn, zone_name, humidity, room_temp,
            outdoor_temp, outdoor_dew,
            on_thresh, effective_on_thresh, off_thresh, effective_off,
            is_warm, action, reason, dew_point_decision)
    except Exception:
        pass


def check_and_control(api: AirzoneCloudAPI, cfg: dict, state: dict,
                      weather_info: dict = None,
                      analytics_conn=None) -> dict:
    """
    Single iteration of the control loop.
    Reads all zones, applies humidity rules, returns updated state.

    If weather_info is provided and weather_optimization is enabled,
    heating is deferred to the warmest hours of the day (higher COP).
    """
    on_thresh = cfg["humidity_on_threshold"]
    off_thresh = cfg["humidity_off_threshold"]
    heat_mode = cfg["heating_mode"]
    setpoint = cfg["heating_setpoint"]
    dry_run = cfg["dry_run"]

    weather_enabled = cfg.get("weather_optimization", False) and weather_info
    emergency_thresh = cfg.get("emergency_humidity_threshold", 88)
    smart_off_enabled = cfg.get("smart_early_off", True) and analytics_conn
    current_dew_point = (weather_info.get("current_outdoor_dew_point")
                         if weather_info else None)
    max_defer_h = cfg.get("max_defer_hours", 18)
    is_warm = weather_info.get("is_warm_now", False) if weather_info else False
    max_indoor_temp = cfg.get("max_indoor_temp", 18.0)

    # Dew point threshold relaxation: when outdoor air is dry, humidity is
    # less of a problem (low moisture ingress) → raise ON threshold to save energy
    dew_relax_enabled = cfg.get("dew_point_relax_enabled", True) and current_dew_point is not None
    dew_relax_below = cfg.get("dew_point_relax_below", 5.0)
    dew_relax_amount = cfg.get("dew_point_relax_amount", 3)
    effective_on_thresh = on_thresh
    dew_point_decision = None
    if dew_relax_enabled and current_dew_point < dew_relax_below:
        effective_on_thresh = on_thresh + dew_relax_amount
        dew_point_decision = (f"Dew point {current_dew_point:.1f}°C < {dew_relax_below}°C → "
                              f"ON threshold relaxed {on_thresh}%→{effective_on_thresh}%")
        log.info("  Dew point %.1f°C < %.1f°C (dry air) → "
                 "ON threshold relaxed from %d%% to %d%%",
                 current_dew_point, dew_relax_below, on_thresh, effective_on_thresh)

    activated = state.get("zones_we_activated", {})
    pending = state.get("zones_pending_warm", {})

    try:
        zones = api.get_all_zones()
    except requests.RequestException as e:
        log.error("API error reading zones: %s", e)
        return state

    if not zones:
        log.warning("No zones returned from API — check credentials and installation.")
        return state

    # Phase 0a: force-activate zones that exceeded max_defer_hours
    if pending and max_defer_h > 0:
        zone_hum = {z.get("_device_id", ""): z.get("humidity") for z in zones}
        for dev_id in list(pending):
            info = pending[dev_id]
            since_str = info.get("pending_since", "")
            if not since_str:
                continue
            try:
                since = datetime.fromisoformat(since_str)
                hours_pending = (datetime.now() - since).total_seconds() / 3600
            except (ValueError, TypeError):
                continue
            if hours_pending >= max_defer_h:
                hum = zone_hum.get(dev_id)
                if hum is not None and hum > off_thresh:
                    log.warning("  %s: deferred for %.1fh (max %dh) → "
                                "force-activating heating (humidity %d%%)",
                                info["label"], hours_pending, max_defer_h, hum)
                    if not dry_run:
                        try:
                            api.set_zone(dev_id,
                                         installation_id=info.get("installation_id", ""),
                                         power=True, mode=heat_mode,
                                         setpoint_air_heat={"celsius": setpoint})
                            activated[dev_id] = {
                                **info,
                                "activated_at": datetime.now().isoformat(),
                                "humidity_at_activation": hum,
                            }
                        except requests.RequestException as e:
                            log.error("  Failed to force-activate zone: %s", e)
                            continue
                    del pending[dev_id]
                else:
                    log.info("  %s: max defer exceeded but humidity recovered "
                             "(%s%%), cancelling", info["label"], hum)
                    del pending[dev_id]

    # Phase 0b: if warm window just started, activate any pending zones
    if weather_enabled and is_warm and pending:
        zone_hum = {z.get("_device_id", ""): z.get("humidity") for z in zones}
        for dev_id in list(pending):
            info = pending[dev_id]
            hum = zone_hum.get(dev_id)
            if hum is not None and hum > off_thresh:
                log.warning("  %s: warm window active, activating deferred heating "
                            "(humidity still %d%%)", info["label"], hum)
                if not dry_run:
                    try:
                        api.set_zone(dev_id,
                                     installation_id=info.get("installation_id", ""),
                                     power=True, mode=heat_mode,
                                     setpoint_air_heat={"celsius": setpoint})
                        activated[dev_id] = {
                            **info,
                            "activated_at": datetime.now().isoformat(),
                        }
                    except requests.RequestException as e:
                        log.error("  Failed to activate deferred zone: %s", e)
                        continue
                del pending[dev_id]
            else:
                log.info("  %s: warm window active but humidity recovered (%s%%), "
                         "cancelling", info["label"], hum)
                del pending[dev_id]

    for zone in zones:
        dev_id = zone.get("_device_id", "")
        inst_id = zone.get("_installation_id", "")
        name = zone.get("name", dev_id)
        inst_name = zone.get("_installation_name", "")
        label = f"{inst_name}/{name}" if inst_name else name

        humidity = zone.get("humidity")
        is_on = zone.get("power", False)
        current_mode = zone.get("mode")
        room_temp = zone.get("local_temp")
        if isinstance(room_temp, dict):
            room_temp = room_temp.get("celsius")

        if humidity is None:
            log.debug("%-30s  no humidity sensor, skipping", label)
            continue

        mode_str = MODE_NAMES.get(current_mode, str(current_mode))
        temp_str = f"  temp={room_temp:.1f}°C" if room_temp is not None else ""
        dew_str = f"  dew_pt={current_dew_point:.1f}°C" if current_dew_point is not None else ""
        log.info("%-30s  humidity=%d%%  on=%-3s  mode=%s%s%s",
                 label, humidity, "ON" if is_on else "OFF", mode_str,
                 temp_str, dew_str)

        # ── Safety cap: stop heating if room too warm ──
        if room_temp is not None and room_temp >= max_indoor_temp and dev_id in activated:
            log.warning("  %s: room temp %.1f°C >= %.1f°C safety cap → "
                        "turning heating OFF (humidity still %d%%)",
                        label, room_temp, max_indoor_temp, humidity)
            if not dry_run:
                try:
                    prev = activated[dev_id]
                    prev_power = prev.get("previous_power", False)
                    prev_mode = prev.get("previous_mode", 1)
                    prev_inst = prev.get("installation_id", inst_id)
                    api.set_zone(dev_id, installation_id=prev_inst,
                                 power=prev_power, mode=prev_mode)
                    completed = state.setdefault("completed_cycles", [])
                    completed.append({
                        "zone_name": label,
                        "device_id": dev_id,
                        "activated_at": prev.get("activated_at"),
                        "deactivated_at": datetime.now().isoformat(),
                        "humidity_at_activation": prev.get("humidity_at_activation"),
                        "humidity_at_deactivation": humidity,
                        "reason": "max_indoor_temp",
                        "room_temp": room_temp,
                    })
                    if len(completed) > 100:
                        state["completed_cycles"] = completed[-100:]
                    del activated[dev_id]
                except requests.RequestException as e:
                    log.error("  Failed to turn off zone: %s", e)
            else:
                log.info("  [DRY RUN] Would turn off (temp cap)")
                del activated[dev_id]
            _log_decision(analytics_conn, label, humidity, room_temp,
                          weather_info, on_thresh, effective_on_thresh,
                          off_thresh, off_thresh, is_warm,
                          "OFF_TEMP_CAP",
                          f"Room {room_temp:.1f}°C >= {max_indoor_temp}°C cap",
                          dew_point_decision)
            continue  # skip further checks for this zone

        # ── Too humid → activate or defer heating
        if humidity >= effective_on_thresh:
            if is_on and current_mode == heat_mode:
                if dev_id in activated:
                    # WE activated this zone → let it run until humidity
                    # drops to off_thresh, regardless of warm window
                    log.debug("  Already heating (our cycle), holding")
                    _log_decision(analytics_conn, label, humidity, room_temp,
                                  weather_info, on_thresh, effective_on_thresh,
                                  off_thresh, off_thresh, is_warm,
                                  "HOLD_ON", "Already heating (our cycle)",
                                  dew_point_decision)
                elif (weather_enabled and not is_warm
                        and humidity < emergency_thresh):
                    # Zone is heating (externally or leftover) outside warm
                    # window and we didn't start it → defer, don't force off
                    log.debug("  Zone heating externally outside warm window, deferring")
                    if dev_id not in pending:
                        pending[dev_id] = {
                            "label": label,
                            "pending_since": datetime.now().isoformat(),
                            "humidity_at_trigger": humidity,
                            "device_id": dev_id,
                            "installation_id": inst_id,
                            "previous_power": is_on,
                            "previous_mode": current_mode,
                        }
                    _log_decision(analytics_conn, label, humidity, room_temp,
                                  weather_info, on_thresh, effective_on_thresh,
                                  off_thresh, off_thresh, is_warm,
                                  "DEFERRED", "Heating externally, outside warm window",
                                  dew_point_decision)
                else:
                    log.debug("  Already heating, nothing to do")
                    _log_decision(analytics_conn, label, humidity, room_temp,
                                  weather_info, on_thresh, effective_on_thresh,
                                  off_thresh, off_thresh, is_warm,
                                  "HOLD_ON", "Already heating",
                                  dew_point_decision)
            elif (weather_enabled and not is_warm
                  and humidity < emergency_thresh
                  and dev_id not in pending):
                # Defer to warm window
                next_w = weather_info.get("next_warm_start", "unknown")
                log.warning("  %s: humidity %d%% >= %d%% → DEFERRED to warm window "
                            "(next: %s)", label, humidity, effective_on_thresh, next_w)
                pending[dev_id] = {
                    "label": label,
                    "pending_since": datetime.now().isoformat(),
                    "humidity_at_trigger": humidity,
                    "device_id": dev_id,
                    "installation_id": inst_id,
                    "previous_power": is_on,
                    "previous_mode": current_mode,
                }
                _log_decision(analytics_conn, label, humidity, room_temp,
                              weather_info, on_thresh, effective_on_thresh,
                              off_thresh, off_thresh, is_warm,
                              "DEFERRED", "Warm window deferral",
                              dew_point_decision)
            elif dev_id not in activated:
                # Activate: warm window active, emergency, no weather,
                # or pending zone whose warm window has arrived
                is_from_pending = dev_id in pending
                if (weather_enabled and humidity >= emergency_thresh
                        and not is_warm):
                    reason = "EMERGENCY"
                    log.warning("  %s: EMERGENCY humidity %d%% >= %d%% → "
                                "heating ON (overriding warm window)",
                                label, humidity, emergency_thresh)
                elif is_from_pending and is_warm:
                    reason = "Warm window active (was deferred)"
                    log.warning("  %s: warm window now active, activating "
                                "deferred zone (humidity %d%%)",
                                label, humidity)
                elif is_from_pending and not is_warm:
                    # Still pending, not warm yet — log and skip
                    _log_decision(analytics_conn, label, humidity, room_temp,
                                  weather_info, on_thresh, effective_on_thresh,
                                  off_thresh, off_thresh, is_warm,
                                  "HOLD_PENDING",
                                  "Waiting for warm window (deferred)",
                                  dew_point_decision)
                    continue
                elif is_warm:
                    reason = "Warm window active"
                    log.warning("  %s: humidity %d%% >= %d%% → turning heating ON "
                                "(warm window)", label, humidity, effective_on_thresh)
                else:
                    reason = "Immediate activation"
                    log.warning("  %s: humidity %d%% >= %d%% → turning heating ON",
                                label, humidity, effective_on_thresh)

                if not dry_run:
                    try:
                        api.set_zone(dev_id, installation_id=inst_id,
                                     power=True, mode=heat_mode,
                                     setpoint_air_heat={"celsius": setpoint})
                        activated[dev_id] = {
                            "label": label,
                            "activated_at": datetime.now().isoformat(),
                            "humidity_at_activation": humidity,
                            "previous_power": is_on,
                            "previous_mode": current_mode,
                            "device_id": dev_id,
                            "installation_id": inst_id,
                        }
                    except requests.RequestException as e:
                        log.error("  Failed to control zone: %s", e)
                        continue
                else:
                    log.info("  [DRY RUN] Would send: power=True, mode=%d, "
                             "setpoint=%.1f", heat_mode, setpoint)
                    activated[dev_id] = {"dry_run": True, "label": label,
                                         "device_id": dev_id}
                # Remove from pending if was deferred before
                pending.pop(dev_id, None)
                _log_decision(analytics_conn, label, humidity, room_temp,
                              weather_info, on_thresh, effective_on_thresh,
                              off_thresh, off_thresh, is_warm,
                              "ON", reason, dew_point_decision)

        # ── Humidity recovered → turn off (only zones WE activated)
        # Smart early-off: learned runoff adjustment per zone per dew point
        effective_off = off_thresh
        early_off_info = None
        if smart_off_enabled and dev_id in activated:
            try:
                from airzone_analytics import get_smart_early_off_adjustment
                adj = get_smart_early_off_adjustment(
                    analytics_conn, label,
                    current_dew_point=current_dew_point,
                    max_adjustment=cfg.get("smart_early_off_max", 5),
                    min_cycles=cfg.get("smart_early_off_min_cycles", 5))
                if adj["adjustment"] > 0:
                    effective_off = off_thresh + adj["adjustment"]
                    early_off_info = adj
                    log.debug("  %s: smart early-off → effective %d%% "
                              "(base %d%% + %.1f%% runoff, band %s)",
                              label, effective_off, off_thresh,
                              adj["adjustment"], adj["band_used"])
            except Exception:
                pass  # fall back to standard threshold

        if humidity <= effective_off:
            if dev_id in activated:
                if early_off_info:
                    log.warning("  %s: smart early-off at %d%% "
                                "(base %d%% + %.1f%% learned runoff, "
                                "band %s) → heating OFF",
                                label, humidity, off_thresh,
                                early_off_info["adjustment"],
                                early_off_info["band_used"])
                else:
                    log.warning("  %s: humidity %d%% <= %d%% → "
                                "turning heating OFF",
                                label, humidity, off_thresh)
                if not dry_run:
                    try:
                        prev = activated[dev_id]
                        prev_power = prev.get("previous_power", False)
                        prev_mode = prev.get("previous_mode", 1)
                        prev_inst = prev.get("installation_id", inst_id)
                        api.set_zone(dev_id, installation_id=prev_inst,
                                     power=prev_power, mode=prev_mode)
                        # Record completed cycle for analytics
                        completed = state.setdefault("completed_cycles", [])
                        completed.append({
                            "zone_name": label,
                            "device_id": dev_id,
                            "activated_at": prev.get("activated_at"),
                            "deactivated_at": datetime.now().isoformat(),
                            "humidity_at_activation": prev.get(
                                "humidity_at_activation"),
                            "humidity_at_deactivation": humidity,
                            "smart_early_off": early_off_info,
                        })
                        # Cap list to prevent unbounded state file growth
                        if len(completed) > 100:
                            state["completed_cycles"] = completed[-100:]
                        del activated[dev_id]
                    except requests.RequestException as e:
                        log.error("  Failed to control zone: %s", e)
                else:
                    log.info("  [DRY RUN] Would restore previous state")
                    del activated[dev_id]
                off_reason = (f"Smart early-off (band {early_off_info['band_used']}, "
                              f"+{early_off_info['adjustment']:.1f}%)"
                              if early_off_info else "Humidity recovered")
                _log_decision(analytics_conn, label, humidity, room_temp,
                              weather_info, on_thresh, effective_on_thresh,
                              off_thresh, effective_off, is_warm,
                              "OFF", off_reason, dew_point_decision)
            if dev_id in pending:
                log.info("  %s: humidity recovered (%d%%) while pending, cancelling",
                         label, humidity)
                del pending[dev_id]
                _log_decision(analytics_conn, label, humidity, room_temp,
                              weather_info, on_thresh, effective_on_thresh,
                              off_thresh, effective_off, is_warm,
                              "CANCEL_PENDING", "Humidity recovered while pending",
                              dew_point_decision)

        else:
            if dev_id in activated:
                log.debug("  Humidity %d%% in hysteresis band (%d–%d%%), holding",
                          humidity, off_thresh, on_thresh)
            _log_decision(analytics_conn, label, humidity, room_temp,
                          weather_info, on_thresh, effective_on_thresh,
                          off_thresh, effective_off, is_warm,
                          "NO_ACTION", "Humidity in normal range",
                          dew_point_decision)

    state["zones_we_activated"] = activated
    state["zones_pending_warm"] = pending
    if weather_info:
        state["last_weather_info"] = weather_info
    state["last_check"] = datetime.now().isoformat()
    save_state(state)
    return state


# ── DHW Control Logic ────────────────────────────────────────────────────────

def check_and_control_dhw(cfg: dict, state: dict,
                          weather_info: dict = None,
                          api: AirzoneCloudAPI = None) -> dict:
    """
    DHW control: turn hot water on only during the warmest hours.

    Supports two backends:
      1. Cloud API (preferred) — works from anywhere, uses the same
         Airzone Cloud account as zone control. No local IP needed.
      2. Local API (fallback) — requires dhw_local_api to be set and
         the controller to be on the same network as the webserver.

    When dhw_warm_hours_only is True, the system only heats water during
    the N warmest hours in the next 24h — this maximises the heat pump's
    COP and minimises electricity use.
    """
    if not cfg.get("dhw_enabled"):
        return state

    ip = cfg.get("dhw_local_api", "")
    use_cloud = api is not None
    dry_run = cfg.get("dry_run", False)
    setpoint = cfg.get("dhw_setpoint", 20.0)
    warm_only = cfg.get("dhw_warm_hours_only", True)

    if not use_cloud and not ip:
        log.warning("DHW enabled but no Cloud API or dhw_local_api — skipping")
        return state

    # Get current DHW status
    try:
        if use_cloud:
            dhw_status = api.get_dhw_status()
            if not dhw_status:
                log.warning("DHW: no DHW device found via Cloud API")
                return state
        else:
            dhw_status = dhw_get_status(ip)
    except Exception as e:
        source = "Cloud API" if use_cloud else f"local API at {ip}"
        log.error("DHW: cannot reach %s: %s", source, e)
        return state

    current_power = dhw_status.get("acs_power")
    current_setpoint = dhw_status.get("acs_setpoint")
    tank_temp = dhw_status.get("acs_temp")
    dhw_dev_id = dhw_status.get("_device_id", "")
    dhw_inst_id = dhw_status.get("_installation_id", "")

    log.info("DHW: tank=%.1f°C  power=%s  setpoint=%s°C  (via %s)",
             tank_temp if tank_temp is not None else 0,
             "ON" if current_power else "OFF",
             current_setpoint if current_setpoint is not None else "?",
             "cloud" if use_cloud else "local")

    # Helper to send DHW commands via the appropriate backend
    def _dhw_send(power=None, setpoint_val=None):
        if dry_run:
            log.info("  [DRY RUN] Would send DHW command: power=%s setpoint=%s",
                     power, setpoint_val)
            return
        if use_cloud:
            p = True if power == 1 else (False if power == 0 else None)
            sp = int(setpoint_val) if setpoint_val is not None else None
            api.set_dhw(dhw_dev_id, dhw_inst_id, power=p, setpoint=sp)
        else:
            dhw_set(ip, power=power, setpoint=setpoint_val, dry_run=False)

    if not warm_only:
        # Always-on mode: just ensure DHW is on at the right setpoint
        if not current_power or current_setpoint != setpoint:
            log.warning("DHW: setting power=ON, setpoint=%.1f°C", setpoint)
            _dhw_send(power=1, setpoint_val=setpoint)
        state["dhw_last_action"] = "always_on"
        state["dhw_last_check"] = datetime.now().isoformat()
        save_state(state)
        return state

    # Warm-hours-only mode: compute DHW-specific warm window
    dhw_warm_count = cfg.get("dhw_warm_hours_count", 3)

    # DHW fetches its own forecast (independent of zone weather_info)
    from airzone_weather import compute_warm_window, get_forecast
    try:
        forecast = get_forecast(cfg.get("latitude", 44.07),
                                cfg.get("longitude", -1.26))
        dhw_weather = compute_warm_window(forecast, dhw_warm_count)
    except Exception as e:
        log.warning("DHW: cannot fetch weather forecast: %s", e)
        state["dhw_last_check"] = datetime.now().isoformat()
        save_state(state)
        return state
    is_dhw_warm = dhw_weather.get("is_warm_now", False)
    prev_action = state.get("dhw_last_action")
    dhw_override_count = state.get("dhw_override_count", 0)

    if is_dhw_warm:
        # Warm window active → turn DHW ON
        if not current_power or current_setpoint != setpoint:
            log.warning("DHW: warm window active (outdoor %.1f°C) → "
                        "turning ON, setpoint=%.1f°C",
                        dhw_weather.get("current_outdoor_temp", 0), setpoint)
            _dhw_send(power=1, setpoint_val=setpoint)
        else:
            log.info("DHW: warm window active, already heating at %.1f°C",
                     setpoint)
        state["dhw_last_action"] = "on_warm_window"
        state["dhw_override_count"] = 0
    else:
        # Outside warm window → turn DHW OFF (with override protection)
        if prev_action == "off_backed_off":
            # Already backed off — don't fight until warm window resets
            log.info("DHW: outside warm window, backed off (FTC in control)")
            state["dhw_last_action"] = "off_backed_off"
        elif current_power:
            if prev_action == "off_waiting" and dhw_override_count >= 2:
                # FTC controller keeps overriding us — stop fighting
                log.warning("DHW: heat pump overrode OFF command %d times, "
                            "backing off (FTC schedule likely active)",
                            dhw_override_count)
                state["dhw_last_action"] = "off_backed_off"
            elif prev_action == "off_waiting":
                # We sent OFF before but it's ON again → FTC overrode us
                dhw_override_count += 1
                next_start = dhw_weather.get("next_warm_start", "unknown")
                log.warning("DHW: FTC overrode OFF (attempt %d) → "
                            "re-sending OFF (next warm: %s)",
                            dhw_override_count, next_start)
                _dhw_send(power=0)
                state["dhw_override_count"] = dhw_override_count
                state["dhw_last_action"] = "off_waiting"
            else:
                # First OFF command for this period
                next_start = dhw_weather.get("next_warm_start", "unknown")
                log.warning("DHW: outside warm window → turning OFF "
                            "(next warm: %s)", next_start)
                _dhw_send(power=0)
                state["dhw_override_count"] = 0
                state["dhw_last_action"] = "off_waiting"
        else:
            log.info("DHW: outside warm window, already off")
            state["dhw_last_action"] = "off_waiting"
            state["dhw_override_count"] = 0

    # Store DHW weather info for reference
    state["dhw_weather"] = {
        "warm_hours": dhw_weather.get("warm_hours", []),
        "is_warm_now": is_dhw_warm,
        "next_warm_start": dhw_weather.get("next_warm_start"),
        "next_warm_end": dhw_weather.get("next_warm_end"),
        "outdoor_temp": dhw_weather.get("current_outdoor_temp"),
    }
    state["dhw_last_check"] = datetime.now().isoformat()
    save_state(state)
    return state


# ── Status / Dump helpers ─────────────────────────────────────────────────────

def print_status(zones: list[dict]):
    """Pretty-print all zone data."""
    print(f"\n{'Installation/Zone':<35} {'On':<5} {'Mode':<7} {'Temp':>6} {'Set':>6} {'Hum':>5}")
    print("─" * 65)
    for z in zones:
        inst = z.get("_installation_name", "")
        name = z.get("name", "?")
        label = f"{inst}/{name}" if inst else name
        on = "ON" if z.get("power") else "OFF"
        mode = z.get("mode")
        mode_s = MODE_NAMES.get(mode, str(mode))
        temp = z.get("roomTemp") or z.get("local_temp") or z.get("temp") or "—"
        setp = z.get("setpoint") or "—"
        if isinstance(setp, dict):
            setp = setp.get("celsius", "—")
        hum = z.get("humidity") or "—"
        temp_s = f"{temp}°C" if isinstance(temp, (int, float)) else str(temp)
        setp_s = f"{setp}°C" if isinstance(setp, (int, float)) else str(setp)
        hum_s = f"{hum}%" if isinstance(hum, (int, float)) else str(hum)
        print(f"{label:<35} {on:<5} {mode_s:<7} {temp_s:>6} {setp_s:>6} {hum_s:>5}")
    print()


def print_dhw_status(ip: str, cfg: dict):
    """Print current DHW status and weather-based schedule."""
    print(f"\n{'─' * 50}")
    print("DHW (Hot Water) Status")
    print(f"{'─' * 50}")
    try:
        status = dhw_get_status(ip)
        power = status.get("acs_power")
        setpoint = status.get("acs_setpoint")
        tank = status.get("acs_temp")
        print(f"  Webserver:    {ip}:3000")
        print(f"  Power:        {'ON' if power else 'OFF'}")
        print(f"  Tank temp:    {tank}°C" if tank is not None else "  Tank temp:    —")
        print(f"  Setpoint:     {setpoint}°C" if setpoint is not None else "  Setpoint:     —")
        print(f"  Config target:{cfg.get('dhw_setpoint', '—')}°C")
        print(f"  Warm-only:    {'Yes' if cfg.get('dhw_warm_hours_only', True) else 'No'}")
    except Exception as e:
        print(f"  Error connecting to {ip}:3000 — {e}")
        return

    # Show upcoming warm hours for DHW
    if cfg.get("dhw_warm_hours_only", True):
        try:
            from airzone_weather import get_forecast, compute_warm_window
            forecast = get_forecast(cfg.get("latitude", 44.07),
                                    cfg.get("longitude", -1.26))
            dhw_hours = cfg.get("dhw_warm_hours_count", 3)
            w = compute_warm_window(forecast, dhw_hours)
            print(f"\n  DHW Warm Window ({dhw_hours} warmest hours):")
            print(f"  Currently:    {'IN warm window' if w.get('is_warm_now') else 'OUTSIDE warm window'}")
            print(f"  Outdoor temp: {w.get('current_outdoor_temp', '—')}°C")
            for h in w.get("warm_hours", []):
                marker = " ◀ NOW" if h["hour"][:13] == datetime.now().isoformat()[:13] else ""
                print(f"    {h['hour'][:16]}  {h['temp_c']:5.1f}°C{marker}")
            if w.get("next_warm_start"):
                print(f"  Next ON:      {w['next_warm_start'][:16]}")
        except Exception as e:
            print(f"  Weather error: {e}")
    print()


def dump_api(api: AirzoneCloudAPI):
    """Print raw API responses to help debug structure or field names."""
    print("\n=== /api/v1/installations ===")
    installations = api.get_installations()
    for inst in installations:
        inst_id = inst.get("installation_id") or inst.get("id", "")
        print(f"\n  Installation: {inst.get('name', '?')} (id={inst_id})")
        print(f"  Raw: {json.dumps(inst, indent=4)}")

        detail = api.get_installation_detail(inst_id)
        print(f"\n  ==> Installation detail (id={inst_id}):")
        print(json.dumps(detail, indent=4))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Airzone Cloud Humidity Controller")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--status", action="store_true",
                        help="Print all zone data and exit")
    parser.add_argument("--dump", action="store_true",
                        help="Print raw API JSON responses (useful for debugging)")
    parser.add_argument("--dhw-status", action="store_true",
                        help="Print DHW (hot water) status and exit")
    parser.add_argument("--dhw-on", action="store_true",
                        help="Turn DHW on immediately and exit")
    parser.add_argument("--dhw-off", action="store_true",
                        help="Turn DHW off immediately and exit")
    parser.add_argument("--dhw-set", type=float, metavar="TEMP",
                        help="Set DHW temperature and turn on (e.g. --dhw-set 50)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run without sending any commands")
    parser.add_argument("--once", action="store_true",
                        help="Check once and exit (instead of looping as daemon)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    setup_logging(cfg.get("log_file", "") if not args.status and not args.dump else "")

    if args.dry_run:
        cfg["dry_run"] = True

    email = cfg.get("email", "")
    password = cfg.get("password", "")
    if not email or not password:
        print("Error: 'email' and 'password' must be set in airzone_config.json")
        print("       (or via AIRZONE_EMAIL / AIRZONE_PASSWORD env vars)\n")
        sys.exit(1)

    api = AirzoneCloudAPI()

    # Try cached token first; fall back to login
    if not api.load_cached_tokens():
        api.login(email, password)

    # ── Dump mode
    if args.dump:
        dump_api(api)
        return

    # ── Status mode
    if args.status:
        zones = api.get_all_zones()
        print_status(zones)
        # Also show DHW if configured
        dhw_ip = cfg.get("dhw_local_api", "")
        if dhw_ip:
            print_dhw_status(dhw_ip, cfg)
        return

    # ── DHW quick commands
    dhw_ip = cfg.get("dhw_local_api", "")
    if args.dhw_status:
        if not dhw_ip:
            print("Error: 'dhw_local_api' not set in config")
            sys.exit(1)
        print_dhw_status(dhw_ip, cfg)
        return

    if args.dhw_on:
        if not dhw_ip:
            print("Error: 'dhw_local_api' not set in config")
            sys.exit(1)
        setpoint = cfg.get("dhw_setpoint", 20.0)
        print(f"Turning DHW ON (setpoint {setpoint}°C)...")
        dhw_set(dhw_ip, power=1, setpoint=setpoint)
        print("Done.")
        return

    if args.dhw_off:
        if not dhw_ip:
            print("Error: 'dhw_local_api' not set in config")
            sys.exit(1)
        print("Turning DHW OFF...")
        dhw_set(dhw_ip, power=0)
        print("Done.")
        return

    if args.dhw_set is not None:
        if not dhw_ip:
            print("Error: 'dhw_local_api' not set in config")
            sys.exit(1)
        temp = args.dhw_set
        print(f"Setting DHW to {temp}°C and turning ON...")
        dhw_set(dhw_ip, power=1, setpoint=temp)
        print("Done.")
        return

    # ── Daemon / one-shot
    log.info("Airzone Cloud Humidity Controller starting")
    log.info("  Thresholds: ON >= %d%%,  OFF <= %d%%",
             cfg["humidity_on_threshold"], cfg["humidity_off_threshold"])
    log.info("  Poll interval: %ds", cfg["poll_interval_seconds"])
    if cfg["dry_run"]:
        log.info("  *** DRY RUN — no commands will be sent ***")

    state = load_state()
    running = True

    def shutdown(sig, frame):
        nonlocal running
        log.info("Shutting down")
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while running:
        try:
            api.ensure_token(email, password)
        except Exception as e:
            log.error("Authentication error: %s — retrying next cycle", e)
        else:
            weather_info = None
            if cfg.get("weather_optimization", False):
                try:
                    from airzone_weather import get_forecast, compute_warm_window
                    forecast = get_forecast(cfg.get("latitude", 44.07),
                                            cfg.get("longitude", -1.26))
                    weather_info = compute_warm_window(
                        forecast, cfg.get("warm_hours_count", 6))
                except Exception as e:
                    log.error("Weather fetch failed: %s — proceeding without", e)
            state = check_and_control(api, cfg, state, weather_info=weather_info)

            # DHW control (prefers Cloud API, falls back to local API)
            if cfg.get("dhw_enabled"):
                try:
                    state = check_and_control_dhw(cfg, state,
                                                  weather_info=weather_info,
                                                  api=api)
                except Exception as e:
                    log.error("DHW control error: %s", e)

        if args.once:
            break

        deadline = time.time() + cfg["poll_interval_seconds"]
        while running and time.time() < deadline:
            time.sleep(1)

    log.info("Stopped")


if __name__ == "__main__":
    main()

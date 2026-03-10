# Session Summary: DHW Cloud API Integration

## What Was Done

### 1. DHW Tab Added to macOS GUI (`src/airzone_app.py`)
- Added a **Hot Water** tab alongside the existing **Zones** tab using `QTabWidget`
- Tab shows: tank temperature (large display), power state (ON/OFF), setpoint, weather schedule
- Controls: ON/OFF buttons, temperature spinner (40-65°C) with Set button
- Weather-Based Schedule section shows outdoor temp, warm window timing, HEATING/STANDBY status
- Refresh button for manual status updates

### 2. Cloud API DHW Support (`src/airzone_humidity_controller.py`)
Added three new methods to `AirzoneCloudAPI`:
- **`get_dhw_devices()`** — discovers DHW devices (`az_acs` / `aidoo_acs` type) across all installations
- **`get_dhw_status()`** — reads tank temp, power, setpoint via Cloud API (returns same format as local API)
- **`set_dhw(device_id, installation_id, power, setpoint, powerful_mode)`** — controls DHW via Cloud PATCH

### 3. Dual-Backend DHW Control
- `check_and_control_dhw()` now accepts optional `api` parameter
- **Cloud API preferred** (works from anywhere), **Local API fallback** (when `dhw_local_api` is set)
- GUI workers (`DHWStatusWorker`, `DHWCommandWorker`) support both backends
- No local IP or VPN needed for basic DHW control

### 4. Settings Dialog Updated
- DHW section shows "Uses your Airzone Cloud account (works from anywhere)"
- Local API IP field is now optional (labeled "optional — for local network fallback")
- Test Connection tries local first (if IP provided), then falls back to Cloud API

### 5. Config Fields (in `DEFAULT_CONFIG` and `airzone_config.json`)
```json
{
  "dhw_enabled": false,
  "dhw_setpoint": 50.0,
  "dhw_local_api": "",
  "dhw_warm_hours_only": true,
  "dhw_warm_hours_count": 3
}
```

### 6. Auto-Control Integration
- `PollWorker` now calls `check_and_control_dhw()` with Cloud API when auto-control is enabled
- CLI daemon loop also passes `api` to `check_and_control_dhw()`

## Files Modified
| File | Changes |
|------|---------|
| `src/airzone_humidity_controller.py` | Added `get_dhw_devices()`, `get_dhw_status()`, `set_dhw()` to Cloud API class; updated `check_and_control_dhw()` for dual backend; daemon loop passes `api` |
| `src/airzone_app.py` | Added `DHWTab`, `DHWStatusWorker`, `DHWCommandWorker` with Cloud API support; added DHW tab to MainWindow; updated Settings dialog; PollWorker runs DHW control |
| `airzone_config.json` | Has DHW fields (dhw_enabled, dhw_setpoint, dhw_local_api, dhw_warm_hours_only, dhw_warm_hours_count) |

## How It Works
1. User enables DHW in Settings (just tick the checkbox — no IP needed)
2. App uses existing Airzone Cloud login to discover DHW device automatically
3. Hot Water tab shows live tank temp, power state, and controls
4. With auto-control + weather optimization: DHW only heats during warmest hours (better COP)
5. Works from anywhere in the world via Cloud API

## Pending / Notes
- The Airzone webserver local IP is likely `192.168.1.16` (found via Deco app) — but untested since the system is in France
- Cloud API DHW depends on the installation having an `az_acs` or `aidoo_acs` device registered — needs testing with the actual account
- macOS app was built successfully with `bash build_app.sh` → `dist/Airzone.app`

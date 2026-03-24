# PROJECT SUMMARY — airzone

## Project Name & Purpose
**Airzone HVAC smart controller** — a Python/PyQt5 desktop app and CLI daemon that manages Airzone climate zones (France property), integrating with the Airzone Cloud API, Netatmo weather station, Linky electricity meter, and a domestic hot water (DHW) system. Includes an energy analytics module and a "best price" electricity optimiser.

## Summary of Work Done

### Core Application (`src/`)
- `airzone_app.py` — main PyQt5 GUI (Zones tab + Hot Water tab)
- `airzone_humidity_controller.py` — Cloud API client + dual-backend DHW control
- `airzone_control_brain.py` — main control logic (auto-control, scheduling)
- `airzone_thermal_model.py` — thermal physics model for heating predictions
- `airzone_netatmo.py` — Netatmo weather station integration
- `airzone_linky.py` — Linky electricity meter integration
- `airzone_weather.py` — weather forecast client
- `airzone_analytics.py` — energy usage analytics
- `airzone_best_price.py` — electricity price optimisation
- `airzone_baseline.py` — baseline energy consumption tracking

### DHW (Hot Water) Integration — Recently Added
- Cloud API DHW discovery (`get_dhw_devices()`), status, and control
- GUI Hot Water tab with temperature display, ON/OFF controls, weather-based schedule
- Dual-backend: Cloud API (primary, works remotely) + local API (optional fallback)

### Compiled Executables
- `AirzoneCollector.exe` — Windows data collector
- `dist/` and `dist (1)/` — macOS/Windows app bundles
- `AirzoneCollector.spec` — PyInstaller spec

## Current Status & Open Threads
- **Core app**: Complete and functional
- **DHW integration**: Built but needs testing with actual Airzone Cloud account (az_acs device)
- Local Airzone webserver IP estimated as `192.168.1.16` — needs confirmation
- `Best Price/` — electricity price optimisation module
- `pi/` — Raspberry Pi deployment variant
- `docs/SESSION_DHW_CLOUD.md` — latest DHW session log
- `docs/SESSION_LOG.md` — running project session log
- `airzone_config.json` — main config (keep private — contains cloud credentials)

## Key File Locations
```
airzone/
  airzone_config.json               ← Main config (API credentials, device IDs)
  src/
    airzone_app.py                  ← Main PyQt5 GUI entry point
    airzone_control_brain.py        ← Auto-control logic
    airzone_humidity_controller.py  ← Cloud API + DHW client
    airzone_thermal_model.py        ← Thermal physics model
    airzone_netatmo.py              ← Netatmo integration
    airzone_linky.py                ← Linky meter integration
    airzone_best_price.py           ← Electricity price optimiser
    airzone_analytics.py            ← Analytics module
  pi/                               ← Raspberry Pi deployment
  docs/
    SESSION_LOG.md                  ← Running project log
    SESSION_DHW_CLOUD.md            ← DHW Cloud API session notes
  AirzoneCollector.exe              ← Windows collector executable
  AirzoneCollector.spec             ← PyInstaller build spec
```

## Dependencies / How to Run
```bash
pip install PyQt5 requests python-dotenv --break-system-packages

# GUI:
python3 src/airzone_app.py

# CLI daemon:
python3 src/airzone_control_brain.py
```

## Configuration Notes
- `airzone_config.json` holds all credentials and device IDs — keep private
- Cloud API works from anywhere; local API requires same LAN as the Airzone webserver
- DHW `dhw_enabled: false` by default — enable in config once tested

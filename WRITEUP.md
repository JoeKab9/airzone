# Airzone

## Project Overview

A **home HVAC humidity controller** for the **Airzone** zoned climate control system. A macOS desktop application (PyQt5) that monitors temperature and humidity per zone, visualizes historical data, auto-controls heating based on humidity thresholds, manages domestic hot water (DHW), integrates with energy consumption data (Linky), and includes analytics for heating cycle optimization.

## Purpose

Control and monitor a multi-zone Airzone HVAC system installed in a French home:
- Display live temperature, humidity, and on/off status per zone
- Automatically enable/disable heating when humidity exceeds thresholds
- Manage domestic hot water (DHW) via cloud API
- Track energy consumption via Linky smart meter integration
- Analyze heating cycle efficiency and optimize warm-up hours
- Provide historical graphs and analytics

## Technologies and Frameworks

- **Python 3**
- **PyQt5** (macOS desktop GUI with tabs, tables, graphs)
- **matplotlib** (embedded charts in Qt5 via FigureCanvasQTAgg)
- **requests** (HTTP client for Airzone Cloud API)
- **SQLite** (local history database for readings)
- **Flask + Gunicorn** (Raspberry Pi web dashboard)
- **PyInstaller** (packaged as macOS `.app`)
- **OS Keychain** (secure credential storage)

## File/Folder Structure

```
airzone/
  src/
    airzone_app.py                  # Main PyQt5 GUI application
    airzone_humidity_controller.py  # Core Airzone API + humidity logic
    airzone_weather.py              # Weather data integration
    airzone_analytics.py            # Heating cycle analysis & optimization
    airzone_linky.py                # Linky/Enedis energy meter integration
    airzone_netatmo.py              # Netatmo weather station integration
    airzone_secrets.py              # OS keychain credential management
  pi/
    airzone_daemon.py               # Raspberry Pi background collector
    airzone_dashboard.py            # Flask web dashboard for Pi
    airzone_db.py                   # Pi-side database management
    airzone_pi_config.json          # Pi configuration
  icons/
    make_icon.py                    # Icon generation script
    icon.png                        # App icon
    Airzone.iconset/                # macOS iconset (multiple sizes)
  docs/
    devis_chauffage/                # Heating system quotes/invoices (French)
    devis_plomberie/                # Plumbing quotes/invoices
    factures_chauffage/             # Heating invoices
    factures_plomberie/             # Plumbing invoices
    plans/                          # Floor plans with heating layout
    manuals/                        # Equipment manuals (Mitsubishi Ecodan, etc.)
    duplicates/                     # Duplicate document copies
    SESSION_LOG.md                  # Development session log
    SESSION_DHW_CLOUD.md            # DHW cloud feature notes
  data/
    airzone_config.json             # Application configuration
    .airzone_tokens.json            # API authentication tokens
    airzone_weather_cache.json      # Cached weather data
  build/Airzone/                    # PyInstaller build artifacts
  dist/                             # Distribution output
  Airzone.spec                      # PyInstaller spec file
```

## Key Components

### airzone_app.py — Main GUI Application

A full-featured PyQt5 desktop application with:
- **Zone table**: Live temperature, humidity, power state, and outdoor temp per zone
- **Click-to-graph**: Click any zone row to see historical temperature/humidity plots
- **Humidity auto-control**: Configurable threshold — automatically enables heating when humidity exceeds limit
- **DHW management**: Domestic hot water on/off control via Airzone Cloud API
- **Settings dialog**: API credentials, zone configuration, polling interval
- **Local SQLite DB**: Every poll records zone data for graph history
- **Tab-based UI**: Zones, Analytics, Energy (Linky), Settings

### airzone_humidity_controller.py — Core Logic

Implements `AirzoneCloudAPI` client:
- OAuth authentication with token persistence
- Zone temperature/humidity polling
- Heating mode control per zone
- DHW status/control
- Multiple operating modes (heat, cool, ventilation, dry, auto)

### airzone_analytics.py — Heating Analytics

Analyzes heating patterns:
- Cycle detection (heating on/off transitions)
- Zone thermal profiling (how fast each zone heats/cools)
- Optimal warm-up hour computation
- Temperature band efficiency analysis

### airzone_linky.py — Energy Consumption

French smart meter integration:
- Linky load curve data retrieval
- Enedis file import (CSV/Excel from grid operator website)
- Energy analysis by time period
- Correlation with heating cycles

### Raspberry Pi Deployment

Separate daemon and dashboard for headless operation:
- `airzone_daemon.py`: Background data collector running on Pi
- `airzone_dashboard.py`: Flask web dashboard accessible from any browser
- The macOS app can fall back to the Pi dashboard API for historical data

## Hardware Context

The `docs/` folder reveals the physical installation:
- **Heat pump**: Mitsubishi Electric Ecodan (PUHZ-SHW80-230) — air-to-water
- **Airzone system**: Zoned climate control with per-room temperature/humidity sensors
- **Location**: France (quotes/invoices from French contractors — "BEEREPOOT Chauffage")
- **Includes**: Underfloor heating (plancher chauffant) and a gainable heat pump system

## macOS App Packaging

Built with PyInstaller into a standalone `.app`:
- Custom Airzone icon (multiple resolutions in `.iconset`)
- Frozen-aware path handling for bundled resources
- Credentials stored in macOS Keychain (not in config files)

## Dependencies

```
PyQt5          # GUI framework
requests       # HTTP client
matplotlib     # Embedded charts
Flask          # Pi web dashboard
gunicorn       # Pi WSGI server
pyinstaller    # macOS app packaging
Pillow         # Icon processing
```

## Observations

- Very complete home automation application — from cloud API integration to local analytics
- Dual deployment: macOS desktop app + Raspberry Pi headless daemon
- The humidity-based heating control is practical: prevents condensation in French homes during cold/humid seasons
- Extensive documentation in `docs/` including actual contractor quotes, invoices, and equipment manuals
- The Linky integration is French-specific (Linky is the French smart electricity meter)
- Secure credential handling via OS keychain rather than plaintext config files
- Active development tracked via session logs

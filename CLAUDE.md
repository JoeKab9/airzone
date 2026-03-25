> ⚠️ **SESSION START — Read `CLAUDE_RULES.md` before writing any code. Acknowledge the rules before proceeding.**

# Airzone — Claude Instructions

Smart HVAC controller for a French property (Contis, Landes). Reads from the Airzone Cloud API, Netatmo weather station, and Linky electricity meter. Controls heating zones and domestic hot water (DHW). Includes an energy analytics module and electricity price optimiser.

## Tech Stack
- Python 3.12, PyQt5 (desktop GUI), SQLite, requests
- Airzone Cloud API, Netatmo API, Enedis Linky (via data-connect)
- PyInstaller for compiled executables (Mac + Windows)
- Raspberry Pi variant in `pi/` for always-on server deployment

## Project Structure
```
airzone/
  src/
    airzone_app.py                  ← Main PyQt5 GUI entry point
    airzone_control_brain.py        ← Auto-control + scheduling logic
    airzone_humidity_controller.py  ← Cloud API client + DHW control
    airzone_thermal_model.py        ← Thermal physics model
    airzone_netatmo.py              ← Netatmo weather station
    airzone_linky.py                ← Linky electricity meter
    airzone_best_price.py           ← Electricity price optimiser
    airzone_analytics.py            ← Energy analytics
    airzone_weather.py              ← Weather forecast client
    airzone_baseline.py             ← Baseline consumption tracking
    airzone_secrets.py              ← Loads credentials from secrets file
  scripts/
    airzone_collector.py            ← CLI data collector
    airzone_collector_gui.py        ← Collector GUI
    airzone_dashboard_server.py     ← Local dashboard server
    airzone_poller_server.py        ← Polling daemon
    requirements.txt                ← Python dependencies
    build_app.sh                    ← macOS build script
  pi/
    airzone_daemon.py               ← Pi always-on daemon
    airzone_dashboard.py            ← Pi dashboard
    airzone_db.py                   ← Pi database layer
    install.sh                      ← Pi setup script
    static/ + templates/            ← Pi web dashboard
  Best Price/
    best_price.py                   ← Standalone price optimiser
  docs/
    SESSION_LOG.md                  ← Running project log
    SESSION_DHW_CLOUD.md            ← DHW Cloud API session notes
  airzone_config.json               ← Main config (SECRETS — never commit)
  .env.example                      ← Template for .env
```

## Secrets (not in git — in Google Drive or ~/.ssh area)
- `airzone_config.json` — Airzone Cloud credentials, device IDs, zone config
- `data/.airzone_secrets.json` — additional API secrets
- `pi/data/.airzone_tokens.json` — Pi OAuth tokens

## Hetzner Server
| Field | Value |
|-------|-------|
| IP | 65.108.147.47 |
| Path | `/opt/airzone/` |
| URL | http://65.108.147.47/airzone/ |
| Auth | admin / 2183 |
| SSH | `ssh -i ~/.ssh/id_ed25519_hetzner root@65.108.147.47` |

## Deploy to Hetzner
```bash
git push
ssh -i ~/.ssh/id_ed25519_hetzner root@65.108.147.47 \
  "cd /opt/airzone && git pull && systemctl restart airzone-daemon airzone-dashboard"
```

## Run Locally
```bash
pip install -r scripts/requirements.txt
python src/airzone_app.py          # PyQt5 GUI
python scripts/airzone_collector.py  # CLI collector
```

## Current Status
- Core app: complete and functional
- DHW (hot water) integration: built, needs testing with real az_acs device
- `dhw_enabled: false` in config by default — enable once tested
- Local Airzone webserver IP estimated as `192.168.1.16` — confirm on-site

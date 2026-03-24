# Airzone Collector - Session Summary (2026-03-24)

## What Was Built

### Airzone Data Collector (Windows EXE)
A standalone Windows GUI application that polls the Airzone Cloud API every 5 minutes and stores **all** data points in a local SQLite database.

**Features:**
- Start/Stop button for automatic polling
- Manual "Poll Now" button
- "DB Stats" button showing record counts, date range, file size
- "Copy Log" button to copy the log window to clipboard
- Custom HVAC-style icon (dark blue circle with airflow waves)
- All 11 devices polled: 1 DHW (hot water), 8 zones, 2 other
- Calls spread evenly across the 5-minute interval (~25s apart) to avoid API rate limits (429 errors)
- Retry logic with backoff on rate-limited responses

**Data collected per zone:** temperature, humidity, setpoint, power state, mode, and full raw JSON snapshot
**Data collected for DHW:** tank temperature, setpoint, power state, raw JSON
**Also stored:** installation snapshots, full poll snapshots with all raw API responses

### Database Location
- **Local (active writes):** `C:\Users\mauri\AppData\Local\AirzoneCollector\airzone_raw.db`
- **Google Drive (auto-synced copy):** `G:\Other computers\My Mac\ClaudeCodeProjects\airzone\collector_data\airzone_raw.db`
- SQLite can't write directly to Google Drive (sync locks cause disk I/O errors), so the app writes locally and copies to Drive after each successful poll

### Hetzner VPS Deployment
A second independent poller was deployed to a Hetzner CX23 server (Helsinki):
- **IP:** 65.108.147.47
- **SSH:** `ssh -i ~/.ssh/id_ed25519_hetzner root@65.108.147.47`
- **Install path:** `/opt/airzone/`
- **Python venv:** `/opt/airzone/venv/`
- **Database:** `/opt/airzone/data/airzone_raw.db`
- **Services:**
  - `airzone-poller.service` — polls every 5 min (systemd timer or built-in loop)
  - `airzone-dashboard.service` — Flask web dashboard on port 5001
- **Nginx:** reverse proxy at `/airzone/` -> `localhost:5001`
- **Dashboard URL:** `http://65.108.147.47/airzone/`
- Credentials stored in `/opt/airzone/.env`

**Important:** Do NOT touch `/opt/homecomfort/` on that server — separate project.

## Key Files

| File | Purpose |
|------|---------|
| `scripts/airzone_collector_gui.py` | Main collector GUI app (tkinter) |
| `scripts/airzone_collector.py` | Core polling logic (no GUI) |
| `scripts/airzone_collector.ico` | Custom icon for the EXE |
| `scripts/airzone_dashboard_server.py` | Flask dashboard (deployed to Hetzner) |
| `scripts/build_windows.bat` | PyInstaller build script |
| `AirzoneCollector.exe` | Built EXE (root of project) |
| `collector_data/` | Google Drive sync folder for DB copies |
| `.env` | Airzone credentials (AIRZONE_EMAIL, AIRZONE_PASSWORD) |

## Issues Encountered & Fixed

1. **`load_config()` missing path argument** — fixed to auto-detect config path relative to exe/script
2. **No credentials found** — `.env` wasn't bundled; fixed fallback loader to check exe directory
3. **setpoint=None** — API returns setpoint under `setpoint_air_heat`/`setpoint_air_cool`, not `setpoint`; fixed field lookup
4. **429 Too Many Requests** — API rate-limits ~10 calls/min; fixed by spreading calls 25s apart across the 5-min window + retry with backoff
5. **Euro sign (€) in password** — UTF-8 encoding issue made password appear as 12 bytes instead of 10 chars; fixed `.env` parser to read as UTF-8
6. **SQLite disk I/O on Google Drive** — Drive sync locks the DB file; fixed by writing to local AppData and auto-copying to Drive
7. **Dashboard "Loading" with no data** — fetch URLs were absolute (`/api/current`) but nginx serves under `/airzone/`; fixed to use relative base path
8. **EXE deleted after reboot** — Windows Defender quarantined unsigned EXE; added exclusion instructions

## Database Schema (SQLite)

Main tables:
- `zone_readings` — temperature, humidity, setpoint, power, mode per zone per poll
- `dhw_readings` — tank temp, setpoint, power for hot water
- `other_devices` — any non-zone, non-DHW devices
- `poll_snapshots` — full raw JSON from each poll cycle
- `installation_snapshots` — installation-level metadata

## Current State (as of end of session)

- **Windows collector:** Working, polling every 5 min, DB has 312+ zone readings from 2026-03-16 onward
- **Hetzner poller:** Running independently as systemd service
- **Hetzner dashboard:** Accessible at `http://65.108.147.47/airzone/`
- **Data syncing:** Local DB auto-copies to Google Drive `collector_data/` folder

## To Resume

1. **Run the collector:** Double-click `AirzoneCollector.exe`, click Start
2. **Check server:** `ssh -i ~/.ssh/id_ed25519_hetzner root@65.108.147.47` then `systemctl status airzone-poller`
3. **View dashboard:** Open `http://65.108.147.47/airzone/` in browser
4. **Rebuild EXE:** `pyinstaller --noconfirm --onefile --windowed --name AirzoneCollector --icon scripts/airzone_collector.ico --add-data "src;src" --add-data "data;data" --hidden-import airzone_humidity_controller --hidden-import airzone_secrets --paths src scripts/airzone_collector_gui.py`
5. **Add Defender exclusion:** Windows Security > Virus & threat protection > Manage settings > Exclusions > Add file > point to `AirzoneCollector.exe`

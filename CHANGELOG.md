# Airzone - What Was Done & Next Steps

## Changes Made (10 Mar 2026)

### Phase 1: DP-Spread Predictive Controller (`src/airzone_control_brain.py`)

**New file**: `src/airzone_control_brain.py` — Complete port of the Loveable TypeScript control intelligence to Python.

**What it does**:
- **DP spread-based control**: Replaces simple humidity thresholds (70%/65%) with dew point spread hysteresis (heat ON < 4°C spread, OFF ≥ 6°C)
- **Sensor fusion**: Uses Netatmo humidity (more accurate) with averaged Airzone+Netatmo temperatures per zone
- **Predictive shutoff**: Accounts for concrete thermal runoff — stops heating 1°C early because the floor continues to warm
- **DP spread predictions**: Forecasts spread 3h ahead using weather data + learned thermal model
- **Decision cascade**: Emergency stop → Experiment block → Temp limit → Early stop (runoff) → Skip/Defer/Heat
- **COP-aware deferral**: Defers heating to warmer outdoor hours for better heat pump efficiency
- **Occupancy detection**: Uses Netatmo CO2 (>600ppm) and noise (>45dB) signals
- **Daily assessment**: Estimates kWh, cost, and DP spread improvement for each day
- **Linky reconciliation**: Compares estimated kWh with actual meter reading, learns correction factor
- **Self-learning**: Gradient descent on prediction bias adjusts infiltration rate over time
- **All data in local SQLite**: `control_log`, `daily_assessment`, `system_state`, `dp_spread_predictions` tables

**Sensor mapping** (Netatmo → Airzone):
- "Cuisine Base" → "Cuisine"
- "Boyz" → "Studio"
- "Slaapkamer" → "Mur bleu"

**Integration**:
- **macOS app** (`src/airzone_app.py`): PollWorker uses ControlBrain when `dp_spread_control: true` (default). Falls back to legacy controller on error.
- **Pi daemon** (`pi/airzone_daemon.py`): Same — uses brain with fallback to legacy.
- **Zone table**: New "DP Spread" column with color coding (red ≤2°, orange <4°, yellow <6°, green ≥6°)
- **LocalHistoryDB**: Creates brain tables on startup
- **Build scripts**: Added `airzone_control_brain` hidden import

**Key constants**:
- `DP_SPREAD_HEAT_ON = 4` (heat when spread < 4°C)
- `DP_SPREAD_HEAT_OFF = 6` (stop when spread ≥ 6°C)
- `MAX_INDOOR_TEMP = 18` (never heat above 18°C)
- `TARIFF = 0.1927` (€/kWh)

## Changes Made (8 Mar 2026, session 2)

### Secure Credential Storage (`src/airzone_secrets.py`)
- **New module**: `airzone_secrets.py` — unified credential storage using OS keychain
  - macOS: Keychain Access (encrypted at rest by the Secure Enclave)
  - Windows: Windows Credential Manager (DPAPI-encrypted)
  - Fallback: restricted-permissions JSON file if keyring unavailable
- **Automatic migration**: On first run, plaintext credentials are moved from `airzone_config.json` to the keychain, then blanked from the config file
- **Token migration**: Airzone JWT tokens (`.airzone_tokens.json`) and Netatmo OAuth tokens (`netatmo_tokens.json`) are also migrated and the plaintext files are deleted
- **Secrets stored**: email, password, Netatmo client ID/secret, Netatmo OAuth tokens, Airzone JWT tokens, Linky token/PRM
- **Settings dialog**: Shows lock icon with active backend (e.g. "🔒 Credentials stored in: OS Keychain")
- **Dependency**: `keyring>=25.0` (added to requirements.txt and build scripts)

## Changes Made (8 Mar 2026)

### Heating Runoff Model + Dew Point Tracking (fully implemented)
- **Weather pipeline**: Open-Meteo now fetches dew point + outdoor humidity alongside temperature
- **DB schema**: Added `outdoor_dew_point`, `outdoor_humidity` to `zone_readings`; added `runoff_drop`, `runoff_duration_hours`, `runoff_trough_humidity`, `avg_outdoor_dew_point` to `heating_cycles`
- **Runoff analytics engine** (`src/airzone_analytics.py`):
  - `compute_runoff()` measures continued humidity drop after heating stops (thermal inertia)
  - `get_smart_early_off_adjustment()` learns per-zone runoff bucketed by 5 dew point bands (<0, 0-5, 5-10, 10-15, >15 C)
  - Conservative estimate (mean - 0.5*std), capped at 5%, with confidence scoring
- **Controller smart early-off** (`src/airzone_humidity_controller.py`):
  - New config keys: `smart_early_off` (bool), `smart_early_off_max` (int), `smart_early_off_min_cycles` (int)
  - Uses learned runoff + current outdoor dew point to stop heating earlier when safe
  - Logs decisions: "Smart early-off at 68% (band <0 C, runoff 3.2%)"
- **Dashboard**: Dew point line on outdoor chart, runoff/early-off columns in zone profiles and heating cycles, `/api/analytics/runoff` endpoint
- **macOS app**: Smart early-off checkbox + max spin in Settings, runoff stats and band breakdown in Analytics tab

### Netatmo Integration (`src/airzone_netatmo.py`)
- Full OAuth2 flow (authorization code + token refresh)
- `get_stations()` lists all modules with MAC addresses
- `get_measure()` fetches historical data with automatic pagination
- `fetch_all_modules()` bulk-fetches all modules for a date range
- SQLite storage in `netatmo_readings` table
- CLI: `--auth`, `--list`, `--fetch --days 30`
- **Next**: Add `netatmo_client_id` and `netatmo_client_secret` to config, run `--auth` once, then integrate into daemon polling loop

### Bug Fixes
- **SIGSEGV crash (8 Mar)**: PollWorker QThread got garbage-collected mid-run when `self.worker` was reassigned. Fixed with `deleteLater()` on all worker threads.
- **SIGABRT crashes (4-5 Mar)**: Already fixed in previous session (try/except in slot handlers)
- **Pi daemon**: Now passes `outdoor_humidity` to DB alongside `outdoor_dew_point`

### Pi DB Consistency
- `airzone_db.py` migration now adds both `outdoor_dew_point` and `outdoor_humidity` columns
- `log_readings()` accepts and stores `outdoor_humidity`
- Daemon extracts `current_outdoor_humidity` from weather info

## Next Steps

### Windows Deployment (priority)
The GUI app runs permanently on a Windows 11 machine with auto-control enabled.
The project syncs via Google Drive between Mac and Windows.

**On the Windows machine:**
1. Install Python 3.9+ from python.org
2. Open a terminal and run:
   ```
   pip install pyqt5 matplotlib requests openpyxl pyinstaller
   ```
3. The project folder syncs via Google Drive -- it will already be there
4. Build the Windows .exe:
   ```
   cd <project-folder>
   scripts\build_windows.bat
   ```
5. Copy `airzone_config.json` into `dist\Airzone\`
6. Run `dist\Airzone\Airzone.exe` -- enable auto-control, leave it running
7. Optionally also run the dashboard for web access:
   ```
   python pi\airzone_dashboard.py --port 5050
   ```

**On the MacBook (occasional monitoring):**
- Open the same project from Google Drive
- Run Airzone.app from `dist/` with auto-control OFF (monitoring only)
- Or access the Windows dashboard at `http://<windows-ip>:5050`
- **Never enable auto-control on both machines simultaneously**

**Remote dashboard access (outside the LAN):**
Use Cloudflare Tunnel (free) to expose the dashboard securely without port forwarding:
1. Create a free Cloudflare account, add a domain (or use a free subdomain)
2. Install `cloudflared` on the Windows machine:
   ```
   winget install cloudflare.cloudflared
   ```
3. Login and create a tunnel:
   ```
   cloudflared tunnel login
   cloudflared tunnel create airzone
   cloudflared tunnel route dns airzone airzone.yourdomain.com
   ```
4. Run the tunnel (points your domain to the local dashboard):
   ```
   cloudflared tunnel --url http://localhost:5050 run airzone
   ```
5. Access from anywhere: `https://airzone.yourdomain.com`
6. Optional: add Cloudflare Access (free for up to 50 users) for login protection

Alternative options:
- **Tailscale** (free): creates a private VPN mesh, access via `http://windows-tailscale-ip:5050` from any device with Tailscale installed. Simplest option, no domain needed.
- **ngrok** (free tier): `ngrok http 5050` gives a temporary public URL. Good for quick testing.

**Important:**
- The SQLite databases (`airzone_history.db`, `airzone_state.json`) must be LOCAL to each machine, not on Google Drive (corruption risk from concurrent access)
- The `src/` code and `airzone_config.json` CAN sync via Google Drive
- Each machine gets its own database in `~/Library/Application Support/Airzone/` (Mac) or `%APPDATA%\Airzone\` (Windows)

### Netatmo Setup
1. Create app at https://dev.netatmo.com/ to get `client_id` and `client_secret`
2. Add to `airzone_config.json`:
   ```json
   "netatmo_enabled": true,
   "netatmo_client_id": "YOUR_CLIENT_ID",
   "netatmo_client_secret": "YOUR_CLIENT_SECRET"
   ```
3. Run: `python src/airzone_netatmo.py --auth --client-id X --client-secret Y`
4. Then: `python src/airzone_netatmo.py --fetch --days 365` to pull historical data
5. Modules: outdoor, kitchen, studio, mur bleu

### Energy Price Analysis
Three EDF tariff screenshots are in the project (`IMG_9726.jpeg` TEMPO, `IMG_9727.jpeg` Heures Creuses, `IMG_9728.jpeg` BASE). Analysis to be done when requested.

### Strategy Improvements
Analysis showed only 30% of heating cycles reduced humidity. Key findings:
- Cycles too short (20-40 min) -- need longer sustained heating
- Humidity trends worsening in most zones
- Passive ventilation (doorframe vents) means outdoor moisture continuously enters
- The runoff model will help optimize when to stop, but cycle duration needs to increase

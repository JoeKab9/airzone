# Claude Session Log — 2026-03-04

## What this session did

### 1. Polish pass (earlier)
- Added `requirements.txt` (PyQt5, requests, matplotlib, Flask, gunicorn, pyinstaller, Pillow)
- Expanded `.gitignore` (build/, dist/, __pycache__/, *.pyc, *.spec, .DS_Store, caches, configs)
- Rebuilt macOS app

### 2. Local history database (so graph works without Pi)
- Added `LocalHistoryDB` class to `airzone_app.py` — SQLite store for zone readings
- Every poll now records temperature, humidity, power state, and outdoor temp
- Clicking a zone row shows the graph using local data (no Pi dashboard needed)
- Falls back to Pi dashboard API if a URL is configured in Settings

### 3. Folder cleanup — source code moved to `src/`
- Created `src/` and moved: `airzone_app.py`, `airzone_humidity_controller.py`, `airzone_weather.py`
- Updated all path references:
  - `src/airzone_humidity_controller.py`: `SCRIPT_DIR = Path(__file__).parent.parent` (non-frozen)
  - `src/airzone_weather.py`: same `.parent.parent` change
  - `src/airzone_app.py`: `LOCAL_DB_PATH = CONFIG_PATH.parent / "airzone_history.db"` (frozen-aware)
  - `build_app.sh`: `--paths "src"` + entry point `src/airzone_app.py`
  - `airzone.command`: `python3 src/airzone_app.py`
  - `pi/airzone_daemon.py`: `sys.path.insert(0, str(PARENT_DIR / "src"))`
- Rebuilt macOS app successfully

### 4. DB persistence fix
- `LOCAL_DB_PATH` was using `Path(__file__).parent` which resolves to a temp dir inside a PyInstaller bundle — data was lost on restart
- Fixed by anchoring to `CONFIG_PATH.parent` (already frozen-aware from the controller)
- DB now persists across app restarts

## Files modified
- `src/airzone_app.py` (moved + edited)
- `src/airzone_humidity_controller.py` (moved + edited)
- `src/airzone_weather.py` (moved + edited)
- `pi/airzone_daemon.py` (sys.path update)
- `build_app.sh` (paths)
- `airzone.command` (paths)
- `.gitignore` (expanded)
- `requirements.txt` (created)

## Current folder structure
```
airzone/
  src/                  ← source code (3 Python files)
  pi/                   ← Raspberry Pi daemon + dashboard
  icons/                ← icon generation assets
  docs/                 ← manuals, invoices
  build_app.sh          ← build script
  airzone.command       ← launcher
  Airzone.icns          ← app icon
  requirements.txt
  .gitignore
  dist/Airzone.app      ← built app (gitignored)
```

## NOT touched by this session
- Pi daemon logic / dashboard / DB schema
- Weather optimization logic
- DHW (hot water) features
- Settings dialog content
- Any UI styling or layout
- Version tracking / credentials standardization

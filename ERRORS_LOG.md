# Airzone Project — Errors & Issues Encountered

## Critical Bugs

### 1. Zone Deferral Bug (Critical)
**When**: Early development of `check_and_control()` in `airzone_humidity_controller.py`
**What**: Zones already in the `pending` (deferred) dict fell through the `elif` chain and got immediately activated — creating an on/off oscillation loop.
**Root cause**: `dev_id not in pending` condition in the deferral branch caused zones already pending to skip deferral, then fall through to `elif dev_id not in activated:` which activated them immediately.
**Fix**: Added `and dev_id not in pending` to the activation condition.

### 2. Warm Window Cycling (Critical)
**When**: March 7, 2026 — 4 on/off cycles in 3 hours
**What**: System turned heating ON at warm window start, then OFF 5 min later ("outside warm window"), then ON again.
**Root cause**: After Phase 0b activated pending zones, the next poll re-entered the zone loop. If `is_warm` flipped to False, the code turned the zone OFF and deferred it again.
**Fix**: Once a zone is activated for humidity control, it runs until humidity drops to `off_thresh` or the 18°C temp cap — never turned off based on warm window alone.

### 3. Pending Zones Never Activating
**When**: Zones stuck in "deferred" state for 29+ hours
**What**: Even during warm window, pending zones wouldn't activate.
**Root cause**: In the zone loop, `elif dev_id not in activated and dev_id not in pending` — when zone IS in pending AND warm window IS active, no branch matched.
**Fix**: Restructured elif chain to handle pending+warm and pending+not-warm cases separately.

## PyQt5 Crashes (6 total)

### 4. SIGABRT — Unhandled Slot Exceptions (5 crashes)
**When**: March 4-5, 2026
**What**: App crashed with SIGABRT from `pyqt5_err_print() → QMessageLogger::fatal()`
**Root cause**: Unhandled Python exceptions in QTimer slot handlers (`_on_zones`, `_on_state`, `_on_weather`). PyQt5 calls `qFatal()` → `abort()` when exceptions escape slots.
**Fix**: Wrapped all timer-connected slots in `try/except` guards.

### 5. SIGSEGV — QThread Garbage Collection (1 crash)
**When**: March 8, 2026
**What**: NULL pointer dereference when `self.worker.isRunning()` was called on a deleted C++ object.
**Root cause**: `self.worker.finished.connect(self.worker.deleteLater)` freed the C++ QThread object, but the Python reference wasn't cleared.
**Fix**: Replaced `deleteLater` with `lambda: setattr(self, "worker", None)`.

### 6. Analytics Timer Dies Silently
**What**: Analytics tab stopped auto-updating after first run.
**Root cause**: `AnalyticsWorker` defined `finished = pyqtSignal(dict)` which shadowed QThread's built-in `finished` signal. `deleteLater()` destroyed the C++ object → `isRunning()` crashed silently → timer died.
**Fix**: Renamed signals to `analysis_done` and `linky_done`.

## Data & API Issues

### 7. Linky API Response Format Change
**What**: `fetch_load_curve()` returned empty data despite API returning 200.
**Root cause**: conso.boris.sh API changed response format — `interval_reading` now at top level instead of nested under `meter_reading`.
**Fix**: Parser now checks both locations: `data.get("meter_reading", {}).get("interval_reading")` with fallback to `data.get("interval_reading")`.

### 8. SQLite Disk I/O Error (Linky Backfill)
**What**: `sqlite3.OperationalError: disk I/O error` during Linky backfill on Hetzner.
**Root cause**: Concurrent access — Flask web requests and background backfill thread both writing to the same DB. WAL mode was set but `HistoryDB.__init__` ran `_create_tables()` on every request, competing for write locks.
**Fix**: (a) Shared singleton HistoryDB instance instead of per-request creation, (b) Wrapped `_create_tables()` in `try/except sqlite3.OperationalError`.

### 9. Supabase Migration Crash — Integer Timestamp
**What**: `netatmo_sync_status` migration crashed with `AttributeError: 'int' object has no attribute 'replace'`
**Root cause**: `last_synced_ts` is a unix timestamp (integer), but code called `.replace("Z", "+00:00")` on it.
**Fix**: Added `isinstance(last_synced, (int, float))` check with `datetime.utcfromtimestamp()` for integer values.

## Python Compatibility

### 10. Python 3.9 Syntax Crashes
**What**: `dict | None` union syntax crashes on Python 3.9.
**Root cause**: Modern union type syntax requires Python 3.10+.
**Fix**: Added `from __future__ import annotations` to `airzone_analytics.py`, `airzone_linky.py`, `airzone_humidity_controller.py`, `airzone_weather.py`.

### 11. Timezone Mismatch (Pi)
**What**: Outdoor temp/dew point always None on Raspberry Pi.
**Root cause**: `datetime.now()` returns UTC on Pi, but Open-Meteo forecast times are Europe/Paris. Current hour never matched.
**Fix**: `datetime.now(ZoneInfo("Europe/Paris"))` for consistent timezone comparison.

## Logic & Data Bugs

### 12. `conn.total_changes` Cumulative Counter
**What**: Analytics reported wildly inflated "rows inserted" numbers.
**Root cause**: `conn.total_changes` is cumulative across the connection lifetime, not per-statement.
**Fix**: Replaced with `cur.rowcount` per statement.

### 13. Humidity Zero Treated as Missing
**What**: Humidity of 0% treated as `None` in cycle analysis.
**Root cause**: `if hum_start` evaluates to `False` when humidity is 0.
**Fix**: Changed all checks to `if hum_start is not None`.

### 14. Outdoor Temp Missing from Graph
**What**: Outdoor temperature line disappeared after data directory migration.
**Root cause**: `zones_ready` signal emitted before `weather_ready`, so `_outdoor_temp` was `None` when `_on_zones` logged readings.
**Fix**: Reordered signals: `weather_ready` fires before `zones_ready`.

### 15. Data Lost on App Rebuild
**What**: History DB wiped every time the macOS app was rebuilt.
**Root cause**: Data stored inside `Airzone.app/data/` which PyInstaller deletes on rebuild.
**Fix**: Moved persistent data to `~/Library/Application Support/Airzone/`.

## UI/UX Issues

### 16. Hardcoded Credentials in Git
**What**: Airzone Cloud email and password hardcoded in `airzone_app.py`, tracked in git.
**Fix**: Removed hardcoded creds, switched to `.env` file (project-local, gitignored).

### 17. Settings Dialog Too Tall
**What**: Linky settings section hidden below visible area on smaller screens.
**Fix**: Wrapped Settings dialog content in `QScrollArea`.

### 18. Duplicate CFG.tariff Getter
**What**: First definition referenced non-existent `#set-tariff` input, silently overridden.
**Root cause**: Old single-tariff input removed when multi-period tariffs were added, but old getter remained.
**Fix**: Removed dead first definition.

### 19. `durations` Variable Scoped Too Narrowly
**What**: Short-cycle count referenced `durations` outside its `if` block → would have thrown `ReferenceError`.
**Fix**: Moved `durations` declaration to outer scope.

---

*Last updated: 25 March 2026*

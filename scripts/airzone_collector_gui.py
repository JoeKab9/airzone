#!/usr/bin/env python3
"""
Airzone Data Collector — GUI
==============================
Windows GUI app that polls Airzone Cloud every 5 minutes,
stores all raw data in SQLite, and shows a live log.
"""
from __future__ import annotations

import json
import queue
import sqlite3
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timezone
from pathlib import Path

# ── Make src/ importable ──────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).resolve().parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_SRC_DIR = _BASE / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Add scripts dir too for non-frozen mode
_SCRIPTS_DIR = _BASE / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from airzone_humidity_controller import (  # noqa: E402
    AirzoneCloudAPI, load_config, CONFIG_PATH, DATA_DIR,
)

# ── Ensure .env is loaded (critical for frozen exe) ───────────────────────────
# The secrets module walks up from src/ which breaks inside PyInstaller.
# Manually load .env from known locations into os.environ as a fallback.
import os as _os, re as _re

def _load_dotenv_fallback():
    """Load .env into os.environ if secrets module can't find it."""
    candidates = [
        _BASE / ".env",
        _BASE / "data" / ".env",
        Path(r"G:\Other computers\My Mac\ClaudeCodeProjects\airzone\.env"),
    ]
    for p in candidates:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = _re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
                if m:
                    key, val = m.group(1), m.group(2).strip().strip("\"'")
                    if val and key not in _os.environ:
                        _os.environ[key] = val
            return

_load_dotenv_fallback()

# ── Constants ─────────────────────────────────────────────────────────────────

APP_DIR = _BASE / "collector_data"
DB_DIR = APP_DIR
LOG_DIR = APP_DIR / "logs"
DB_PATH = DB_DIR / "airzone_raw.db"
DEFAULT_INTERVAL = 300  # 5 minutes
MODE_NAMES = {1: "stop", 2: "cool", 3: "heat", 4: "fan", 5: "dry", 7: "auto"}


# ── Database ──────────────────────────────────────────────────────────────────

def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS poll_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            poll_type   TEXT NOT NULL,
            raw_json    TEXT NOT NULL
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_snap_ts
        ON poll_snapshots(timestamp)""")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS zone_readings (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL,
            installation_id     TEXT,
            installation_name   TEXT,
            device_id           TEXT NOT NULL,
            zone_name           TEXT,
            device_type         TEXT,
            temperature         REAL,
            humidity            INTEGER,
            setpoint_heat       REAL,
            setpoint_cool       REAL,
            power               INTEGER,
            mode                INTEGER,
            mode_name           TEXT,
            is_connected        INTEGER,
            air_active          INTEGER,
            aq_quality          REAL,
            raw_json            TEXT NOT NULL
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_zr_ts
        ON zone_readings(timestamp)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_zr_device
        ON zone_readings(device_id, timestamp)""")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS installation_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL,
            installation_id     TEXT NOT NULL,
            installation_name   TEXT,
            raw_json            TEXT NOT NULL
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_inst_ts
        ON installation_snapshots(timestamp)""")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dhw_readings (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL,
            installation_id     TEXT,
            device_id           TEXT NOT NULL,
            device_type         TEXT,
            power               INTEGER,
            setpoint            REAL,
            tank_temp           REAL,
            raw_json            TEXT NOT NULL
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_dhw_ts
        ON dhw_readings(timestamp)""")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS other_devices (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL,
            installation_id     TEXT,
            device_id           TEXT NOT NULL,
            device_type         TEXT,
            device_name         TEXT,
            raw_json            TEXT NOT NULL
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_other_ts
        ON other_devices(timestamp)""")

    conn.commit()
    return conn


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_celsius(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, dict):
        return val.get("celsius")
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Collector Engine (runs in background thread) ──────────────────────────────

class CollectorEngine:
    def __init__(self, log_queue: queue.Queue):
        self.api = AirzoneCloudAPI()
        self.conn: sqlite3.Connection | None = None
        # When frozen on Windows, CONFIG_PATH from the module may point to
        # a Mac-specific path.  Override to the project's data/ directory.
        if getattr(sys, "frozen", False):
            cfg_path = _BASE / "data" / "airzone_config.json"
            if not cfg_path.exists():
                # Fallback: same folder as the exe
                cfg_path = Path(sys.executable).parent / "data" / "airzone_config.json"
            if not cfg_path.exists():
                cfg_path = CONFIG_PATH  # last resort
        else:
            cfg_path = CONFIG_PATH
        self.cfg = load_config(cfg_path)
        self.log_q = log_queue
        self._authenticated = False
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_q.put(f"[{ts}] {msg}")

    def _authenticate(self):
        email = self.cfg.get("email", "")
        password = self.cfg.get("password", "")
        # Fallback: read directly from os.environ if config didn't have them
        if not email:
            email = _os.environ.get("AIRZONE_EMAIL", "")
            if email:
                self.cfg["email"] = email
        if not password:
            password = _os.environ.get("AIRZONE_PASSWORD", "")
            if password:
                self.cfg["password"] = password
        if not email or not password:
            self._log("ERROR: No credentials. Set AIRZONE_EMAIL/PASSWORD in .env")
            return False
        try:
            # Always try fresh login (cached tokens often cause 400 errors)
            self.api.login(email, password)
            self._authenticated = True
            self._log("Authenticated to Airzone Cloud")
            return True
        except Exception as e:
            self._log(f"ERROR: Authentication failed: {e}")
            self._log(f"  Email: {email[:3]}...{email[-8:]}, pass length: {len(password)}")
            return False

    def poll_once(self, interval: int = DEFAULT_INTERVAL):
        if not self._authenticated:
            if not self._authenticate():
                return

        try:
            self.api.ensure_token(self.cfg["email"], self.cfg["password"])
        except Exception as e:
            self._log(f"Token refresh failed: {e}")
            self._authenticated = False
            return

        now = _now_iso()
        total_zones = 0
        total_dhw = 0
        total_other = 0

        try:
            installations = self.api.get_installations()
        except Exception as e:
            self._log(f"ERROR: Failed to fetch installations: {e}")
            self._authenticated = False
            return

        self.conn.execute(
            "INSERT INTO poll_snapshots (timestamp, poll_type, raw_json) "
            "VALUES (?, ?, ?)",
            (now, "installations_list", json.dumps(installations, default=str))
        )

        # Pass 1: collect all devices and installation details
        all_devices = []  # list of (inst_id, inst_name, device_dict)
        for inst in installations:
            inst_id = inst.get("installation_id") or inst.get("id", "")
            inst_name = inst.get("name", "")

            try:
                detail = self.api.get_installation_detail(inst_id)
            except Exception as e:
                self._log(f"WARNING: Install detail failed for {inst_id}: {e}")
                continue

            self.conn.execute(
                "INSERT INTO installation_snapshots "
                "(timestamp, installation_id, installation_name, raw_json) "
                "VALUES (?, ?, ?, ?)",
                (now, inst_id, inst_name, json.dumps(detail, default=str))
            )

            for group in detail.get("groups", []):
                for device in group.get("devices", []):
                    all_devices.append((inst_id, inst_name, device))

        # Calculate delay to spread calls evenly over the interval
        # Use 80% of interval to leave headroom before next poll
        n_devices = len(all_devices)
        if n_devices > 1:
            spread_delay = (interval * 0.8) / n_devices
        else:
            spread_delay = 0
        # Minimum 25s to be safe, cap at 45s
        spread_delay = max(25.0, min(spread_delay, 45.0))
        self._log(f"Fetching {n_devices} devices ({spread_delay:.0f}s apart)...")

        # Pass 2: fetch status for each device, spaced evenly
        for idx, (inst_id, inst_name, device) in enumerate(all_devices):
            dev_type = device.get("type", "")
            dev_id = device.get("device_id") or device.get("id", "")
            dev_name = device.get("name", "")

            # Wait between calls (skip delay before first device)
            if idx > 0:
                # Check stop event during wait so Stop is responsive
                for _ in range(int(spread_delay)):
                    if self._stop_event.is_set():
                        self._log("Poll interrupted by stop.")
                        self.conn.commit()
                        return
                    time.sleep(1)

            # Fetch with retry on 429
            status = {}
            for attempt in range(3):
                if attempt > 0:
                    time.sleep(15.0)
                try:
                    status = self.api.get_device_status(dev_id, inst_id)
                    break
                except Exception as e:
                    if "429" in str(e) and attempt < 2:
                        self._log(f"  Rate-limited on {dev_name}, "
                                  f"retry {attempt + 1}/2...")
                        continue
                    self._log(f"  Status failed: {dev_name} ({dev_id}): {e}")
                    break

            merged = {**device, **status}
            raw = json.dumps(merged, default=str)
            # Use current time for each reading (more accurate)
            reading_ts = _now_iso()

            if dev_type == "az_zone":
                temp = _extract_celsius(
                    merged.get("local_temp") or merged.get("roomTemp"))
                humidity = merged.get("humidity")
                sp_heat = _extract_celsius(merged.get("setpoint_air_heat"))
                sp_cool = _extract_celsius(merged.get("setpoint_air_cool"))
                power = 1 if merged.get("power") else 0
                mode = merged.get("mode")
                mode_name = MODE_NAMES.get(mode, str(mode))
                connected = 1 if merged.get("isConnected") else 0
                air_active = 1 if merged.get("air_active") else 0
                aq = merged.get("aq_quality")

                self.conn.execute(
                    "INSERT INTO zone_readings "
                    "(timestamp, installation_id, installation_name, "
                    " device_id, zone_name, device_type, temperature, "
                    " humidity, setpoint_heat, setpoint_cool, power, "
                    " mode, mode_name, is_connected, air_active, "
                    " aq_quality, raw_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (reading_ts, inst_id, inst_name, dev_id, dev_name,
                     dev_type, temp, humidity, sp_heat, sp_cool,
                     power, mode, mode_name, connected, air_active,
                     aq, raw)
                )
                sp_display = sp_heat if sp_heat is not None else sp_cool
                self._log(f"  {dev_name}: {temp}C, {humidity}%, "
                          f"sp={sp_display}C, "
                          f"power={'ON' if power else 'OFF'}, "
                          f"mode={mode_name}")
                total_zones += 1

            elif dev_type in ("az_acs", "aidoo_acs"):
                dhw_power = 1 if merged.get("power") or merged.get("acs_power") else 0
                dhw_sp = _extract_celsius(
                    merged.get("setpoint_air_heat")
                    or merged.get("setpoint")
                    or merged.get("acs_setpoint"))
                tank = _extract_celsius(
                    merged.get("zone_work_temp")
                    or merged.get("local_temp")
                    or merged.get("tank_temp")
                    or merged.get("acs_temp"))

                self.conn.execute(
                    "INSERT INTO dhw_readings "
                    "(timestamp, installation_id, device_id, device_type, "
                    " power, setpoint, tank_temp, raw_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (reading_ts, inst_id, dev_id, dev_type,
                     dhw_power, dhw_sp, tank, raw)
                )
                self._log(f"  DHW {dev_name}: tank={tank}C, "
                          f"sp={dhw_sp}C, power={'ON' if dhw_power else 'OFF'}")
                total_dhw += 1

            else:
                self.conn.execute(
                    "INSERT INTO other_devices "
                    "(timestamp, installation_id, device_id, device_type, "
                    " device_name, raw_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (reading_ts, inst_id, dev_id, dev_type, dev_name, raw)
                )
                total_other += 1

        self.conn.commit()
        self._log(f"Poll OK: {total_zones} zones, {total_dhw} DHW, "
                  f"{total_other} other | DB: {DB_PATH.name}")

    def _run_loop(self, interval: int):
        self.conn = init_db(DB_PATH)
        self._log(f"Database: {DB_PATH}")
        if not self._authenticate():
            self._running = False
            return

        while not self._stop_event.is_set():
            try:
                # poll_once spreads calls over ~80% of the interval
                self.poll_once(interval=interval)
            except Exception as e:
                self._log(f"ERROR: {e}")
                self._authenticated = False

            # Short gap before next cycle (the spread is inside poll_once)
            for _ in range(10):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

        self.conn.close()
        self.conn = None
        self._log("Collector stopped.")
        self._running = False

    def start(self, interval: int = DEFAULT_INTERVAL):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, args=(interval,),
                                        daemon=True)
        self._thread.start()

    def stop(self):
        if not self._running:
            return
        self._log("Stopping collector...")
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        return self._running


# ── GUI ───────────────────────────────────────────────────────────────────────

class CollectorApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Airzone Data Collector")
        self.root.geometry("780x520")
        self.root.minsize(600, 400)
        self.root.configure(bg="#1e1e2e")

        # Set window icon
        try:
            if getattr(sys, "frozen", False):
                ico = Path(sys.executable).parent / "airzone_collector.ico"
            else:
                ico = Path(__file__).parent / "airzone_collector.ico"
            if ico.exists():
                self.root.iconbitmap(str(ico))
        except Exception:
            pass

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.engine = CollectorEngine(self.log_queue)
        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Colors
        bg = "#1e1e2e"
        fg = "#cdd6f4"
        accent = "#89b4fa"
        green = "#a6e3a1"
        red = "#f38ba8"
        surface = "#313244"

        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg,
                        font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=bg, foreground=accent,
                        font=("Segoe UI", 14, "bold"))
        style.configure("Status.TLabel", background=bg, foreground=fg,
                        font=("Segoe UI", 9))
        style.configure("Start.TButton", font=("Segoe UI", 11, "bold"),
                        padding=(20, 8))
        style.configure("Action.TButton", font=("Segoe UI", 9), padding=(12, 5))

        # Main frame
        main = ttk.Frame(self.root, padding=15)
        main.pack(fill=tk.BOTH, expand=True)

        # Header
        header = ttk.Frame(main)
        header.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(header, text="Airzone Data Collector",
                  style="Title.TLabel").pack(side=tk.LEFT)
        self.status_label = ttk.Label(header, text="Stopped",
                                      style="Status.TLabel")
        self.status_label.pack(side=tk.RIGHT)

        # Info bar
        info = ttk.Frame(main)
        info.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(info, text=f"DB: {DB_PATH}",
                  style="Status.TLabel").pack(side=tk.LEFT)
        ttk.Label(info, text="Interval: 5 min",
                  style="Status.TLabel").pack(side=tk.RIGHT)

        # Buttons
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(0, 10))

        self.start_btn = ttk.Button(btn_frame, text="Start",
                                    style="Start.TButton",
                                    command=self._toggle)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.poll_btn = ttk.Button(btn_frame, text="Poll Now",
                                   style="Action.TButton",
                                   command=self._poll_now)
        self.poll_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.copy_btn = ttk.Button(btn_frame, text="Copy Log",
                                   style="Action.TButton",
                                   command=self._copy_log)
        self.copy_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.clear_btn = ttk.Button(btn_frame, text="Clear Log",
                                    style="Action.TButton",
                                    command=self._clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.stats_btn = ttk.Button(btn_frame, text="DB Stats",
                                    style="Action.TButton",
                                    command=self._show_stats)
        self.stats_btn.pack(side=tk.RIGHT)

        # Log area
        self.log_text = scrolledtext.ScrolledText(
            main, wrap=tk.WORD, font=("Consolas", 9),
            bg=surface, fg=fg, insertbackground=fg,
            selectbackground=accent, selectforeground=bg,
            relief=tk.FLAT, borderwidth=0, padx=8, pady=8,
            state=tk.DISABLED
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Tag for different log levels
        self.log_text.tag_configure("error", foreground=red)
        self.log_text.tag_configure("ok", foreground=green)
        self.log_text.tag_configure("info", foreground=fg)
        self.log_text.tag_configure("data", foreground=accent)

        self._append_log("Airzone Data Collector ready.")
        self._append_log(f"Database: {DB_PATH}")
        self._append_log("Press Start to begin polling every 5 minutes.")

    def _append_log(self, msg: str):
        self.log_text.configure(state=tk.NORMAL)
        # Determine tag
        tag = "info"
        if "ERROR" in msg or "failed" in msg.lower():
            tag = "error"
        elif "Poll OK" in msg or "Authenticated" in msg:
            tag = "ok"
        elif msg.startswith("  "):
            tag = "data"

        self.log_text.insert(tk.END, msg + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_log_queue(self):
        """Drain the log queue into the text widget."""
        while True:
            try:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
            except queue.Empty:
                break

        # Update status
        if self.engine.is_running:
            self.status_label.configure(text="Running", foreground="#a6e3a1")
            self.start_btn.configure(text="Stop")
        else:
            self.status_label.configure(text="Stopped", foreground="#f38ba8")
            self.start_btn.configure(text="Start")

        self.root.after(200, self._poll_log_queue)

    def _toggle(self):
        if self.engine.is_running:
            self.engine.stop()
        else:
            self.engine.start(interval=DEFAULT_INTERVAL)

    def _poll_now(self):
        """Trigger a single poll in a background thread."""
        if self.engine.is_running:
            self._append_log("Already running — next poll will happen on schedule.")
            return

        # Disable button to prevent double-click (re-enabled after poll)
        self.poll_btn.configure(state=tk.DISABLED)

        def _do():
            conn = init_db(DB_PATH)
            self.engine.conn = conn
            self.engine._authenticated = False
            try:
                self.engine.poll_once()
            except Exception as e:
                self.engine._log(f"ERROR: {e}")
            finally:
                conn.close()
                self.engine.conn = None
                # Re-enable button from the main thread
                self.root.after(0, lambda: self.poll_btn.configure(state=tk.NORMAL))

        self._append_log("Manual poll starting...")
        threading.Thread(target=_do, daemon=True).start()

    def _copy_log(self):
        self.log_text.configure(state=tk.NORMAL)
        content = self.log_text.get("1.0", tk.END).strip()
        self.log_text.configure(state=tk.DISABLED)
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self._append_log("Log copied to clipboard.")

    def _clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _show_stats(self):
        """Show database statistics."""
        def _do():
            try:
                if not DB_PATH.exists():
                    self.engine._log("No database file yet.")
                    return
                conn = sqlite3.connect(str(DB_PATH), timeout=10)
                zones = conn.execute(
                    "SELECT COUNT(*) FROM zone_readings").fetchone()[0]
                dhw = conn.execute(
                    "SELECT COUNT(*) FROM dhw_readings").fetchone()[0]
                other = conn.execute(
                    "SELECT COUNT(*) FROM other_devices").fetchone()[0]
                snaps = conn.execute(
                    "SELECT COUNT(*) FROM poll_snapshots").fetchone()[0]
                inst = conn.execute(
                    "SELECT COUNT(*) FROM installation_snapshots").fetchone()[0]

                first = conn.execute(
                    "SELECT MIN(timestamp) FROM zone_readings").fetchone()[0]
                last = conn.execute(
                    "SELECT MAX(timestamp) FROM zone_readings").fetchone()[0]

                distinct_zones = conn.execute(
                    "SELECT COUNT(DISTINCT zone_name) FROM zone_readings"
                ).fetchone()[0]

                conn.close()

                import os
                size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)

                self.engine._log("--- Database Stats ---")
                self.engine._log(f"  File size: {size_mb:.2f} MB")
                self.engine._log(f"  Zone readings: {zones:,}")
                self.engine._log(f"  DHW readings: {dhw:,}")
                self.engine._log(f"  Other devices: {other:,}")
                self.engine._log(f"  Poll snapshots: {snaps:,}")
                self.engine._log(f"  Install snapshots: {inst:,}")
                self.engine._log(f"  Distinct zones: {distinct_zones}")
                self.engine._log(f"  First reading: {first or 'N/A'}")
                self.engine._log(f"  Last reading: {last or 'N/A'}")
                self.engine._log("----------------------")
            except Exception as e:
                self.engine._log(f"ERROR reading stats: {e}")

        threading.Thread(target=_do, daemon=True).start()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        if self.engine.is_running:
            self.engine.stop()
            # Give the thread a moment to clean up
            time.sleep(0.5)
        self.root.destroy()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = CollectorApp()
    app.run()


if __name__ == "__main__":
    main()

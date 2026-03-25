#!/usr/bin/env python3
"""
Airzone Dashboard — Multi-section web UI
==========================================
Tabbed dashboard: Overview | HVAC | Weather | Netatmo | Energy
Reads from local SQLite and fetches external APIs (Open-Meteo, Netatmo, Linky).

Usage:
    python3 airzone_dashboard_server.py              # port 8080
    python3 airzone_dashboard_server.py --port 9090
"""
from __future__ import annotations

import json
import sqlite3
import os
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template_string, request as flask_request

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "airzone_raw.db"
CONFIG_PATH = BASE_DIR / "airzone_config.json"

app = Flask(__name__)

# Load config for coordinates
_config = {}
if CONFIG_PATH.exists():
    with open(CONFIG_PATH) as f:
        _config = json.load(f)
LAT = _config.get("latitude", 44.07)
LON = _config.get("longitude", -1.26)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_tables():
    """Create additional tables if they don't exist."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weather_history (
            timestamp TEXT PRIMARY KEY,
            temperature REAL, humidity REAL, dew_point REAL,
            wind_speed REAL, wind_direction REAL, wind_gusts REAL,
            rain REAL, radiation REAL, cloud_cover REAL, pressure REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS netatmo_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            module_mac TEXT,
            module_name TEXT,
            temperature REAL,
            humidity REAL,
            co2 REAL,
            noise REAL,
            pressure REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_netatmo_ts ON netatmo_readings(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_netatmo_mod_ts ON netatmo_readings(module_mac, timestamp)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS linky_readings (
            timestamp TEXT PRIMARY KEY,
            wh REAL,
            source TEXT DEFAULT 'load_curve'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_linky_ts ON linky_readings(timestamp)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_analysis (
            date TEXT PRIMARY KEY,
            total_kwh REAL,
            base_kwh REAL,
            heatpump_kwh REAL,
            hot_water_kwh REAL,
            heating_hours REAL,
            avg_outdoor_temp REAL,
            kwh_per_heating_hr REAL,
            outdoor_temp_band TEXT
        )
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Weather backfill (Open-Meteo Archive API)
# ---------------------------------------------------------------------------

_weather_backfill_lock = threading.Lock()
_weather_backfill_status = {"running": False, "progress": "", "last_run": None}


def backfill_weather(days_back=730):
    """Fetch historical weather from Open-Meteo Archive API."""
    if not _weather_backfill_lock.acquire(blocking=False):
        return
    try:
        _weather_backfill_status["running"] = True
        conn = get_db()

        # Find latest weather record
        row = conn.execute("SELECT MAX(timestamp) FROM weather_history").fetchone()
        latest = row[0] if row and row[0] else None

        if latest:
            start_date = (datetime.fromisoformat(latest.replace("Z", "+00:00")) + timedelta(hours=1)).strftime("%Y-%m-%d")
        else:
            start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")

        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if start_date >= end_date:
            _weather_backfill_status["progress"] = "Already up to date"
            return

        _weather_backfill_status["progress"] = f"Fetching {start_date} to {end_date}..."

        # Open-Meteo Archive API (for dates > 5 days ago)
        # and Forecast API (for recent dates)
        params_common = {
            "latitude": LAT,
            "longitude": LON,
            "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,rain,shortwave_radiation,cloud_cover,pressure_msl",
            "timezone": "UTC",
        }

        all_rows = []

        # Fetch in 90-day chunks to avoid API limits
        chunk_start = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        # Archive API covers up to 5 days ago; forecast API covers recent + future
        archive_cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        while chunk_start < end_dt:
            chunk_end = min(chunk_start + timedelta(days=90), end_dt)

            cs = chunk_start.strftime("%Y-%m-%d")
            ce = chunk_end.strftime("%Y-%m-%d")

            # Use archive API for historical data, forecast for recent
            if cs < archive_cutoff:
                url = "https://archive-api.open-meteo.com/v1/archive"
                params = {**params_common, "start_date": cs, "end_date": min(ce, archive_cutoff)}
            else:
                url = "https://api.open-meteo.com/v1/forecast"
                params = {**params_common, "past_days": 7, "forecast_days": 2}

            _weather_backfill_status["progress"] = f"Fetching {cs} to {ce}..."

            try:
                resp = requests.get(url, params=params, timeout=60)
                resp.raise_for_status()
                data = resp.json()

                hourly = data.get("hourly", {})
                times = hourly.get("time", [])
                fields = ["temperature_2m", "relative_humidity_2m", "dew_point_2m",
                           "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
                           "rain", "shortwave_radiation", "cloud_cover", "pressure_msl"]

                for i, t in enumerate(times):
                    ts = t if ":" in t else t + "T00:00"
                    vals = []
                    for f in fields:
                        arr = hourly.get(f, [])
                        vals.append(arr[i] if i < len(arr) else None)
                    all_rows.append((ts, *vals))

            except Exception as e:
                _weather_backfill_status["progress"] = f"Error at {cs}: {e}"
                traceback.print_exc()

            # For forecast API, only one call needed (it returns past_days + forecast)
            if url.endswith("/forecast"):
                break

            chunk_start = chunk_end + timedelta(days=1)
            time.sleep(0.3)  # Rate limiting

        # Bulk insert in batches to avoid long DB locks
        if all_rows:
            batch_size = 500
            inserted = 0
            for i in range(0, len(all_rows), batch_size):
                batch = all_rows[i:i + batch_size]
                try:
                    batch_conn = get_db()
                    batch_conn.executemany("""
                        INSERT OR REPLACE INTO weather_history
                        (timestamp, temperature, humidity, dew_point, wind_speed, wind_direction,
                         wind_gusts, rain, radiation, cloud_cover, pressure)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, batch)
                    batch_conn.commit()
                    batch_conn.close()
                    inserted += len(batch)
                    _weather_backfill_status["progress"] = f"Inserted {inserted}/{len(all_rows)} records..."
                except Exception as e:
                    _weather_backfill_status["progress"] = f"DB error at batch {i}: {e}"
                    traceback.print_exc()
                    time.sleep(1)

            _weather_backfill_status["progress"] = f"Done: {inserted} records inserted"
        else:
            _weather_backfill_status["progress"] = "No new data to insert"

        _weather_backfill_status["last_run"] = datetime.now(timezone.utc).isoformat()
        conn.close()

    except Exception as e:
        _weather_backfill_status["progress"] = f"Error: {e}"
        traceback.print_exc()
    finally:
        _weather_backfill_status["running"] = False
        _weather_backfill_lock.release()


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Airzone Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f172a; color: #e2e8f0; }
  .header { background: #1e293b; padding: 16px 24px; border-bottom: 1px solid #334155;
            display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
  .header h1 { font-size: 20px; color: #38bdf8; }
  .header .status { font-size: 13px; color: #94a3b8; }
  .header .status .dot { display: inline-block; width: 8px; height: 8px;
           border-radius: 50%; background: #22c55e; margin-right: 6px; }

  /* Pill navigation */
  .nav-pills { display: flex; gap: 4px; background: #1e293b; padding: 8px 24px;
               border-bottom: 1px solid #334155; overflow-x: auto; }
  .nav-pills button { background: transparent; color: #94a3b8; border: none;
    padding: 8px 20px; border-radius: 20px; cursor: pointer; font-size: 14px;
    font-weight: 500; white-space: nowrap; transition: all 0.2s; }
  .nav-pills button:hover { background: #334155; color: #e2e8f0; }
  .nav-pills button.active { background: #38bdf8; color: #0f172a; }

  .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
  .section { display: none; }
  .section.active { display: block; }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
          gap: 16px; margin-bottom: 24px; }
  .card { background: #1e293b; border-radius: 12px; padding: 18px;
          border: 1px solid #334155; }
  .card h3 { font-size: 14px; color: #94a3b8; margin-bottom: 10px;
             text-transform: uppercase; letter-spacing: 0.5px; }
  .card .temp { font-size: 36px; font-weight: 700; color: #f1f5f9; }
  .card .temp .unit { font-size: 18px; color: #64748b; }
  .card .meta { display: flex; gap: 16px; margin-top: 8px; font-size: 13px; color: #94a3b8; flex-wrap: wrap; }
  .card .meta .label { color: #64748b; }
  .card.dhw { border-color: #f59e0b; }
  .card.dhw .temp { color: #fbbf24; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 11px; font-weight: 600; text-transform: uppercase; }
  .badge.on { background: #166534; color: #4ade80; }
  .badge.off { background: #1e293b; color: #64748b; border: 1px solid #334155; }
  .chart-container { background: #1e293b; border-radius: 12px; padding: 18px;
                     border: 1px solid #334155; margin-bottom: 24px; }
  .chart-container h2 { font-size: 16px; color: #94a3b8; margin-bottom: 12px; }
  .chart-container canvas { max-height: 300px; }
  .stats { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }
  .stat { background: #1e293b; border-radius: 8px; padding: 14px 18px;
          border: 1px solid #334155; min-width: 120px; }
  .stat .val { font-size: 24px; font-weight: 700; color: #38bdf8; }
  .stat .lbl { font-size: 12px; color: #64748b; margin-top: 2px; }
  .period-btns { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
  .period-btns button { background: #334155; color: #94a3b8; border: none;
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; transition: all 0.2s; }
  .period-btns button.active { background: #38bdf8; color: #0f172a; }

  /* Overview cards */
  .overview-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
                   gap: 16px; margin-bottom: 24px; }
  .overview-card { background: #1e293b; border-radius: 12px; padding: 20px;
                   border: 1px solid #334155; text-align: center; }
  .overview-card .icon { font-size: 28px; margin-bottom: 8px; }
  .overview-card .big { font-size: 32px; font-weight: 700; color: #f1f5f9; }
  .overview-card .sub { font-size: 13px; color: #94a3b8; margin-top: 4px; }
  .overview-card .detail { font-size: 12px; color: #64748b; margin-top: 2px; }

  /* Wind direction indicator */
  .wind-arrow { display: inline-block; font-size: 20px; transition: transform 0.3s; }

  .loading { text-align: center; padding: 40px; color: #64748b; }

  /* Two-column chart layout for weather */
  .chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 900px) { .chart-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="header">
  <h1>Airzone Dashboard</h1>
  <div class="status"><span class="dot"></span><span id="lastPoll">Loading...</span></div>
</div>
<div class="nav-pills">
  <button class="active" onclick="showSection('overview')">Overview</button>
  <button onclick="showSection('hvac')">HVAC</button>
  <button onclick="showSection('weather')">Weather</button>
  <button onclick="showSection('netatmo')">Netatmo</button>
  <button onclick="showSection('energy')">Energy</button>
</div>

<div class="container">

<!-- ===== OVERVIEW SECTION ===== -->
<div class="section active" id="sec-overview">
  <div class="overview-grid" id="overviewCards">
    <div class="loading">Loading overview...</div>
  </div>
  <div class="grid" id="overviewZones"></div>
</div>

<!-- ===== HVAC SECTION ===== -->
<div class="section" id="sec-hvac">
  <div class="stats" id="statsBar"></div>
  <div class="grid" id="zoneGrid"></div>
  <div id="hvacZoneDetail" style="display:none;">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;">
      <button onclick="clearZoneSelection()" style="background:#334155;color:#94a3b8;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;">← All Zones</button>
      <h2 id="hvacZoneTitle" style="font-size:18px;color:#38bdf8;"></h2>
    </div>
    <div class="period-btns" id="hvacPeriodBtns">
      <button onclick="setHvacPeriod(6)">6h</button>
      <button onclick="setHvacPeriod(24)" class="active">24h</button>
      <button onclick="setHvacPeriod(72)">3d</button>
      <button onclick="setHvacPeriod(168)">7d</button>
    </div>
    <div class="chart-container"><h2>Temperature (Indoor vs Outdoor)</h2><canvas id="tempChart"></canvas></div>
    <div class="chart-container"><h2>Relative Humidity (Indoor vs Outdoor)</h2><canvas id="humChart"></canvas></div>
    <div class="chart-container"><h2>Dew Point (Indoor vs Outdoor)</h2><canvas id="dpChart"></canvas></div>
    <div class="chart-container"><h2>DHW Tank Temperature</h2><canvas id="dhwChart"></canvas></div>
  </div>
</div>

<!-- ===== WEATHER SECTION ===== -->
<div class="section" id="sec-weather">
  <div class="period-btns" id="weatherPeriodBtns">
    <button onclick="setWeatherPeriod(24)">24h</button>
    <button onclick="setWeatherPeriod(168)" class="active">7d</button>
    <button onclick="setWeatherPeriod(720)">30d</button>
    <button onclick="setWeatherPeriod(8760)">1y</button>
    <button onclick="setWeatherPeriod(17520)">2y</button>
  </div>
  <div id="weatherStatus" style="font-size:12px;color:#64748b;margin-bottom:12px;"></div>
  <div class="chart-grid">
    <div class="chart-container"><h2>Temperature & Dew Point</h2><canvas id="wxTempChart"></canvas></div>
    <div class="chart-container"><h2>Wind Speed & Gusts</h2><canvas id="wxWindChart"></canvas></div>
    <div class="chart-container"><h2>Wind Direction</h2><canvas id="wxWindDirChart"></canvas></div>
    <div class="chart-container"><h2>Solar Radiation</h2><canvas id="wxRadChart"></canvas></div>
    <div class="chart-container"><h2>Rain</h2><canvas id="wxRainChart"></canvas></div>
    <div class="chart-container"><h2>Humidity</h2><canvas id="wxHumChart"></canvas></div>
    <div class="chart-container"><h2>Cloud Cover</h2><canvas id="wxCloudChart"></canvas></div>
    <div class="chart-container"><h2>Pressure</h2><canvas id="wxPressChart"></canvas></div>
  </div>
</div>

<!-- ===== NETATMO SECTION ===== -->
<div class="section" id="sec-netatmo">
  <div class="period-btns" id="netatmoPeriodBtns">
    <button onclick="setNetatmoPeriod(24)">24h</button>
    <button onclick="setNetatmoPeriod(168)" class="active">7d</button>
    <button onclick="setNetatmoPeriod(720)">30d</button>
    <button onclick="setNetatmoPeriod(8760)">1y</button>
    <button onclick="setNetatmoPeriod(17520)">2y</button>
  </div>
  <div id="netatmoCharts"><div class="loading">Loading Netatmo data...</div></div>
</div>

<!-- ===== ENERGY SECTION ===== -->
<div class="section" id="sec-energy">
  <div class="period-btns" id="energyPeriodBtns">
    <button onclick="setEnergyPeriod(7)">7d</button>
    <button onclick="setEnergyPeriod(30)" class="active">30d</button>
    <button onclick="setEnergyPeriod(90)">3m</button>
    <button onclick="setEnergyPeriod(365)">1y</button>
    <button onclick="setEnergyPeriod(730)">2y</button>
  </div>
  <div class="stats" id="energyStats"></div>
  <div class="chart-container"><h2>Daily Consumption</h2><canvas id="energyDailyChart"></canvas></div>
  <div class="chart-grid">
    <div class="chart-container"><h2>Monthly Totals</h2><canvas id="energyMonthlyChart"></canvas></div>
    <div class="chart-container"><h2>Hourly Load Curve (Last 3 Days)</h2><canvas id="energyHourlyChart"></canvas></div>
  </div>
</div>

</div><!-- /container -->

<script>
const TZ = 'Europe/Paris';
const BASE = window.location.pathname.replace(/\/$/, '');
const colors = ['#38bdf8','#a78bfa','#fb923c','#4ade80','#f472b6','#facc15','#34d399','#f87171','#818cf8'];
const TARIFF = 0.1927; // EUR/kWh

function toLocal(ts) {
  if (!ts) return null;
  const s = ts.endsWith('Z') ? ts : (ts.includes('+') ? ts : ts + 'Z');
  return new Date(s);
}
function fmtDate(ts) {
  if (!ts) return 'N/A';
  return toLocal(ts).toLocaleString('fr-FR', {timeZone: TZ});
}
function windDir(deg) {
  const dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
  return dirs[Math.round(deg / 22.5) % 16];
}

// ---------------------------------------------------------------------------
// Section navigation
// ---------------------------------------------------------------------------
let currentSection = 'overview';
const sectionLoaded = {overview: false, hvac: false, weather: false, netatmo: false, energy: false};

function showSection(name) {
  currentSection = name;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.getElementById('sec-' + name).classList.add('active');
  document.querySelectorAll('.nav-pills button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  if (!sectionLoaded[name]) loadSection(name);
}

function loadSection(name) {
  sectionLoaded[name] = true;
  switch(name) {
    case 'overview': loadOverview(); break;
    case 'hvac': loadHvac(); break;
    case 'weather': loadWeather(); break;
    case 'netatmo': loadNetatmo(); break;
    case 'energy': loadEnergy(); break;
  }
}

// ---------------------------------------------------------------------------
// Chart defaults
// ---------------------------------------------------------------------------
function chartOpts(yLabel) {
  return {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
    scales: {
      x: { type: 'time', ticks: { color: '#64748b', maxTicksLimit: 12 }, grid: { color: '#1e293b' },
           time: { tooltipFormat: 'dd/MM HH:mm' } },
      y: { ticks: { color: '#64748b' }, grid: { color: '#334155' },
           title: yLabel ? { display: true, text: yLabel, color: '#64748b' } : undefined }
    }
  };
}
function makeDs(label, data, color, opts) {
  return { label, data, borderColor: color, borderWidth: 1.5, pointRadius: 0, tension: 0.3, fill: false, ...opts };
}

// ---------------------------------------------------------------------------
// OVERVIEW
// ---------------------------------------------------------------------------
async function loadOverview() {
  try {
    const r = await fetch(BASE + '/api/overview');
    const d = await r.json();

    // Status
    document.getElementById('lastPoll').textContent = 'Last poll: ' + fmtDate(d.last_poll);

    let cards = '';
    // Outdoor temp
    if (d.outdoor_temp !== null) {
      cards += `<div class="overview-card">
        <div class="icon">🌡</div>
        <div class="big">${d.outdoor_temp != null ? d.outdoor_temp.toFixed(1) : '?'}°C</div>
        <div class="sub">Outdoor Temperature</div>
        ${d.outdoor_humidity != null ? '<div class="detail">Humidity: ' + d.outdoor_humidity + '%</div>' : ''}
      </div>`;
    }
    // Wind
    if (d.wind_speed !== null) {
      cards += `<div class="overview-card">
        <div class="icon"><span class="wind-arrow" style="transform:rotate(${d.wind_direction || 0}deg)">↓</span></div>
        <div class="big">${d.wind_speed != null ? d.wind_speed.toFixed(0) : '?'} km/h</div>
        <div class="sub">Wind ${d.wind_direction != null ? windDir(d.wind_direction) : ''}</div>
        ${d.wind_gusts ? '<div class="detail">Gusts: ' + d.wind_gusts.toFixed(0) + ' km/h</div>' : ''}
      </div>`;
    }
    // Weather temp (fallback if no Netatmo)
    if (d.outdoor_temp === null && d.weather_temp !== null) {
      cards += `<div class="overview-card">
        <div class="icon">🌡</div>
        <div class="big">${d.weather_temp.toFixed(1)}°C</div>
        <div class="sub">Outdoor (Open-Meteo)</div>
      </div>`;
    }
    // Solar radiation
    if (d.radiation !== null && d.radiation !== undefined) {
      cards += `<div class="overview-card">
        <div class="icon">☀</div>
        <div class="big">${d.radiation.toFixed(0)}</div>
        <div class="sub">W/m² Solar</div>
      </div>`;
    }
    // Energy today
    if (d.energy_today_kwh !== null && d.energy_today_kwh !== undefined) {
      cards += `<div class="overview-card">
        <div class="icon">⚡</div>
        <div class="big">${d.energy_today_kwh.toFixed(1)}</div>
        <div class="sub">kWh Today</div>
        <div class="detail">€${(d.energy_today_kwh * TARIFF).toFixed(2)}</div>
      </div>`;
    }
    // System stats
    cards += `<div class="overview-card">
      <div class="icon">📊</div>
      <div class="big">${d.zone_count || 0}</div>
      <div class="sub">Active Zones</div>
      <div class="detail">${d.total_readings ? d.total_readings.toLocaleString() + ' readings' : ''}</div>
    </div>`;
    cards += `<div class="overview-card">
      <div class="icon">💾</div>
      <div class="big">${d.db_size_mb || 0} MB</div>
      <div class="sub">Database</div>
      <div class="detail">${d.uptime_days || 0} days collecting</div>
    </div>`;

    document.getElementById('overviewCards').innerHTML = cards;

    // Zone summary cards
    let zh = '';
    (d.zones || []).forEach(z => {
      zh += `<div class="card">
        <h3>${z.zone_name}</h3>
        <div class="temp">${z.temperature != null ? z.temperature.toFixed(1) : '?'}<span class="unit">°C</span></div>
        <div class="meta">
          <span><span class="label">Humidity </span>${z.humidity || '?'}%</span>
          <span><span class="label">SP </span>${z.setpoint_heat || '?'}°C</span>
          <span class="badge ${z.power ? 'on' : 'off'}">${z.power ? 'ON' : 'OFF'}</span>
        </div>
      </div>`;
    });
    if (d.dhw) {
      zh += `<div class="card dhw">
        <h3>Hot Water</h3>
        <div class="temp">${d.dhw.tank_temp || '?'}<span class="unit">°C</span></div>
        <div class="meta">
          <span><span class="label">SP </span>${d.dhw.setpoint || '?'}°C</span>
          <span class="badge ${d.dhw.power ? 'on' : 'off'}">${d.dhw.power ? 'ON' : 'OFF'}</span>
        </div>
      </div>`;
    }
    document.getElementById('overviewZones').innerHTML = zh;
  } catch(e) { console.error('Overview error:', e); }
}

// ---------------------------------------------------------------------------
// HVAC
// ---------------------------------------------------------------------------
let hvacPeriod = 24;
let selectedZone = null;
let tempChart, humChart, dpChart, dhwChart;

// Dew point calculation (Magnus formula)
function calcDewPoint(t, rh) {
  if (t == null || rh == null || rh <= 0) return null;
  const a = 17.27, b = 237.7;
  const alpha = (a * t) / (b + t) + Math.log(rh / 100);
  return (b * alpha) / (a - alpha);
}

function selectZone(zoneName) {
  selectedZone = zoneName;
  document.getElementById('hvacZoneDetail').style.display = 'block';
  document.getElementById('hvacZoneTitle').textContent = zoneName;
  // Highlight selected card
  document.querySelectorAll('#zoneGrid .card').forEach(c => {
    c.style.borderColor = c.dataset.zone === zoneName ? '#38bdf8' : '#334155';
  });
  loadHvacHistory();
}

function clearZoneSelection() {
  selectedZone = null;
  document.getElementById('hvacZoneDetail').style.display = 'none';
  document.querySelectorAll('#zoneGrid .card').forEach(c => c.style.borderColor = '#334155');
}

function setHvacPeriod(h) {
  hvacPeriod = h;
  document.querySelectorAll('#hvacPeriodBtns button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  loadHvacHistory();
}

async function loadHvac() {
  const r = await fetch(BASE + '/api/current');
  const d = await r.json();
  document.getElementById('lastPoll').textContent = 'Last poll: ' + fmtDate(d.last_poll);

  document.getElementById('statsBar').innerHTML = `
    <div class="stat"><div class="val">${d.total_readings.toLocaleString()}</div><div class="lbl">Total readings</div></div>
    <div class="stat"><div class="val">${d.zones.length}</div><div class="lbl">Active zones</div></div>
    <div class="stat"><div class="val">${d.db_size_mb} MB</div><div class="lbl">Database</div></div>
    <div class="stat"><div class="val">${d.uptime_days}d</div><div class="lbl">Collecting since</div></div>
  `;

  let html = '';
  if (d.dhw) {
    html += `<div class="card dhw" style="cursor:default;"><h3>Hot Water (DHW)</h3>
      <div class="temp">${d.dhw.tank_temp || '?'}<span class="unit">°C</span></div>
      <div class="meta">
        <span><span class="label">Setpoint </span>${d.dhw.setpoint || '?'}°C</span>
        <span class="badge ${d.dhw.power ? 'on' : 'off'}">${d.dhw.power ? 'ON' : 'OFF'}</span>
      </div></div>`;
  }
  d.zones.forEach(z => {
    const name = z.zone_name || 'unknown';
    const selected = selectedZone === name;
    html += `<div class="card" data-zone="${name}" onclick="selectZone('${name.replace(/'/g, "\\'")}')"
      style="cursor:pointer;border-color:${selected ? '#38bdf8' : '#334155'};transition:border-color 0.2s;">
      <h3>${name}</h3>
      <div class="temp">${z.temperature != null ? z.temperature.toFixed(1) : '?'}<span class="unit">°C</span></div>
      <div class="meta">
        <span><span class="label">Humidity </span>${z.humidity || '?'}%</span>
        <span><span class="label">SP </span>${z.setpoint_heat || '?'}°C</span>
        <span class="badge ${z.power ? 'on' : 'off'}">${z.power ? 'ON' : 'OFF'}</span>
      </div></div>`;
  });
  document.getElementById('zoneGrid').innerHTML = html;
}

async function loadHvacHistory() {
  if (!selectedZone) return;
  const zoneParam = encodeURIComponent(selectedZone);
  const r = await fetch(BASE + '/api/history?hours=' + hvacPeriod + '&zone=' + zoneParam);
  const d = await r.json();

  const indoor = d.zones[selectedZone] || [];
  const outdoor = d.outdoor || [];

  // Indoor data with calculated dew points
  const indoorTemp = indoor.map(p => ({x: toLocal(p.t), y: p.temp}));
  const indoorHum = indoor.map(p => ({x: toLocal(p.t), y: p.hum}));
  const indoorDp = indoor.map(p => ({x: toLocal(p.t), y: calcDewPoint(p.temp, p.hum)}));

  // Outdoor data (from weather_history)
  const outdoorTemp = outdoor.map(p => ({x: toLocal(p.t), y: p.temp}));
  const outdoorHum = outdoor.map(p => ({x: toLocal(p.t), y: p.hum}));
  const outdoorDp = outdoor.map(p => ({x: toLocal(p.t), y: p.dp}));

  // Temperature: indoor vs outdoor
  if (tempChart) tempChart.destroy();
  tempChart = new Chart(document.getElementById('tempChart'), {
    type: 'line',
    data: { datasets: [
      makeDs(selectedZone + ' (indoor)', indoorTemp, '#38bdf8', {borderWidth: 2}),
      makeDs('Outdoor', outdoorTemp, '#fb923c', {borderDash: [5,3]}),
    ]},
    options: chartOpts('°C')
  });

  // Humidity: indoor vs outdoor
  if (humChart) humChart.destroy();
  humChart = new Chart(document.getElementById('humChart'), {
    type: 'line',
    data: { datasets: [
      makeDs(selectedZone + ' (indoor)', indoorHum, '#a78bfa', {borderWidth: 2}),
      makeDs('Outdoor', outdoorHum, '#fb923c', {borderDash: [5,3]}),
    ]},
    options: chartOpts('%')
  });

  // Dew Point: indoor vs outdoor
  if (dpChart) dpChart.destroy();
  dpChart = new Chart(document.getElementById('dpChart'), {
    type: 'line',
    data: { datasets: [
      makeDs(selectedZone + ' (indoor)', indoorDp, '#4ade80', {borderWidth: 2}),
      makeDs('Outdoor', outdoorDp, '#fb923c', {borderDash: [5,3]}),
    ]},
    options: chartOpts('°C')
  });

  // DHW
  if (d.dhw && d.dhw.length > 0) {
    const dhwDs = [
      makeDs('Tank temp', d.dhw.map(p => ({x: toLocal(p.t), y: p.tank})), '#fbbf24', {borderWidth: 2}),
      makeDs('Setpoint', d.dhw.map(p => ({x: toLocal(p.t), y: p.sp})), '#f59e0b', {borderDash: [4,4], borderWidth: 1}),
    ];
    if (dhwChart) dhwChart.destroy();
    dhwChart = new Chart(document.getElementById('dhwChart'), {type:'line', data:{datasets:dhwDs}, options:chartOpts('°C')});
  }
}

// ---------------------------------------------------------------------------
// WEATHER
// ---------------------------------------------------------------------------
let weatherPeriod = 168;
let wxCharts = {};

function setWeatherPeriod(h) {
  weatherPeriod = h;
  document.querySelectorAll('#weatherPeriodBtns button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  loadWeather();
}

async function loadWeather() {
  const status = document.getElementById('weatherStatus');
  status.textContent = 'Loading weather data...';
  try {
    const r = await fetch(BASE + '/api/weather?hours=' + weatherPeriod);
    const d = await r.json();

    if (d.backfill_status) status.textContent = 'Backfill: ' + d.backfill_status;
    else status.textContent = d.count + ' data points';

    const pts = d.data || [];
    if (pts.length === 0) {
      status.textContent = 'No weather data yet. Backfill may be running...';
      return;
    }

    // Downsample for very large datasets
    const maxPts = 2000;
    const step = pts.length > maxPts ? Math.ceil(pts.length / maxPts) : 1;
    const sampled = step > 1 ? pts.filter((_, i) => i % step === 0) : pts;

    // Temp + Dew Point
    Object.values(wxCharts).forEach(c => c.destroy());
    wxCharts = {};

    wxCharts.temp = new Chart(document.getElementById('wxTempChart'), {
      type: 'line',
      data: { datasets: [
        makeDs('Temperature', sampled.map(p => ({x: new Date(p.t), y: p.temp})), '#fb923c'),
        makeDs('Dew Point', sampled.map(p => ({x: new Date(p.t), y: p.dp})), '#38bdf8', {borderDash: [4,4]}),
      ]},
      options: chartOpts('°C')
    });

    // Wind
    wxCharts.wind = new Chart(document.getElementById('wxWindChart'), {
      type: 'line',
      data: { datasets: [
        makeDs('Wind Speed', sampled.map(p => ({x: new Date(p.t), y: p.ws})), '#4ade80'),
        makeDs('Gusts', sampled.map(p => ({x: new Date(p.t), y: p.wg})), '#f87171', {borderDash: [3,3]}),
      ]},
      options: chartOpts('km/h')
    });

    // Solar Radiation
    wxCharts.rad = new Chart(document.getElementById('wxRadChart'), {
      type: 'line',
      data: { datasets: [
        makeDs('Solar Radiation', sampled.map(p => ({x: new Date(p.t), y: p.rad})), '#facc15', {fill: true, backgroundColor: 'rgba(250,204,21,0.1)'}),
      ]},
      options: chartOpts('W/m²')
    });

    // Rain
    wxCharts.rain = new Chart(document.getElementById('wxRainChart'), {
      type: 'bar',
      data: { datasets: [{
        label: 'Rain', data: sampled.map(p => ({x: new Date(p.t), y: p.rain})),
        backgroundColor: '#38bdf8', borderRadius: 2,
      }]},
      options: chartOpts('mm')
    });

    // Wind Direction (scatter with color by speed)
    wxCharts.windDir = new Chart(document.getElementById('wxWindDirChart'), {
      type: 'line',
      data: { datasets: [
        makeDs('Direction', sampled.map(p => ({x: new Date(p.t), y: p.wd})), '#facc15', {pointRadius: 1, borderWidth: 0.5}),
      ]},
      options: {
        ...chartOpts('°'),
        scales: {
          ...chartOpts('°').scales,
          y: { min: 0, max: 360, ticks: { color: '#64748b', stepSize: 90,
            callback: v => ({0:'N',90:'E',180:'S',270:'W',360:'N'}[v] || v + '°') },
            grid: { color: '#334155' }, title: { display: true, text: 'Direction', color: '#64748b' } }
        }
      }
    });

    // Humidity
    wxCharts.hum = new Chart(document.getElementById('wxHumChart'), {
      type: 'line',
      data: { datasets: [
        makeDs('Humidity', sampled.map(p => ({x: new Date(p.t), y: p.hum})), '#a78bfa'),
      ]},
      options: chartOpts('%')
    });

    // Cloud Cover
    wxCharts.cloud = new Chart(document.getElementById('wxCloudChart'), {
      type: 'line',
      data: { datasets: [
        makeDs('Cloud Cover', sampled.map(p => ({x: new Date(p.t), y: p.cloud})), '#94a3b8', {fill: true, backgroundColor: 'rgba(148,163,184,0.1)'}),
      ]},
      options: chartOpts('%')
    });

    // Pressure
    wxCharts.press = new Chart(document.getElementById('wxPressChart'), {
      type: 'line',
      data: { datasets: [
        makeDs('Pressure', sampled.map(p => ({x: new Date(p.t), y: p.press})), '#f472b6'),
      ]},
      options: chartOpts('hPa')
    });

  } catch(e) { status.textContent = 'Error loading weather: ' + e.message; }
}

// ---------------------------------------------------------------------------
// NETATMO
// ---------------------------------------------------------------------------
let netatmoPeriod = 168;
let netatmoCharts = {};

function setNetatmoPeriod(h) {
  netatmoPeriod = h;
  document.querySelectorAll('#netatmoPeriodBtns button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  loadNetatmo();
}

async function loadNetatmo() {
  const container = document.getElementById('netatmoCharts');
  container.innerHTML = '<div class="loading">Loading Netatmo data...</div>';
  try {
    const r = await fetch(BASE + '/api/netatmo?hours=' + netatmoPeriod);
    const d = await r.json();

    if (!d.modules || Object.keys(d.modules).length === 0) {
      container.innerHTML = '<div class="loading">No Netatmo data available. Run backfill on server.</div>';
      return;
    }

    Object.values(netatmoCharts).forEach(c => c.destroy());
    netatmoCharts = {};

    let html = '';
    const moduleNames = Object.keys(d.modules);

    // Downsample
    const maxPts = 2000;

    moduleNames.forEach((mod, mi) => {
      const pts = d.modules[mod];
      const step = pts.length > maxPts ? Math.ceil(pts.length / maxPts) : 1;
      const sampled = step > 1 ? pts.filter((_, i) => i % step === 0) : pts;

      const hasTemp = sampled.some(p => p.temp !== null);
      const hasHum = sampled.some(p => p.hum !== null);
      const hasCo2 = sampled.some(p => p.co2 !== null);
      const hasNoise = sampled.some(p => p.noise !== null);
      const hasPress = sampled.some(p => p.press !== null);

      html += `<h2 style="color:#94a3b8;margin:20px 0 12px;font-size:18px;">${mod}</h2>`;

      if (hasTemp) {
        html += `<div class="chart-container"><h2>Temperature</h2><canvas id="nat_temp_${mi}"></canvas></div>`;
      }
      if (hasHum) {
        html += `<div class="chart-container"><h2>Humidity</h2><canvas id="nat_hum_${mi}"></canvas></div>`;
      }
      if (hasCo2) {
        html += `<div class="chart-container"><h2>CO₂</h2><canvas id="nat_co2_${mi}"></canvas></div>`;
      }
      if (hasNoise) {
        html += `<div class="chart-container"><h2>Noise</h2><canvas id="nat_noise_${mi}"></canvas></div>`;
      }
      if (hasPress) {
        html += `<div class="chart-container"><h2>Pressure</h2><canvas id="nat_press_${mi}"></canvas></div>`;
      }

      // Defer chart creation
      setTimeout(() => {
        const color = colors[mi % colors.length];
        if (hasTemp) {
          netatmoCharts['temp_'+mi] = new Chart(document.getElementById('nat_temp_'+mi), {
            type:'line', data:{datasets:[makeDs(mod, sampled.map(p => ({x: toLocal(p.t), y: p.temp})), color)]}, options:chartOpts('°C')});
        }
        if (hasHum) {
          netatmoCharts['hum_'+mi] = new Chart(document.getElementById('nat_hum_'+mi), {
            type:'line', data:{datasets:[makeDs(mod, sampled.map(p => ({x: toLocal(p.t), y: p.hum})), color)]}, options:chartOpts('%')});
        }
        if (hasCo2) {
          netatmoCharts['co2_'+mi] = new Chart(document.getElementById('nat_co2_'+mi), {
            type:'line', data:{datasets:[makeDs(mod, sampled.map(p => ({x: toLocal(p.t), y: p.co2})), color)]}, options:chartOpts('ppm')});
        }
        if (hasNoise) {
          netatmoCharts['noise_'+mi] = new Chart(document.getElementById('nat_noise_'+mi), {
            type:'line', data:{datasets:[makeDs(mod, sampled.map(p => ({x: toLocal(p.t), y: p.noise})), color)]}, options:chartOpts('dB')});
        }
        if (hasPress) {
          netatmoCharts['press_'+mi] = new Chart(document.getElementById('nat_press_'+mi), {
            type:'line', data:{datasets:[makeDs(mod, sampled.map(p => ({x: toLocal(p.t), y: p.press})), color)]}, options:chartOpts('hPa')});
        }
      }, 50);
    });

    container.innerHTML = html;
  } catch(e) {
    container.innerHTML = '<div class="loading">Error: ' + e.message + '</div>';
  }
}

// ---------------------------------------------------------------------------
// ENERGY
// ---------------------------------------------------------------------------
let energyPeriod = 30;
let energyChartObjs = {};

function setEnergyPeriod(d) {
  energyPeriod = d;
  document.querySelectorAll('#energyPeriodBtns button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  loadEnergy();
}

async function loadEnergy() {
  try {
    const r = await fetch(BASE + '/api/energy?days=' + energyPeriod);
    const d = await r.json();

    // Stats
    const totalKwh = d.daily.reduce((s, p) => s + (p.kwh || 0), 0);
    const totalCost = totalKwh * TARIFF;
    const avgDaily = d.daily.length > 0 ? totalKwh / d.daily.length : 0;

    document.getElementById('energyStats').innerHTML = `
      <div class="stat"><div class="val">${totalKwh.toFixed(1)}</div><div class="lbl">kWh Total</div></div>
      <div class="stat"><div class="val">€${totalCost.toFixed(2)}</div><div class="lbl">Cost (€${TARIFF}/kWh)</div></div>
      <div class="stat"><div class="val">${avgDaily.toFixed(1)}</div><div class="lbl">kWh/day avg</div></div>
      <div class="stat"><div class="val">${d.daily.length}</div><div class="lbl">Days of data</div></div>
    `;

    Object.values(energyChartObjs).forEach(c => c.destroy());
    energyChartObjs = {};

    // Daily consumption bar chart
    if (d.daily.length > 0) {
      energyChartObjs.daily = new Chart(document.getElementById('energyDailyChart'), {
        type: 'bar',
        data: { datasets: [{
          label: 'kWh', data: d.daily.map(p => ({x: p.date, y: p.kwh})),
          backgroundColor: '#38bdf8', borderRadius: 3,
        }]},
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { type: 'time', time: { unit: d.daily.length > 90 ? 'month' : 'day', tooltipFormat: 'dd/MM/yyyy' },
                 ticks: { color: '#64748b' }, grid: { color: '#1e293b' } },
            y: { ticks: { color: '#64748b' }, grid: { color: '#334155' },
                 title: { display: true, text: 'kWh', color: '#64748b' } }
          }
        }
      });
    }

    // Monthly totals
    if (d.monthly && d.monthly.length > 0) {
      energyChartObjs.monthly = new Chart(document.getElementById('energyMonthlyChart'), {
        type: 'bar',
        data: { labels: d.monthly.map(p => p.month), datasets: [{
          label: 'kWh', data: d.monthly.map(p => p.kwh),
          backgroundColor: '#a78bfa', borderRadius: 3,
        }]},
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#64748b' }, grid: { color: '#1e293b' } },
            y: { ticks: { color: '#64748b' }, grid: { color: '#334155' },
                 title: { display: true, text: 'kWh', color: '#64748b' } }
          }
        }
      });
    }

    // Hourly load curve (last 3 days)
    if (d.hourly && d.hourly.length > 0) {
      energyChartObjs.hourly = new Chart(document.getElementById('energyHourlyChart'), {
        type: 'line',
        data: { datasets: [
          makeDs('Consumption', d.hourly.map(p => ({x: toLocal(p.t), y: p.wh / 1000})), '#fb923c', {fill: true, backgroundColor: 'rgba(251,146,60,0.1)'}),
        ]},
        options: chartOpts('kWh')
      });
    }

  } catch(e) { console.error('Energy error:', e); }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
loadOverview();
setInterval(() => { if (currentSection === 'overview') loadOverview(); }, 60000);
setInterval(() => { if (currentSection === 'hvac') { loadHvac(); } }, 300000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/current")
def api_current():
    conn = get_db()
    zones = conn.execute("""
        SELECT z.* FROM zone_readings z
        INNER JOIN (
            SELECT zone_name, MAX(timestamp) as max_ts
            FROM zone_readings WHERE zone_name IS NOT NULL
            GROUP BY zone_name
        ) latest ON z.zone_name = latest.zone_name AND z.timestamp = latest.max_ts
        ORDER BY z.zone_name
    """).fetchall()

    dhw = conn.execute("SELECT * FROM dhw_readings ORDER BY timestamp DESC LIMIT 1").fetchone()
    total = conn.execute("SELECT COUNT(*) FROM zone_readings").fetchone()[0]
    first = conn.execute("SELECT MIN(timestamp) FROM zone_readings").fetchone()[0]
    last = conn.execute("SELECT MAX(timestamp) FROM zone_readings").fetchone()[0]
    conn.close()

    size_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 1) if DB_PATH.exists() else 0
    uptime_days = 0
    if first:
        try:
            t0 = datetime.fromisoformat(first.replace("Z", "+00:00"))
            uptime_days = (datetime.now(timezone.utc) - t0).days
        except Exception:
            pass

    return jsonify({
        "zones": [dict(z) for z in zones],
        "dhw": dict(dhw) if dhw else None,
        "total_readings": total,
        "db_size_mb": size_mb,
        "last_poll": last,
        "uptime_days": uptime_days,
    })


@app.route("/api/history")
def api_history():
    hours = int(flask_request.args.get("hours", 24))
    zone_filter = flask_request.args.get("zone", None)
    conn = get_db()

    if zone_filter:
        rows = conn.execute("""
            SELECT timestamp, zone_name, temperature, humidity
            FROM zone_readings
            WHERE timestamp > datetime('now', ? || ' hours')
              AND temperature IS NOT NULL AND zone_name = ?
            ORDER BY timestamp
        """, (f"-{hours}", zone_filter)).fetchall()
    else:
        rows = conn.execute("""
            SELECT timestamp, zone_name, temperature, humidity
            FROM zone_readings
            WHERE timestamp > datetime('now', ? || ' hours')
              AND temperature IS NOT NULL
            ORDER BY timestamp
        """, (f"-{hours}",)).fetchall()

    zones: dict[str, list] = {}
    for r in rows:
        name = r["zone_name"] or "unknown"
        if name not in zones:
            zones[name] = []
        zones[name].append({"t": r["timestamp"], "temp": r["temperature"], "hum": r["humidity"]})

    # Outdoor weather data (from weather_history or netatmo outdoor)
    outdoor = []
    try:
        wx_rows = conn.execute("""
            SELECT timestamp, temperature, humidity, dew_point
            FROM weather_history
            WHERE timestamp > datetime('now', ? || ' hours')
            ORDER BY timestamp
        """, (f"-{hours}",)).fetchall()
        outdoor = [{"t": r["timestamp"], "temp": r["temperature"], "hum": r["humidity"], "dp": r["dew_point"]} for r in wx_rows]
    except Exception:
        pass

    # Fallback to netatmo outdoor if no weather data
    if not outdoor:
        try:
            nat_rows = conn.execute("""
                SELECT timestamp, temperature, humidity
                FROM netatmo_readings
                WHERE timestamp > datetime('now', ? || ' hours')
                  AND (module_name LIKE '%outdoor%' OR module_name LIKE '%Outdoor%'
                       OR module_name LIKE '%ext%' OR module_name LIKE '%Ext%')
                ORDER BY timestamp
            """, (f"-{hours}",)).fetchall()
            outdoor = [{"t": r["timestamp"], "temp": r["temperature"], "hum": r["humidity"], "dp": None} for r in nat_rows]
        except Exception:
            pass

    dhw_rows = conn.execute("""
        SELECT timestamp, tank_temp, setpoint
        FROM dhw_readings
        WHERE timestamp > datetime('now', ? || ' hours')
        ORDER BY timestamp
    """, (f"-{hours}",)).fetchall()
    conn.close()

    return jsonify({
        "zones": zones,
        "outdoor": outdoor,
        "dhw": [{"t": r["timestamp"], "tank": r["tank_temp"], "sp": r["setpoint"]} for r in dhw_rows],
    })


@app.route("/api/overview")
def api_overview():
    conn = get_db()

    # Zones
    zones = conn.execute("""
        SELECT z.* FROM zone_readings z
        INNER JOIN (
            SELECT zone_name, MAX(timestamp) as max_ts
            FROM zone_readings WHERE zone_name IS NOT NULL GROUP BY zone_name
        ) latest ON z.zone_name = latest.zone_name AND z.timestamp = latest.max_ts
        ORDER BY z.zone_name
    """).fetchall()

    dhw = conn.execute("SELECT * FROM dhw_readings ORDER BY timestamp DESC LIMIT 1").fetchone()
    total = conn.execute("SELECT COUNT(*) FROM zone_readings").fetchone()[0]
    first = conn.execute("SELECT MIN(timestamp) FROM zone_readings").fetchone()[0]
    last = conn.execute("SELECT MAX(timestamp) FROM zone_readings").fetchone()[0]
    size_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 1) if DB_PATH.exists() else 0

    uptime_days = 0
    if first:
        try:
            t0 = datetime.fromisoformat(first.replace("Z", "+00:00"))
            uptime_days = (datetime.now(timezone.utc) - t0).days
        except Exception:
            pass

    # Netatmo outdoor
    outdoor_temp = None
    outdoor_hum = None
    try:
        nat = conn.execute("""
            SELECT temperature, humidity FROM netatmo_readings
            WHERE module_name LIKE '%outdoor%' OR module_name LIKE '%Outdoor%'
                  OR module_name LIKE '%ext%' OR module_name LIKE '%Ext%'
            ORDER BY timestamp DESC LIMIT 1
        """).fetchone()
        if nat:
            outdoor_temp = nat["temperature"]
            outdoor_hum = nat["humidity"]
    except Exception:
        pass

    # Latest weather
    weather_temp = None
    wind_speed = None
    wind_direction = None
    wind_gusts = None
    radiation = None
    try:
        wx = conn.execute("SELECT * FROM weather_history ORDER BY timestamp DESC LIMIT 1").fetchone()
        if wx:
            weather_temp = wx["temperature"]
            wind_speed = wx["wind_speed"]
            wind_direction = wx["wind_direction"]
            wind_gusts = wx["wind_gusts"]
            radiation = wx["radiation"]
    except Exception:
        pass

    # Energy today
    energy_today = None
    try:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute("""
            SELECT SUM(wh) / 1000.0 as kwh FROM linky_readings
            WHERE timestamp >= ? AND timestamp < ?
        """, (today_str, today_str + "T23:59:59")).fetchone()
        if row and row["kwh"]:
            energy_today = row["kwh"]
    except Exception:
        pass

    conn.close()

    return jsonify({
        "zones": [dict(z) for z in zones],
        "dhw": dict(dhw) if dhw else None,
        "zone_count": len(zones),
        "total_readings": total,
        "db_size_mb": size_mb,
        "last_poll": last,
        "uptime_days": uptime_days,
        "outdoor_temp": outdoor_temp,
        "outdoor_humidity": outdoor_hum,
        "weather_temp": weather_temp,
        "wind_speed": wind_speed,
        "wind_direction": wind_direction,
        "wind_gusts": wind_gusts,
        "radiation": radiation,
        "energy_today_kwh": energy_today,
    })


@app.route("/api/weather")
def api_weather():
    hours = int(flask_request.args.get("hours", 168))
    conn = get_db()

    try:
        rows = conn.execute("""
            SELECT timestamp, temperature, humidity, dew_point,
                   wind_speed, wind_direction, wind_gusts,
                   rain, radiation, cloud_cover, pressure
            FROM weather_history
            WHERE timestamp > datetime('now', ? || ' hours')
            ORDER BY timestamp
        """, (f"-{hours}",)).fetchall()
    except Exception:
        rows = []

    conn.close()

    data = [{
        "t": r["timestamp"], "temp": r["temperature"], "hum": r["humidity"],
        "dp": r["dew_point"], "ws": r["wind_speed"], "wd": r["wind_direction"],
        "wg": r["wind_gusts"], "rain": r["rain"], "rad": r["radiation"],
        "cloud": r["cloud_cover"], "press": r["pressure"],
    } for r in rows]

    return jsonify({
        "data": data,
        "count": len(data),
        "backfill_status": _weather_backfill_status.get("progress") if _weather_backfill_status.get("running") else None,
    })


@app.route("/api/weather/backfill", methods=["POST"])
def api_weather_backfill():
    days = int(flask_request.args.get("days", 730))
    if _weather_backfill_status["running"]:
        return jsonify({"status": "already_running", "progress": _weather_backfill_status["progress"]})
    threading.Thread(target=backfill_weather, args=(days,), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/netatmo")
def api_netatmo():
    hours = int(flask_request.args.get("hours", 168))
    conn = get_db()

    try:
        rows = conn.execute("""
            SELECT timestamp, module_name, temperature, humidity, co2, noise, pressure
            FROM netatmo_readings
            WHERE timestamp > datetime('now', ? || ' hours')
            ORDER BY module_name, timestamp
        """, (f"-{hours}",)).fetchall()
    except Exception:
        rows = []

    conn.close()

    modules: dict[str, list] = {}
    for r in rows:
        name = r["module_name"] or "Unknown"
        if name not in modules:
            modules[name] = []
        modules[name].append({
            "t": r["timestamp"], "temp": r["temperature"], "hum": r["humidity"],
            "co2": r["co2"], "noise": r["noise"], "press": r["pressure"],
        })

    return jsonify({"modules": modules})


@app.route("/api/energy")
def api_energy():
    days = int(flask_request.args.get("days", 30))
    conn = get_db()

    # Daily consumption (aggregate linky_readings by date)
    daily = []
    try:
        rows = conn.execute("""
            SELECT DATE(timestamp) as date, SUM(wh) / 1000.0 as kwh
            FROM linky_readings
            WHERE timestamp > datetime('now', ? || ' days')
            GROUP BY DATE(timestamp)
            ORDER BY date
        """, (f"-{days}",)).fetchall()
        daily = [{"date": r["date"], "kwh": r["kwh"]} for r in rows]
    except Exception:
        pass

    # Monthly totals
    monthly = []
    try:
        rows = conn.execute("""
            SELECT strftime('%Y-%m', timestamp) as month, SUM(wh) / 1000.0 as kwh
            FROM linky_readings
            WHERE timestamp > datetime('now', ? || ' days')
            GROUP BY strftime('%Y-%m', timestamp)
            ORDER BY month
        """, (f"-{days}",)).fetchall()
        monthly = [{"month": r["month"], "kwh": r["kwh"]} for r in rows]
    except Exception:
        pass

    # Hourly load curve (last 3 days)
    hourly = []
    try:
        rows = conn.execute("""
            SELECT timestamp, wh FROM linky_readings
            WHERE timestamp > datetime('now', '-3 days')
            ORDER BY timestamp
        """).fetchall()
        hourly = [{"t": r["timestamp"], "wh": r["wh"]} for r in rows]
    except Exception:
        pass

    conn.close()
    return jsonify({"daily": daily, "monthly": monthly, "hourly": hourly})


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    zone_count = conn.execute("SELECT COUNT(*) FROM zone_readings").fetchone()[0]
    dhw_count = conn.execute("SELECT COUNT(*) FROM dhw_readings").fetchone()[0]
    first = conn.execute("SELECT MIN(timestamp) FROM zone_readings").fetchone()[0]
    last = conn.execute("SELECT MAX(timestamp) FROM zone_readings").fetchone()[0]
    distinct = conn.execute("SELECT COUNT(DISTINCT zone_name) FROM zone_readings").fetchone()[0]

    # Weather count
    wx_count = 0
    try:
        wx_count = conn.execute("SELECT COUNT(*) FROM weather_history").fetchone()[0]
    except Exception:
        pass

    # Netatmo count
    nat_count = 0
    try:
        nat_count = conn.execute("SELECT COUNT(*) FROM netatmo_readings").fetchone()[0]
    except Exception:
        pass

    # Linky count
    linky_count = 0
    try:
        linky_count = conn.execute("SELECT COUNT(*) FROM linky_readings").fetchone()[0]
    except Exception:
        pass

    conn.close()
    size_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 2) if DB_PATH.exists() else 0

    return jsonify({
        "zone_readings": zone_count, "dhw_readings": dhw_count,
        "weather_readings": wx_count, "netatmo_readings": nat_count,
        "linky_readings": linky_count,
        "db_size_mb": size_mb, "distinct_zones": distinct,
        "first_reading": first, "last_reading": last,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--backfill-weather", action="store_true", help="Backfill weather on startup")
    parser.add_argument("--weather-days", type=int, default=730, help="Days to backfill")
    args = parser.parse_args()

    ensure_tables()

    if args.backfill_weather:
        threading.Thread(target=backfill_weather, args=(args.weather_days,), daemon=True).start()

    app.run(host=args.host, port=args.port, debug=False)

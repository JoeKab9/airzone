#!/usr/bin/env python3
"""
Airzone Dashboard — Lightweight web UI
========================================
Reads the SQLite database and serves a simple dashboard showing
current zone readings, DHW status, and historical charts.

Usage:
    python3 airzone_dashboard_server.py              # port 8080
    python3 airzone_dashboard_server.py --port 9090
"""
from __future__ import annotations

import json
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "airzone_raw.db"

app = Flask(__name__)

DASHBOARD_HTML = """
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
            display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 20px; color: #38bdf8; }
  .header .status { font-size: 13px; color: #94a3b8; }
  .header .status .dot { display: inline-block; width: 8px; height: 8px;
           border-radius: 50%; background: #22c55e; margin-right: 6px; }
  .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
          gap: 16px; margin-bottom: 24px; }
  .card { background: #1e293b; border-radius: 12px; padding: 18px;
          border: 1px solid #334155; }
  .card h3 { font-size: 14px; color: #94a3b8; margin-bottom: 10px;
             text-transform: uppercase; letter-spacing: 0.5px; }
  .card .temp { font-size: 36px; font-weight: 700; color: #f1f5f9; }
  .card .temp .unit { font-size: 18px; color: #64748b; }
  .card .meta { display: flex; gap: 16px; margin-top: 8px; font-size: 13px; color: #94a3b8; }
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
  .stats { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 24px; }
  .stat { background: #1e293b; border-radius: 8px; padding: 14px 18px;
          border: 1px solid #334155; }
  .stat .val { font-size: 24px; font-weight: 700; color: #38bdf8; }
  .stat .lbl { font-size: 12px; color: #64748b; margin-top: 2px; }
  .period-btns { display: flex; gap: 8px; margin-bottom: 16px; }
  .period-btns button { background: #334155; color: #94a3b8; border: none;
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .period-btns button.active { background: #38bdf8; color: #0f172a; }
</style>
</head>
<body>
<div class="header">
  <h1>Airzone Dashboard</h1>
  <div class="status"><span class="dot"></span><span id="lastPoll">Loading...</span></div>
</div>
<div class="container">
  <div class="stats" id="statsBar"></div>
  <div class="grid" id="zoneGrid"></div>
  <div class="period-btns">
    <button onclick="setPeriod(6)" id="btn6h">6h</button>
    <button onclick="setPeriod(24)" class="active" id="btn24h">24h</button>
    <button onclick="setPeriod(72)" id="btn72h">3d</button>
    <button onclick="setPeriod(168)" id="btn168h">7d</button>
  </div>
  <div class="chart-container">
    <h2>Temperature History</h2>
    <canvas id="tempChart"></canvas>
  </div>
  <div class="chart-container">
    <h2>Humidity History</h2>
    <canvas id="humChart"></canvas>
  </div>
  <div class="chart-container">
    <h2>DHW Tank Temperature</h2>
    <canvas id="dhwChart"></canvas>
  </div>
</div>
<script>
let currentPeriod = 24;
let tempChart, humChart, dhwChart;
const colors = ['#38bdf8','#a78bfa','#fb923c','#4ade80','#f472b6','#facc15','#34d399','#f87171','#818cf8'];
const TZ = 'Europe/Paris';
function toLocal(ts) { return new Date(ts.endsWith('Z') ? ts : ts + 'Z'); }
// Use relative paths so it works behind any reverse proxy prefix
const BASE = window.location.pathname.replace(/\/$/, '');

function setPeriod(h) {
  currentPeriod = h;
  document.querySelectorAll('.period-btns button').forEach(b => b.classList.remove('active'));
  document.getElementById('btn' + h + 'h').classList.add('active');
  loadHistory();
}

async function loadCurrent() {
  const r = await fetch(BASE + '/api/current');
  const d = await r.json();
  const lp = d.last_poll ? new Date(d.last_poll + 'Z').toLocaleString('fr-FR', {timeZone: 'Europe/Paris'}) : 'N/A';
  document.getElementById('lastPoll').textContent = 'Last poll: ' + lp;

  const statsHtml = `
    <div class="stat"><div class="val">${d.total_readings.toLocaleString()}</div><div class="lbl">Total readings</div></div>
    <div class="stat"><div class="val">${d.zones.length}</div><div class="lbl">Active zones</div></div>
    <div class="stat"><div class="val">${d.db_size_mb} MB</div><div class="lbl">Database</div></div>
    <div class="stat"><div class="val">${d.uptime_days}d</div><div class="lbl">Collecting since</div></div>
  `;
  document.getElementById('statsBar').innerHTML = statsHtml;

  let html = '';
  if (d.dhw) {
    html += `<div class="card dhw">
      <h3>Hot Water (DHW)</h3>
      <div class="temp">${d.dhw.tank_temp || '?'}<span class="unit">°C</span></div>
      <div class="meta">
        <span><span class="label">Setpoint </span>${d.dhw.setpoint || '?'}°C</span>
        <span class="badge ${d.dhw.power ? 'on' : 'off'}">${d.dhw.power ? 'ON' : 'OFF'}</span>
      </div>
    </div>`;
  }
  d.zones.forEach(z => {
    html += `<div class="card">
      <h3>${z.zone_name}</h3>
      <div class="temp">${z.temperature !== null ? z.temperature.toFixed(1) : '?'}<span class="unit">°C</span></div>
      <div class="meta">
        <span><span class="label">Humidity </span>${z.humidity || '?'}%</span>
        <span><span class="label">SP </span>${z.setpoint_heat || '?'}°C</span>
        <span class="badge ${z.power ? 'on' : 'off'}">${z.power ? 'ON' : 'OFF'}</span>
      </div>
    </div>`;
  });
  document.getElementById('zoneGrid').innerHTML = html;
}

async function loadHistory() {
  const r = await fetch(BASE + '/api/history?hours=' + currentPeriod);
  const d = await r.json();

  const chartOpts = {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
    scales: {
      x: { type: 'time', ticks: { color: '#64748b',
        callback: function(val) { return new Date(val).toLocaleString('fr-FR', {timeZone: TZ, hour: '2-digit', minute: '2-digit'}); }
      }, grid: { color: '#1e293b' } },
      y: { ticks: { color: '#64748b' }, grid: { color: '#334155' } }
    }
  };

  // Temperature chart
  const tempDs = Object.keys(d.zones).map((name, i) => ({
    label: name,
    data: d.zones[name].map(p => ({ x: toLocal(p.t), y: p.temp })),
    borderColor: colors[i % colors.length],
    borderWidth: 1.5, pointRadius: 0, tension: 0.3,
  }));
  if (tempChart) tempChart.destroy();
  tempChart = new Chart(document.getElementById('tempChart'), {
    type: 'line', data: { datasets: tempDs }, options: chartOpts
  });

  // Humidity chart
  const humDs = Object.keys(d.zones).map((name, i) => ({
    label: name,
    data: d.zones[name].map(p => ({ x: toLocal(p.t), y: p.hum })),
    borderColor: colors[i % colors.length],
    borderWidth: 1.5, pointRadius: 0, tension: 0.3,
  }));
  if (humChart) humChart.destroy();
  humChart = new Chart(document.getElementById('humChart'), {
    type: 'line', data: { datasets: humDs }, options: chartOpts
  });

  // DHW chart
  if (d.dhw && d.dhw.length > 0) {
    const dhwDs = [{
      label: 'Tank temp',
      data: d.dhw.map(p => ({ x: toLocal(p.t), y: p.tank })),
      borderColor: '#fbbf24', borderWidth: 2, pointRadius: 0, tension: 0.3,
    }, {
      label: 'Setpoint',
      data: d.dhw.map(p => ({ x: toLocal(p.t), y: p.sp })),
      borderColor: '#f59e0b', borderWidth: 1, borderDash: [4, 4], pointRadius: 0,
    }];
    if (dhwChart) dhwChart.destroy();
    dhwChart = new Chart(document.getElementById('dhwChart'), {
      type: 'line', data: { datasets: dhwDs }, options: chartOpts
    });
  }
}

loadCurrent();
loadHistory();
setInterval(loadCurrent, 60000);
setInterval(loadHistory, 300000);
</script>
</body>
</html>
"""


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/current")
def api_current():
    conn = get_db()

    # Latest zone readings (one per zone)
    zones = conn.execute("""
        SELECT z.* FROM zone_readings z
        INNER JOIN (
            SELECT zone_name, MAX(timestamp) as max_ts
            FROM zone_readings WHERE zone_name IS NOT NULL
            GROUP BY zone_name
        ) latest ON z.zone_name = latest.zone_name AND z.timestamp = latest.max_ts
        ORDER BY z.zone_name
    """).fetchall()

    # Latest DHW
    dhw = conn.execute("""
        SELECT * FROM dhw_readings ORDER BY timestamp DESC LIMIT 1
    """).fetchone()

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM zone_readings").fetchone()[0]
    first = conn.execute("SELECT MIN(timestamp) FROM zone_readings").fetchone()[0]
    last = conn.execute("SELECT MAX(timestamp) FROM zone_readings").fetchone()[0]

    conn.close()

    size_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 1) if DB_PATH.exists() else 0

    # Calculate uptime days
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
    from flask import request
    hours = int(request.args.get("hours", 24))
    conn = get_db()

    # Zone history (downsample for large ranges)
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

    # DHW history
    dhw_rows = conn.execute("""
        SELECT timestamp, tank_temp, setpoint
        FROM dhw_readings
        WHERE timestamp > datetime('now', ? || ' hours')
        ORDER BY timestamp
    """, (f"-{hours}",)).fetchall()

    conn.close()

    return jsonify({
        "zones": zones,
        "dhw": [{"t": r["timestamp"], "tank": r["tank_temp"], "sp": r["setpoint"]} for r in dhw_rows],
    })


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    zones = conn.execute("SELECT COUNT(*) FROM zone_readings").fetchone()[0]
    dhw = conn.execute("SELECT COUNT(*) FROM dhw_readings").fetchone()[0]
    first = conn.execute("SELECT MIN(timestamp) FROM zone_readings").fetchone()[0]
    last = conn.execute("SELECT MAX(timestamp) FROM zone_readings").fetchone()[0]
    distinct = conn.execute("SELECT COUNT(DISTINCT zone_name) FROM zone_readings").fetchone()[0]
    conn.close()
    size_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 2) if DB_PATH.exists() else 0
    return jsonify({
        "zone_readings": zones, "dhw_readings": dhw,
        "db_size_mb": size_mb, "distinct_zones": distinct,
        "first_reading": first, "last_reading": last,
    })


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)

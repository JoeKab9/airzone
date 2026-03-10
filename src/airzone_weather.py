"""
Airzone Weather Optimization
==============================
Fetches hourly temperature forecasts from Open-Meteo (free, no API key)
and computes the optimal warm window for heat-pump scheduling.

Heat pumps have a higher COP (coefficient of performance) at higher
outdoor temperatures, so running heating during the warmest hours
of the day saves electricity.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python 3.8

import requests

log = logging.getLogger("airzone")

if getattr(sys, "frozen", False):
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Airzone"
else:
    DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
WEATHER_CACHE_PATH = DATA_DIR / "airzone_weather_cache.json"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Module-level in-memory cache
_cache = {
    "fetched_at": 0,
    "hourly_times": [],
    "hourly_temps": [],
    "hourly_dew_points": [],
    "hourly_rel_humidity": [],
}


def fetch_forecast(lat: float, lon: float, timeout: int = 10) -> dict:
    """Fetch hourly temperature forecast from Open-Meteo."""
    resp = requests.get(OPEN_METEO_URL, params={
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,dew_point_2m,relative_humidity_2m",
        "timezone": "Europe/Paris",
        "forecast_days": 2,
    }, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def get_forecast(lat: float, lon: float, max_age_seconds: int = 3600) -> dict:
    """Return cached forecast, refreshing if older than max_age_seconds."""
    global _cache
    now = time.time()

    # Try memory cache first
    if _cache["fetched_at"] and (now - _cache["fetched_at"]) < max_age_seconds:
        return _cache

    # Try disk cache
    if WEATHER_CACHE_PATH.exists():
        try:
            disk = json.loads(WEATHER_CACHE_PATH.read_text())
            if (now - disk.get("fetched_at", 0)) < max_age_seconds:
                _cache = disk
                return _cache
        except Exception:
            pass

    # Fetch fresh
    try:
        data = fetch_forecast(lat, lon)
        _cache = {
            "fetched_at": now,
            "hourly_times": data["hourly"]["time"],
            "hourly_temps": data["hourly"]["temperature_2m"],
            "hourly_dew_points": data["hourly"].get("dew_point_2m", []),
            "hourly_rel_humidity": data["hourly"].get(
                "relative_humidity_2m", []),
        }
        WEATHER_CACHE_PATH.write_text(json.dumps(_cache))
        log.info("Weather forecast refreshed (%d hours of data)",
                 len(_cache["hourly_times"]))
    except Exception as e:
        log.error("Failed to fetch weather forecast: %s", e)

    return _cache


def compute_warm_window(forecast: dict, warm_hours_count: int = 6) -> dict:
    """
    From the forecast, find the N warmest hours in the next 24h.

    This intentionally uses a rolling 24h window so heating CAN be
    deferred to tomorrow's warm afternoon (better COP = cheaper).
    The controller enforces a max_defer_hours safety limit separately.
    """
    # Open-Meteo returns times in the requested timezone (Europe/Paris).
    # Use the same timezone for "now" so hour/date comparisons are correct,
    # even when the system clock is UTC (e.g. Raspberry Pi).
    tz_paris = ZoneInfo("Europe/Paris")
    now = datetime.now(tz_paris).replace(tzinfo=None)  # naive, Paris local
    window_end = now + timedelta(hours=24)

    times = forecast.get("hourly_times", [])
    temps = forecast.get("hourly_temps", [])
    dew_points = forecast.get("hourly_dew_points", [])

    candidates = []
    current_hour_temp = None
    current_hour_dew_point = None
    current_hour_humidity = None
    rel_humidities = forecast.get("hourly_rel_humidity", [])

    for i, (t_str, temp) in enumerate(zip(times, temps)):
        if temp is None:
            continue
        t = datetime.fromisoformat(t_str)  # naive, Paris local (from API)
        # Include hours from now to 24h ahead
        if now - timedelta(minutes=30) <= t <= window_end:
            candidates.append((t, temp))
        # Current outdoor temp + dew point + humidity = closest hour to now
        if t.hour == now.hour and t.date() == now.date():
            current_hour_temp = temp
            if i < len(dew_points) and dew_points[i] is not None:
                current_hour_dew_point = dew_points[i]
            if i < len(rel_humidities) and rel_humidities[i] is not None:
                current_hour_humidity = rel_humidities[i]

    if not candidates:
        return {
            "forecast_fetched_at": datetime.fromtimestamp(
                forecast.get("fetched_at", 0)).isoformat(),
            "warm_hours": [],
            "current_outdoor_temp": current_hour_temp,
            "current_outdoor_dew_point": current_hour_dew_point,
            "current_outdoor_humidity": current_hour_humidity,
            "avg_warm_temp": None,
            "is_warm_now": False,
            "next_warm_start": None,
            "next_warm_end": None,
        }

    # Pick the N warmest hours
    by_temp = sorted(candidates, key=lambda x: x[1], reverse=True)
    warmest = by_temp[:warm_hours_count]

    # Sort chronologically for display
    warmest_sorted = sorted(warmest, key=lambda x: x[0])

    warm_hour_set = {t.replace(minute=0, second=0, microsecond=0) for t, _ in warmest}
    now_hour = now.replace(minute=0, second=0, microsecond=0)
    is_warm = now_hour in warm_hour_set

    avg_warm = sum(t for _, t in warmest) / len(warmest)

    # Find next warm block start/end for display
    future_warm = sorted([t for t, _ in warmest_sorted if t >= now_hour])
    next_start = future_warm[0].isoformat() if future_warm else None

    next_end = None
    if future_warm:
        end = future_warm[0]
        for t in future_warm:
            if t <= end + timedelta(hours=1):
                end = t
            else:
                break
        next_end = (end + timedelta(hours=1)).isoformat()

    return {
        "forecast_fetched_at": datetime.fromtimestamp(
            forecast.get("fetched_at", 0)).isoformat(),
        "warm_hours": [
            {"hour": t.isoformat(), "temp_c": temp}
            for t, temp in warmest_sorted
        ],
        "current_outdoor_temp": current_hour_temp,
        "current_outdoor_dew_point": current_hour_dew_point,
        "current_outdoor_humidity": current_hour_humidity,
        "avg_warm_temp": round(avg_warm, 1),
        "is_warm_now": is_warm,
        "next_warm_start": next_start,
        "next_warm_end": next_end,
    }


def estimate_cop_savings(current_temp: float, avg_warm_temp: float) -> dict:
    """
    Estimate COP and savings from deferring to warm window.

    Linear COP model for air-source heat pumps:
        COP ≈ 0.5 + 0.05 × outdoor_temp

    Example: 5°C now → COP 0.75,  14°C warm window → COP 1.20
             Savings = (1 - 0.75/1.20) × 100 ≈ 37%
    """
    cop_now = max(0.5 + 0.05 * current_temp, 0.5)
    cop_warm = max(0.5 + 0.05 * avg_warm_temp, 0.5)
    savings_pct = (1 - cop_now / cop_warm) * 100 if cop_warm > cop_now else 0
    return {
        "cop_now": round(cop_now, 2),
        "cop_warm": round(cop_warm, 2),
        "savings_pct": round(max(savings_pct, 0), 1),
    }

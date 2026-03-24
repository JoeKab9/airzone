#!/usr/bin/env python3
"""
best_price.py - Find the cheapest EDF electricity offer based on your Linky data.

Reads LINKY_TOKEN and LINKY_PRM from the project .env file (../airzone/.env).
Additional settings (kVA, HC schedule) can be passed as CLI args or default to 9kVA / 22h-6h.

Requires: pip install requests

Usage:
  python best_price.py              # Full analysis (half-hourly data, 5 years)
  python best_price.py --quick      # Quick mode (daily data only, faster)
  python best_price.py --years 3    # Only 3 years of data
  python best_price.py --csv data.csv  # Use Enedis CSV export instead of API
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — reads from project .env (same as airzone_secrets)
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR = Path(__file__).parent
CACHE_DIR = SCRIPT_DIR / "cache"

# Walk up to find the project root .env (where LINKY_TOKEN lives)
def _find_project_env() -> Path:
    d = SCRIPT_DIR.resolve()
    for _ in range(5):
        candidate = d / ".env"
        if candidate.exists():
            return candidate
        d = d.parent
    return SCRIPT_DIR / ".env"  # fallback

PROJECT_ENV = _find_project_env()


def _parse_env(path: Path) -> dict:
    """Parse a .env file into a flat dict."""
    data = {}
    if not path.exists():
        return data
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if value:
                data[key] = value
    return data


def load_config() -> dict:
    """Load Linky credentials from the project .env file."""
    env = _parse_env(PROJECT_ENV)
    return {
        "token": env.get("LINKY_TOKEN", ""),
        "prm": env.get("LINKY_PRM", ""),
        "rte_client_id": env.get("RTE_CLIENT_ID", ""),
        "rte_client_secret": env.get("RTE_CLIENT_SECRET", ""),
    }

# ═══════════════════════════════════════════════════════════════════════════════
# PRICE DATA — All EDF offers, TTC, applicable 1er février 2026
# ═══════════════════════════════════════════════════════════════════════════════

# Monthly subscriptions (€ TTC / month) by kVA
_SUB_REG_BASE = {3: 12.03, 6: 15.65, 9: 19.56, 12: 23.32, 15: 26.84, 18: 30.49, 24: 38.24, 30: 45.37, 36: 52.54}
_SUB_REG_HC   = {6: 15.65, 9: 19.56, 12: 23.32, 15: 26.84, 18: 30.49, 24: 38.24, 30: 45.37, 36: 52.54}
_SUB_REG_TEMP = {6: 15.59, 9: 19.38, 12: 23.07, 15: 26.47, 18: 30.04, 30: 44.73, 36: 52.42}
_SUB_MKT_BASE = {3: 11.25, 6: 14.78, 9: 18.49, 12: 22.21, 15: 25.74, 18: 29.23, 24: 36.84, 30: 44.07, 36: 51.50}
_SUB_MKT_HC   = {6: 15.05, 9: 18.91, 12: 22.65, 15: 26.17, 18: 29.81, 24: 37.52, 30: 44.65, 36: 51.82}

# kWh prices (cts € TTC / kWh)
OFFERS = [
    # ── Tarif Bleu (regulated) ──
    {"name": "Tarif Bleu - Base",            "sub": _SUB_REG_BASE, "type": "base",
     "p": {"base_kva": {3:19.40,6:19.40,9:19.27,12:19.27,15:19.27,18:19.27,24:19.27,30:19.27,36:19.27}}},
    {"name": "Tarif Bleu - Heures Creuses",  "sub": _SUB_REG_HC,   "type": "hc",
     "p": {"hp": 20.65, "hc": 15.79}},
    {"name": "Tarif Bleu - Tempo",           "sub": _SUB_REG_TEMP, "type": "tempo",
     "p": {"bleu_hc":13.25,"bleu_hp":16.12,"blanc_hc":14.99,"blanc_hp":18.71,"rouge_hc":15.75,"rouge_hp":70.60}},

    # ── Zen Fixe ──
    {"name": "Zen Fixe - Base",              "sub": _SUB_MKT_BASE, "type": "base",    "p": {"base": 17.74}},
    {"name": "Zen Fixe - Heures Creuses",    "sub": _SUB_MKT_HC,   "type": "hc",      "p": {"hp": 18.88, "hc": 14.96}},

    # ── Vert Électrique ──
    {"name": "Vert Électrique - Base",       "sub": _SUB_MKT_BASE, "type": "base",    "p": {"base": 18.81}},
    {"name": "Vert Électrique - HC",         "sub": _SUB_MKT_HC,   "type": "hc",      "p": {"hp": 20.21, "hc": 15.76}},

    # ── Zen Online ──
    {"name": "Zen Online - Base",            "sub": _SUB_REG_BASE, "type": "base",
     "p": {"base_kva": {3:18.45,6:18.45,9:18.33,12:18.33,15:18.33,18:18.33,24:18.33,30:18.33,36:18.33}}},
    {"name": "Zen Online - Heures Creuses",  "sub": _SUB_REG_HC,   "type": "hc",      "p": {"hp": 19.63, "hc": 15.05}},

    # ── Zen Week-End ──
    {"name": "Zen Week-End - WE",            "sub": _SUB_MKT_BASE, "type": "we",
     "p": {"sem": 20.38, "we": 15.38}},
    {"name": "Zen Week-End - HC+WE",         "sub": _SUB_MKT_HC,   "type": "hcwe",
     "p": {"hp_s": 21.53, "hc_s": 16.18, "hp_w": 16.18, "hc_w": 16.18}},
    {"name": "Zen Week-End - Flex",          "sub": _SUB_MKT_HC,   "type": "flex",
     "p": {"hc_eco": 15.19, "hp_eco": 20.91, "hc_sob": 20.91, "hp_sob": 72.53}},

    # ── Zen Week-End Plus ──
    {"name": "Zen WE Plus - WE+Jour",       "sub": _SUB_MKT_BASE, "type": "wej",
     "p": {"sem": 21.33, "we": 16.04, "jour": 16.04}},
    {"name": "Zen WE Plus - HC+WE+Jour",    "sub": _SUB_MKT_HC,   "type": "hcwej",
     "p": {"hp_s": 22.13, "hc_s": 16.60, "hp_w": 16.60, "hc_w": 16.60, "hp_j": 16.60, "hc_j": 16.60}},

    # ── Vert Électrique Week-End ──
    {"name": "Vert Élec. WE - WE",          "sub": _SUB_MKT_BASE, "type": "we",
     "p": {"sem": 20.53, "we": 15.47}},
    {"name": "Vert Élec. WE - HC+WE",       "sub": _SUB_MKT_HC,   "type": "hcwe",
     "p": {"hp_s": 21.58, "hc_s": 16.22, "hp_w": 16.22, "hc_w": 16.22}},

    # ── Vert Électrique Auto ──
    {"name": "Vert Élec. Auto - HC",         "sub": _SUB_MKT_HC,   "type": "hc",
     "p": {"hp": 22.29, "hc": 13.00}},

    # ── Vert Électrique Régional ──
    {"name": "Vert Élec. Régional - Base",   "sub": _SUB_MKT_BASE, "type": "base",
     "p": {"base_kva": {6:19.06,9:19.15,12:19.15,15:19.15,18:19.15,24:19.15,30:19.15,36:19.15}}},
    {"name": "Vert Élec. Régional - HC",     "sub": _SUB_MKT_HC,   "type": "hc",
     "p": {"hp": 21.05, "hc": 15.15}},
]


# ═══════════════════════════════════════════════════════════════════════════════
# CONSO API  (conso.boris.sh)
# ═══════════════════════════════════════════════════════════════════════════════

API_BASE = "https://conso.boris.sh/api"
DELAY = 0.6  # conservative shared rate-limit


def _api(endpoint: str, token: str, prm: str, start: str, end: str) -> Optional[list]:
    """Call conso.boris.sh API. Returns list of interval_reading dicts or None."""
    r = requests.get(
        f"{API_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "best-price/1.0"},
        params={"prm": prm, "start": start, "end": end},
        timeout=30,
    )
    if r.status_code == 200:
        data = r.json()
        # Response may be flat or nested under meter_reading
        if "meter_reading" in data:
            return data["meter_reading"].get("interval_reading", [])
        return data.get("interval_reading", [])
    if r.status_code != 404:
        print(f"    API {r.status_code}: {r.text[:120]}", file=sys.stderr)
    return None


def fetch_load_curve(token: str, prm: str, start_d: date, end_d: date, cache: bool) -> List[dict]:
    tag = f"lc_{prm}_{start_d}_{end_d}"
    cached = _cache_load(tag) if cache else None
    if cached is not None:
        print(f"    Loaded {len(cached):,} cached half-hourly readings")
        return cached

    readings: List[dict] = []
    total = (end_d - start_d).days
    cur = start_d
    done = 0
    while cur < end_d:
        nxt = min(cur + timedelta(days=7), end_d)
        intervals = _api("consumption_load_curve", token, prm, cur.isoformat(), nxt.isoformat())
        if intervals:
            readings.extend(intervals)
        done += (nxt - cur).days
        print(f"\r    Half-hourly: {done}/{total} days ({done*100//total}%)", end="", flush=True)
        cur = nxt
        time.sleep(DELAY)
    print()

    if readings:
        _cache_save(tag, readings)
    return readings


def fetch_daily(token: str, prm: str, start_d: date, end_d: date, cache: bool) -> List[dict]:
    tag = f"dc_{prm}_{start_d}_{end_d}"
    cached = _cache_load(tag) if cache else None
    if cached is not None:
        print(f"    Loaded {len(cached):,} cached daily readings")
        return cached

    readings: List[dict] = []
    cur = start_d
    while cur < end_d:
        nxt = min(cur + timedelta(days=365), end_d)
        intervals = _api("daily_consumption", token, prm, cur.isoformat(), nxt.isoformat())
        if intervals:
            readings.extend(intervals)
        cur = nxt
        time.sleep(DELAY)

    if readings:
        _cache_save(tag, readings)
    print(f"    Fetched {len(readings):,} daily readings")
    return readings


def _cache_load(tag: str) -> Optional[list]:
    p = CACHE_DIR / f"{tag}.json"
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return None


def _cache_save(tag: str, data: list):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_DIR / f"{tag}.json", "w") as f:
        json.dump(data, f)


# ═══════════════════════════════════════════════════════════════════════════════
# CSV IMPORT  (Enedis export)
# ═══════════════════════════════════════════════════════════════════════════════

def load_csv(path: str) -> Tuple[List[dict], str]:
    p = Path(path)
    if not p.exists():
        sys.exit(f"CSV not found: {path}")

    text = None
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            text = p.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        sys.exit("Cannot decode CSV")

    sep = ";" if ";" in text.split("\n")[0] else ","
    lines = text.strip().split("\n")

    # find header row
    hdr_i = 0
    for i, ln in enumerate(lines):
        if any(k in ln.lower() for k in ("horodate", "horodatage", "date", "valeur")):
            hdr_i = i
            break

    hdr = [h.strip().strip('"').lower() for h in lines[hdr_i].split(sep)]
    dc = vc = None
    for i, h in enumerate(hdr):
        if any(k in h for k in ("horodate", "horodatage", "date")):
            dc = i
        if any(k in h for k in ("valeur", "consommation", "value", "wh", "kwh")):
            vc = i
    if dc is None or vc is None:
        sys.exit(f"Cannot find date/value columns in header: {hdr}")

    readings = []
    dtype = None
    for ln in lines[hdr_i + 1:]:
        if not ln.strip():
            continue
        parts = ln.split(sep)
        if len(parts) <= max(dc, vc):
            continue
        ds = parts[dc].strip().strip('"')
        vs = parts[vc].strip().strip('"').replace(",", ".").replace(" ", "")
        if not vs:
            continue
        try:
            val = float(vs)
        except ValueError:
            continue
        if dtype is None:
            dtype = "load_curve" if (" " in ds or "T" in ds) else "daily"
        dt = _parse_dt(ds)
        if dt is None:
            continue
        fmt = "%Y-%m-%d %H:%M:%S" if dtype == "load_curve" else "%Y-%m-%d"
        readings.append({"date": dt.strftime(fmt), "value": str(int(val))})

    print(f"    Loaded {len(readings):,} readings from CSV ({dtype})")
    return readings, dtype


# ═══════════════════════════════════════════════════════════════════════════════
# TEMPO & ECOWATT
# ═══════════════════════════════════════════════════════════════════════════════

def load_tempo(start_d: date, end_d: date, rte_id: str = "", rte_sec: str = "") -> Dict[date, str]:
    colors: Dict[date, str] = {}

    # 1) cache
    cf = CACHE_DIR / "tempo.json"
    if cf.exists():
        with open(cf) as f:
            for k, v in json.load(f).items():
                try:
                    d = date.fromisoformat(k)
                    if start_d <= d <= end_d:
                        colors[d] = v
                except ValueError:
                    pass
        if colors:
            print(f"    Loaded {len(colors)} Tempo days from cache")

    # 2) RTE API
    total = (end_d - start_d).days + 1
    missing = total - len(colors)
    if missing > 0 and rte_id and rte_sec and HAS_REQUESTS:
        print("    Fetching Tempo colours from RTE...")
        new = _fetch_rte_tempo(rte_id, rte_sec, start_d, end_d)
        colors.update(new)
        if new:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(cf, "w") as f:
                json.dump({d.isoformat(): c for d, c in colors.items()}, f)

    # 3) estimate missing
    colors = _estimate_tempo(start_d, end_d, colors)
    return colors


def _fetch_rte_tempo(cid: str, csec: str, s: date, e: date) -> Dict[date, str]:
    out: Dict[date, str] = {}
    try:
        tr = requests.post("https://digital.iservices.rte-france.com/token/oauth/",
                           auth=(cid, csec), data={"grant_type": "client_credentials"}, timeout=15)
        if tr.status_code != 200:
            return out
        tok = tr.json()["access_token"]
        r = requests.get(
            "https://digital.iservices.rte-france.com/open_api/tempo_like_supply_contract/v1/tempo_like_calendars",
            headers={"Authorization": f"Bearer {tok}"},
            params={"start_date": f"{s}T00:00:00+02:00", "end_date": f"{e}T00:00:00+02:00",
                    "fallback_status": "true"}, timeout=30)
        if r.status_code == 200:
            for v in r.json().get("tempo_like_calendars", {}).get("values", []):
                ds = v.get("start_date", "")[:10]
                val = v.get("value", "").upper()
                try:
                    d = date.fromisoformat(ds)
                except ValueError:
                    continue
                if "BLUE" in val or "BLEU" in val:
                    out[d] = "bleu"
                elif "WHITE" in val or "BLANC" in val:
                    out[d] = "blanc"
                elif "RED" in val or "ROUGE" in val:
                    out[d] = "rouge"
            print(f"    Fetched {len(out)} Tempo days from RTE")
    except Exception as ex:
        print(f"    RTE error: {ex}", file=sys.stderr)
    return out


def _estimate_tempo(start_d: date, end_d: date, known: Dict[date, str]) -> Dict[date, str]:
    colors = dict(known)
    probs = {1: (.20, .15), 2: (.18, .12), 3: (.12, .05), 4: (.05, 0), 5: (.02, 0), 6: (0, 0),
             7: (0, 0), 8: (0, 0), 9: (0, 0), 10: (.05, 0), 11: (.15, .08), 12: (.20, .18)}
    cur = start_d
    while cur <= end_d:
        if cur not in colors:
            bp, rp = probs[cur.month]
            if cur.weekday() == 6:  # never red on Sunday
                rp = 0
            h = (cur.toordinal() * 31 + cur.month * 7) % 100
            colors[cur] = "rouge" if h < rp * 100 else "blanc" if h < (rp + bp) * 100 else "bleu"
        cur += timedelta(days=1)
    return colors


def estimate_ecowatt(start_d: date, end_d: date) -> Dict[date, str]:
    sigs: Dict[date, str] = {}
    mp = {1: .08, 2: .06, 3: .02, 4: 0, 5: 0, 6: 0, 7: 0, 8: 0, 9: 0, 10: 0, 11: .03, 12: .08}
    cur = start_d
    while cur <= end_d:
        p = mp[cur.month] if cur.weekday() < 5 else 0
        h = (cur.toordinal() * 17 + cur.month * 13) % 100
        sigs[cur] = "sob" if h < p * 100 else "eco"
        cur += timedelta(days=1)
    return sigs


# ═══════════════════════════════════════════════════════════════════════════════
# CONSUMPTION ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_dt(s: str) -> Optional[datetime]:
    s = re.sub(r"[+-]\d{2}:\d{2}$", "", s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _is_hc(hour: int, hc_s: int, hc_e: int) -> bool:
    return (hour >= hc_s or hour < hc_e) if hc_s > hc_e else (hc_s <= hour < hc_e)


class Breakdown:
    def __init__(self):
        self.total = 0.0
        self.hp = 0.0
        self.hc = 0.0
        self.wd = 0.0          # weekday
        self.we = 0.0          # weekend
        self.wd_hp = 0.0
        self.wd_hc = 0.0
        self.we_hp = 0.0
        self.we_hc = 0.0
        self.day_kwh = defaultdict(float)      # weekday index 0-4 → total
        self.day_hp  = defaultdict(float)
        self.day_hc  = defaultdict(float)
        self.tempo   = defaultdict(float)      # "bleu_hp" etc.
        self.flex    = defaultdict(float)      # "eco_hp", "sob_hc" etc.
        self.start: Optional[date] = None
        self.end: Optional[date] = None
        self.n_days = 0
        self.n_pts = 0


def analyze_curve(readings: List[dict], hc_s: int, hc_e: int,
                  tempo: Dict[date, str], ecowatt: Dict[date, str]) -> Breakdown:
    b = Breakdown()
    for r in readings:
        dt = _parse_dt(r["date"])
        if dt is None:
            continue
        try:
            w = float(r["value"])
        except (ValueError, TypeError):
            continue
        kwh = w * 0.5 / 1000.0
        d = dt.date()
        h = dt.hour
        wkend = dt.weekday() >= 5
        hc = _is_hc(h, hc_s, hc_e)

        b.total += kwh
        b.n_pts += 1
        if b.start is None or d < b.start:
            b.start = d
        if b.end is None or d > b.end:
            b.end = d

        if hc:
            b.hc += kwh
        else:
            b.hp += kwh

        if wkend:
            b.we += kwh
            if hc:
                b.we_hc += kwh
            else:
                b.we_hp += kwh
        else:
            b.wd += kwh
            if hc:
                b.wd_hc += kwh
            else:
                b.wd_hp += kwh
            wd = dt.weekday()
            b.day_kwh[wd] += kwh
            (b.day_hc if hc else b.day_hp)[wd] += kwh

        tc = tempo.get(d, "bleu")
        b.tempo[f"{tc}_{'hc' if hc else 'hp'}"] += kwh

        ec = ecowatt.get(d, "eco")
        b.flex[f"{ec}_{'hc' if hc else 'hp'}"] += kwh

    if b.start and b.end:
        b.n_days = (b.end - b.start).days + 1
    return b


def analyze_daily(readings: List[dict], hc_ratio: float = 0.26,
                   tempo: Dict[date, str] = None,
                   ecowatt: Dict[date, str] = None) -> Breakdown:
    b = Breakdown()
    tempo = tempo or {}
    ecowatt = ecowatt or {}
    for r in readings:
        dt = _parse_dt(r["date"])
        if dt is None:
            continue
        try:
            wh = float(r["value"])
        except (ValueError, TypeError):
            continue
        kwh = wh / 1000.0
        d = dt.date()
        wkend = dt.weekday() >= 5
        hc_kwh = kwh * hc_ratio
        hp_kwh = kwh * (1 - hc_ratio)

        b.total += kwh
        b.n_pts += 1
        if b.start is None or d < b.start:
            b.start = d
        if b.end is None or d > b.end:
            b.end = d
        b.hc += hc_kwh
        b.hp += hp_kwh

        if wkend:
            b.we += kwh
            b.we_hc += hc_kwh
            b.we_hp += hp_kwh
        else:
            b.wd += kwh
            b.wd_hc += hc_kwh
            b.wd_hp += hp_kwh
            wd = dt.weekday()
            b.day_kwh[wd] += kwh
            b.day_hc[wd] += hc_kwh
            b.day_hp[wd] += hp_kwh

        # Tempo (estimated HP/HC split per day)
        tc = tempo.get(d, "bleu")
        b.tempo[f"{tc}_hp"] += hp_kwh
        b.tempo[f"{tc}_hc"] += hc_kwh

        # Ecowatt / Flex
        ec = ecowatt.get(d, "eco")
        b.flex[f"{ec}_hp"] += hp_kwh
        b.flex[f"{ec}_hc"] += hc_kwh

    if b.start and b.end:
        b.n_days = (b.end - b.start).days + 1
    return b


# ═══════════════════════════════════════════════════════════════════════════════
# COST CALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

def _ann(val: float, days: int) -> float:
    return val * 365.25 / days if days > 0 else 0.0


def calc_cost(offer: dict, b: Breakdown, kva: int) -> Optional[dict]:
    sub_tbl = offer["sub"]
    if kva not in sub_tbl:
        return None
    monthly = sub_tbl[kva]
    annual_sub = monthly * 12
    p = offer["p"]
    t = offer["type"]
    nd = b.n_days

    if t == "base":
        if "base_kva" in p:
            pr = p["base_kva"].get(kva)
            if pr is None:
                return None
        else:
            pr = p["base"]
        energy = _ann(b.total, nd) * pr / 100

    elif t == "hc":
        energy = (_ann(b.hp, nd) * p["hp"] + _ann(b.hc, nd) * p["hc"]) / 100

    elif t == "tempo":
        energy = sum(_ann(b.tempo.get(k, 0), nd) * p[k] / 100
                     for k in ("bleu_hc", "bleu_hp", "blanc_hc", "blanc_hp", "rouge_hc", "rouge_hp"))

    elif t == "we":
        energy = (_ann(b.wd, nd) * p["sem"] + _ann(b.we, nd) * p["we"]) / 100

    elif t == "hcwe":
        energy = (_ann(b.wd_hp, nd) * p["hp_s"] + _ann(b.wd_hc, nd) * p["hc_s"]
                  + _ann(b.we_hp, nd) * p["hp_w"] + _ann(b.we_hc, nd) * p["hc_w"]) / 100

    elif t == "flex":
        energy = sum(_ann(b.flex.get(k, 0), nd) * p[pk] / 100
                     for k, pk in [("eco_hc", "hc_eco"), ("eco_hp", "hp_eco"),
                                   ("sob_hc", "hc_sob"), ("sob_hp", "hp_sob")])

    elif t == "wej":
        # try each weekday as "jour choisi", pick cheapest
        best = float("inf")
        for jc in range(5):
            rest = sum(b.day_kwh[d] for d in range(5) if d != jc)
            e = (_ann(rest, nd) * p["sem"] + _ann(b.day_kwh[jc], nd) * p["jour"]
                 + _ann(b.we, nd) * p["we"]) / 100
            best = min(best, e)
        energy = best

    elif t == "hcwej":
        best = float("inf")
        for jc in range(5):
            rest_hp = sum(b.day_hp[d] for d in range(5) if d != jc)
            rest_hc = sum(b.day_hc[d] for d in range(5) if d != jc)
            e = (_ann(rest_hp, nd) * p["hp_s"] + _ann(rest_hc, nd) * p["hc_s"]
                 + _ann(b.day_hp[jc], nd) * p["hp_j"] + _ann(b.day_hc[jc], nd) * p["hc_j"]
                 + _ann(b.we_hp, nd) * p["hp_w"] + _ann(b.we_hc, nd) * p["hc_w"]) / 100
            best = min(best, e)
        energy = best

    else:
        return None

    total = annual_sub + energy
    return {"name": offer["name"], "sub": annual_sub, "energy": energy,
            "total": total, "monthly": monthly}


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT
# ═══════════════════════════════════════════════════════════════════════════════

WD = {0: "Lundi", 1: "Mardi", 2: "Mercredi", 3: "Jeudi", 4: "Vendredi"}


def show_summary(b: Breakdown, dtype: str):
    ann = _ann(b.total, b.n_days)
    print()
    print("  ┌─────────────────────────────────────────────────────────────┐")
    print("  │  CONSUMPTION SUMMARY                                       │")
    print("  ├─────────────────────────────────────────────────────────────┤")
    print(f"  │  Period:      {b.start}  →  {b.end:<27}│")
    print(f"  │  Duration:    {b.n_days} days ({b.n_days/365.25:.1f} years){' ' * max(0, 24 - len(f'{b.n_days} days ({b.n_days/365.25:.1f} years)'))}│")
    print(f"  │  Data points: {b.n_pts:>10,}{' ' * 34}│")
    print(f"  │  Data type:   {'Half-hourly (30 min)' if dtype == 'load_curve' else 'Daily':40}│")
    print(f"  │{' ' * 59}│")
    print(f"  │  Total:       {b.total:>10,.0f} kWh{' ' * 30}│")
    print(f"  │  Annual avg:  {ann:>10,.0f} kWh/year{' ' * 25}│")
    if b.total > 0:
        print(f"  │{' ' * 59}│")
        print(f"  │  Heures Pleines:  {b.hp:>9,.0f} kWh  ({b.hp/b.total*100:4.1f}%){' ' * 18}│")
        print(f"  │  Heures Creuses:  {b.hc:>9,.0f} kWh  ({b.hc/b.total*100:4.1f}%){' ' * 18}│")
        print(f"  │  Weekday:         {b.wd:>9,.0f} kWh  ({b.wd/b.total*100:4.1f}%){' ' * 18}│")
        print(f"  │  Weekend:         {b.we:>9,.0f} kWh  ({b.we/b.total*100:4.1f}%){' ' * 18}│")
        tempo_t = sum(b.tempo.values())
        if tempo_t > 0:
            bl = b.tempo.get("bleu_hp", 0) + b.tempo.get("bleu_hc", 0)
            wh = b.tempo.get("blanc_hp", 0) + b.tempo.get("blanc_hc", 0)
            ro = b.tempo.get("rouge_hp", 0) + b.tempo.get("rouge_hc", 0)
            print(f"  │{' ' * 59}│")
            print(f"  │  Tempo Bleu:      {bl:>9,.0f} kWh  ({bl/tempo_t*100:4.1f}%){' ' * 18}│")
            print(f"  │  Tempo Blanc:     {wh:>9,.0f} kWh  ({wh/tempo_t*100:4.1f}%){' ' * 18}│")
            print(f"  │  Tempo Rouge:     {ro:>9,.0f} kWh  ({ro/tempo_t*100:4.1f}%){' ' * 18}│")
    print("  └─────────────────────────────────────────────────────────────┘")


def show_results(results: List[dict], kva: int):
    results.sort(key=lambda x: x["total"])
    ref = next((r["total"] for r in results if r["name"] == "Tarif Bleu - Base"), results[-1]["total"])

    print()
    print(f"  ╔{'═' * 88}╗")
    print(f"  ║  RANKING — {kva} kVA{' ' * (70 - len(str(kva)))}║")
    print(f"  ╠{'═' * 88}╣")
    print(f"  ║  {'#':<3} {'Offer':<35} {'Abo/year':>10} {'Energy':>10} {'TOTAL':>10} {'vs Bleu':>10}  ║")
    print(f"  ╟{'─' * 88}╢")

    for i, r in enumerate(results, 1):
        diff = r["total"] - ref
        ds = f"{diff:+,.0f}€" if abs(diff) > 0.5 else "ref"
        mark = " ★" if i == 1 else "  "
        print(f"  ║ {i:>2}  {r['name']:<35} {r['sub']:>9,.2f}€ {r['energy']:>9,.2f}€"
              f" {r['total']:>9,.2f}€ {ds:>10}{mark}║")

    print(f"  ╠{'═' * 88}╣")
    best = results[0]
    worst = results[-1]
    saving = worst["total"] - best["total"]
    print(f"  ║  Best:  {best['name']:<35}  →  {best['total']:>9,.2f}€/year{' ' * 21}║")
    print(f"  ║  Worst: {worst['name']:<35}  →  {worst['total']:>9,.2f}€/year{' ' * 21}║")
    print(f"  ║  Max savings: {saving:>,.2f}€/year{' ' * 53}║")
    if best["total"] < ref:
        s = ref - best["total"]
        print(f"  ║  vs Tarif Bleu Base: -{s:>,.2f}€/year{' ' * 47}║")
    print(f"  ╚{'═' * 88}╝")

    # top 3 detail
    print()
    print("  ── TOP 3 ──")
    for i, r in enumerate(results[:3], 1):
        print(f"\n  #{i}  {r['name']}")
        print(f"       Subscription:  {r['monthly']:.2f}€/month  =  {r['sub']:.2f}€/year")
        print(f"       Energy:        {r['energy']:.2f}€/year")
        print(f"       TOTAL:         {r['total']:.2f}€/year  ({r['total']/12:.2f}€/month)")


# ═══════════════════════════════════════════════════════════════════════════════
# INVOICE COMPARISON (real EDF bills from /Contis/Administration/EDF/)
# ═══════════════════════════════════════════════════════════════════════════════

# Historical EDF invoices — consumption and total TTC amounts
INVOICES = [
    {"period": "Jan 2025 → Jan 2026", "kwh": 2385, "total_ttc": 697.34, "tariff": "Bleu Base 9kVA"},
    {"period": "Jan 2024 → Jan 2025", "kwh": 2512, "total_ttc": 821.76, "tariff": "Bleu Base 9kVA"},
    {"period": "Sep 2023 → Jan 2024", "kwh":  661, "total_ttc": 340.46, "tariff": "Bleu Base 9kVA", "partial": True},
    {"period": "Jan 2022 → Jan 2023", "kwh": 1053, "total_ttc": 264.94, "tariff": "Bleu Base 9kVA", "partial": True},
]


def show_invoice_comparison(results: List[dict]):
    print("\n  ── INVOICE HISTORY (actual EDF bills) ──\n")
    best = results[0] if results else None
    for inv in INVOICES:
        partial = inv.get("partial", False)
        label = " (partial)" if partial else ""
        print(f"    {inv['period']}{label}:  {inv['kwh']:,} kWh  →  {inv['total_ttc']:,.2f}€ TTC  ({inv['tariff']})")
    if best:
        # Full-year invoices for comparison
        full = [i for i in INVOICES if not i.get("partial")]
        if full:
            avg_kwh = sum(i["kwh"] for i in full) / len(full)
            avg_paid = sum(i["total_ttc"] for i in full) / len(full)
            print(f"\n    Average (full years):  {avg_kwh:,.0f} kWh/yr  →  {avg_paid:,.2f}€/yr")
            print(f"    Best offer today:     {best['name']}  →  {best['total']:,.2f}€/yr")
            print(f"    vs your average bill: {best['total'] - avg_paid:+,.2f}€/yr")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args():
    ap = argparse.ArgumentParser(description="Find the cheapest EDF electricity offer from your Linky data.")
    ap.add_argument("--csv", help="Path to Enedis CSV export (instead of API)")
    ap.add_argument("--kva", type=int, default=9, help="Subscribed power in kVA (default: 9)")
    ap.add_argument("--hc-start", type=int, default=22, help="HC start hour (default: 22)")
    ap.add_argument("--hc-end", type=int, default=6, help="HC end hour (default: 6)")
    ap.add_argument("--years", type=int, default=5, help="Years of history (default: 5)")
    ap.add_argument("--hc-ratio", type=float, default=0.26,
                    help="HC share of consumption for daily mode (default: 0.26 from invoice data)")
    ap.add_argument("--quick", action="store_true", help="Daily data only (faster, less accurate)")
    ap.add_argument("--no-cache", action="store_true", help="Ignore cached data")
    return ap.parse_args()


def main():
    args = parse_args()

    # ── config: read LINKY_TOKEN + LINKY_PRM from project .env ──
    config = load_config()
    kva = args.kva
    hc_s = args.hc_start
    hc_e = args.hc_end
    hc_ratio = args.hc_ratio
    rte_id = config.get("rte_client_id", "")
    rte_sec = config.get("rte_client_secret", "")
    use_cache = not args.no_cache

    print()
    print("  ╔═══════════════════════════════════════════════════╗")
    print("  ║   BEST PRICE — EDF Electricity Offer Comparator  ║")
    print("  ╚═══════════════════════════════════════════════════╝")
    print(f"    Power: {kva} kVA   HC: {hc_s}h–{hc_e}h")
    print(f"    Credentials: {PROJECT_ENV}")

    breakdown = None
    dtype = "load_curve"

    # ── CSV mode ──
    if args.csv:
        print(f"\n  Loading CSV: {args.csv}")
        readings, dtype = load_csv(args.csv)
        if not readings:
            sys.exit("No data in CSV")
        if dtype == "load_curve":
            dates = [_parse_dt(r["date"]).date() for r in readings if _parse_dt(r["date"])]
            sd, ed = min(dates), max(dates)
            print("  Loading Tempo colours...")
            tempo = load_tempo(sd, ed, rte_id, rte_sec)
            eco = estimate_ecowatt(sd, ed)
            breakdown = analyze_curve(readings, hc_s, hc_e, tempo, eco)
        else:
            dates = [_parse_dt(r["date"]).date() for r in readings if _parse_dt(r["date"])]
            sd, ed = min(dates), max(dates)
            tempo = load_tempo(sd, ed, rte_id, rte_sec)
            eco = estimate_ecowatt(sd, ed)
            breakdown = analyze_daily(readings, hc_ratio=hc_ratio, tempo=tempo, ecowatt=eco)
            hc_pct = int(hc_ratio * 100)
            print(f"\n    Note: daily data only — HC/HP split estimated at {hc_pct}/{100-hc_pct}%.")

    # ── API mode ──
    else:
        if not HAS_REQUESTS:
            sys.exit("Install requests: pip install requests")
        token = config.get("token", "")
        prm = config.get("prm", "")
        if not token or not prm:
            sys.exit(f"LINKY_TOKEN and LINKY_PRM not found in {PROJECT_ENV}")

        print(f"    PRM: {prm[:4]}...{prm[-4:]}   Source: conso.boris.sh")
        end_d = date.today() - timedelta(days=1)
        start_d = end_d - timedelta(days=args.years * 365)

        if args.quick:
            print(f"\n  Fetching daily data ({args.years} years)...")
            daily = fetch_daily(token, prm, start_d, end_d, use_cache)
            if not daily:
                sys.exit("No daily data from API")
            dates = [_parse_dt(r["date"]).date() for r in daily if _parse_dt(r["date"])]
            sd, ed = min(dates), max(dates)
            print("  Loading Tempo colours...")
            tempo = load_tempo(sd, ed, rte_id, rte_sec)
            eco = estimate_ecowatt(sd, ed)
            breakdown = analyze_daily(daily, hc_ratio=hc_ratio, tempo=tempo, ecowatt=eco)
            dtype = "daily"
            hc_pct = int(hc_ratio * 100)
            print(f"    Note: daily data only — HC/HP split estimated at {hc_pct}/{100-hc_pct}%.")
        else:
            print(f"\n  Fetching half-hourly data ({args.years} years)...")
            print("    This may take a few minutes (API rate limits)...\n")
            readings = fetch_load_curve(token, prm, start_d, end_d, use_cache)

            if not readings:
                print("    No half-hourly data, falling back to daily...")
                daily = fetch_daily(token, prm, start_d, end_d, use_cache)
                if not daily:
                    sys.exit("No data from API")
                dates = [_parse_dt(r["date"]).date() for r in daily if _parse_dt(r["date"])]
                sd, ed = min(dates), max(dates)
                tempo = load_tempo(sd, ed, rte_id, rte_sec)
                eco = estimate_ecowatt(sd, ed)
                breakdown = analyze_daily(daily, hc_ratio=hc_ratio, tempo=tempo, ecowatt=eco)
                dtype = "daily"
            else:
                dates = [_parse_dt(r["date"]).date() for r in readings if _parse_dt(r["date"])]
                if not dates:
                    sys.exit("Could not parse dates from API data")
                sd, ed = min(dates), max(dates)
                print("  Loading Tempo colours...")
                tempo = load_tempo(sd, ed, rte_id, rte_sec)
                print("  Estimating Ecowatt signals...")
                eco = estimate_ecowatt(sd, ed)
                breakdown = analyze_curve(readings, hc_s, hc_e, tempo, eco)

    if breakdown is None or breakdown.total == 0:
        sys.exit("No consumption data to analyse")

    # ── results ──
    show_summary(breakdown, dtype)

    print(f"\n  Calculating {len(OFFERS)} offers...")
    results = [r for o in OFFERS if (r := calc_cost(o, breakdown, kva)) is not None]
    if not results:
        sys.exit(f"No offers available for {kva} kVA")

    show_results(results, kva)

    # ── Invoice comparison ──
    show_invoice_comparison(results)

    if dtype == "daily":
        hc_pct = int(args.hc_ratio * 100)
        print(f"\n  ⚠  HC/Tempo/Weekend/Flex results are ESTIMATES (daily data only, {hc_pct}/{100-hc_pct}% HC/HP).")
        print("     HC ratio based on historical invoice data (EDF 2022-23).")
        print("     For exact results, use half-hourly data (courbe de charge).")
    print()


if __name__ == "__main__":
    main()

"""
Airzone Best Price — EDF Tariff Comparison
============================================
Importable module for comparing EDF electricity offers using Linky data.
Extracted from Best Price/best_price.py for integration into the Airzone app.

Usage:
    from airzone_best_price import BestPriceAnalyzer
    analyzer = BestPriceAnalyzer(token, prm, kva=9, hc_schedule="22-6")
    result = analyzer.run_analysis(days=365)
    # result = {"offers": [...], "current_kwh": ..., "cheapest": ...}

No UI dependencies — usable by macOS app, Pi daemon, or CLI.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

log = logging.getLogger("airzone")

# ── Price Data — All EDF offers, TTC, Feb 2026 ──────────────────────────────

_SUB_REG_BASE = {3: 12.03, 6: 15.65, 9: 19.56, 12: 23.32, 15: 26.84,
                 18: 30.49, 24: 38.24, 30: 45.37, 36: 52.54}
_SUB_REG_HC = {6: 15.65, 9: 19.56, 12: 23.32, 15: 26.84, 18: 30.49,
               24: 38.24, 30: 45.37, 36: 52.54}
_SUB_REG_TEMP = {6: 15.59, 9: 19.38, 12: 23.07, 15: 26.47, 18: 30.04,
                 30: 44.73, 36: 52.42}
_SUB_MKT_BASE = {3: 11.25, 6: 14.78, 9: 18.49, 12: 22.21, 15: 25.74,
                 18: 29.23, 24: 36.84, 30: 44.07, 36: 51.50}
_SUB_MKT_HC = {6: 15.05, 9: 18.91, 12: 22.65, 15: 26.17, 18: 29.81,
               24: 37.52, 30: 44.65, 36: 51.82}

OFFERS = [
    {"name": "Tarif Bleu - Base", "sub": _SUB_REG_BASE, "type": "base",
     "p": {"base_kva": {3: 19.40, 6: 19.40, 9: 19.27, 12: 19.27,
                        15: 19.27, 18: 19.27, 24: 19.27, 30: 19.27, 36: 19.27}}},
    {"name": "Tarif Bleu - Heures Creuses", "sub": _SUB_REG_HC, "type": "hc",
     "p": {"hp": 20.65, "hc": 15.79}},
    {"name": "Tarif Bleu - Tempo", "sub": _SUB_REG_TEMP, "type": "tempo",
     "p": {"bleu_hc": 13.25, "bleu_hp": 16.12, "blanc_hc": 14.99,
            "blanc_hp": 18.71, "rouge_hc": 15.75, "rouge_hp": 70.60}},
    {"name": "Zen Fixe - Base", "sub": _SUB_MKT_BASE, "type": "base",
     "p": {"base": 17.74}},
    {"name": "Zen Fixe - Heures Creuses", "sub": _SUB_MKT_HC, "type": "hc",
     "p": {"hp": 18.88, "hc": 14.96}},
    {"name": "Vert Électrique - Base", "sub": _SUB_MKT_BASE, "type": "base",
     "p": {"base": 18.81}},
    {"name": "Vert Électrique - HC", "sub": _SUB_MKT_HC, "type": "hc",
     "p": {"hp": 20.21, "hc": 15.76}},
    {"name": "Zen Online - Base", "sub": _SUB_REG_BASE, "type": "base",
     "p": {"base_kva": {3: 18.45, 6: 18.45, 9: 18.33, 12: 18.33,
                        15: 18.33, 18: 18.33, 24: 18.33, 30: 18.33, 36: 18.33}}},
    {"name": "Zen Online - Heures Creuses", "sub": _SUB_REG_HC, "type": "hc",
     "p": {"hp": 19.63, "hc": 15.05}},
    {"name": "Zen Week-End - WE", "sub": _SUB_MKT_BASE, "type": "we",
     "p": {"sem": 20.38, "we": 15.38}},
    {"name": "Zen Week-End - HC+WE", "sub": _SUB_MKT_HC, "type": "hcwe",
     "p": {"hp_s": 21.53, "hc_s": 16.18, "hp_w": 16.18, "hc_w": 16.18}},
    {"name": "Vert Élec. Auto - HC", "sub": _SUB_MKT_HC, "type": "hc",
     "p": {"hp": 22.29, "hc": 13.00}},
]


# ── Helpers ──────────────────────────────────────────────────────────────────

API_BASE = "https://conso.boris.sh/api"


def _is_hc(hour: int, hc_start: int, hc_end: int) -> bool:
    """Check if hour falls within Heures Creuses window."""
    if hc_start > hc_end:
        return hour >= hc_start or hour < hc_end
    return hc_start <= hour < hc_end


def _parse_hc_schedule(hc_str: str) -> tuple[int, int]:
    """Parse '22-6' format to (22, 6)."""
    parts = hc_str.split("-")
    return int(parts[0]), int(parts[1])


def _annualize(val: float, days: int) -> float:
    """Annualize a value from N days of data."""
    return val * 365.25 / days if days > 0 else 0.0


def _estimate_tempo_color(d: date) -> str:
    """Estimate Tempo day color based on month."""
    probs = {1: (.20, .15), 2: (.18, .12), 3: (.12, .05), 4: (.05, 0),
             5: (.02, 0), 6: (0, 0), 7: (0, 0), 8: (0, 0), 9: (0, 0),
             10: (.05, 0), 11: (.15, .08), 12: (.20, .18)}
    bp, rp = probs[d.month]
    if d.weekday() == 6:
        rp = 0
    h = (d.toordinal() * 31 + d.month * 7) % 100
    if h < rp * 100:
        return "rouge"
    if h < (rp + bp) * 100:
        return "blanc"
    return "bleu"


# ── Consumption Breakdown ────────────────────────────────────────────────────

class ConsumptionBreakdown:
    """Breakdown of energy consumption by time-of-use."""

    def __init__(self):
        self.total_kwh = 0.0
        self.hp_kwh = 0.0
        self.hc_kwh = 0.0
        self.weekday_kwh = 0.0
        self.weekend_kwh = 0.0
        self.tempo: dict[str, float] = defaultdict(float)
        self.n_days = 0
        self.start_date: Optional[date] = None
        self.end_date: Optional[date] = None


# ── BestPriceAnalyzer ────────────────────────────────────────────────────────

class BestPriceAnalyzer:
    """
    Analyze Linky consumption data and compare EDF electricity offers.

    Can use data from:
    - Live API (token + prm)
    - Local SQLite linky_readings table
    """

    def __init__(self, token: str = "", prm: str = "",
                 kva: int = 9, hc_schedule: str = "22-6"):
        self.token = token
        self.prm = prm
        self.kva = kva
        self.hc_start, self.hc_end = _parse_hc_schedule(hc_schedule)

    def run_analysis(self, days: int = 365,
                     conn=None) -> dict:
        """
        Run tariff comparison.

        Args:
            days: Number of days of history to analyze
            conn: Optional SQLite connection with linky_readings table.
                  If provided, uses local data instead of API.

        Returns dict with:
            offers: list of {name, annual_cost, savings_vs_current}
            breakdown: {total_kwh, hp_kwh, hc_kwh, ...}
            cheapest: name of cheapest offer
        """
        # Get consumption data
        if conn:
            breakdown = self._analyze_from_db(conn, days)
        elif self.token and self.prm:
            breakdown = self._analyze_from_api(days)
        else:
            return {"error": "No data source — provide token+prm or DB connection"}

        if breakdown.total_kwh == 0:
            return {"error": "No consumption data found",
                    "offers": [], "breakdown": {}}

        # Calculate cost for each offer
        results = []
        for offer in OFFERS:
            cost = self._calc_cost(offer, breakdown)
            if cost is not None:
                results.append(cost)

        # Sort by annual cost
        results.sort(key=lambda r: r["annual_cost"])

        # Calculate savings vs most expensive (or current)
        if results:
            cheapest_cost = results[0]["annual_cost"]
            for r in results:
                r["savings_vs_cheapest"] = round(r["annual_cost"] - cheapest_cost, 2)

        return {
            "offers": results,
            "breakdown": {
                "total_kwh": round(breakdown.total_kwh, 1),
                "annual_kwh": round(_annualize(breakdown.total_kwh, breakdown.n_days), 0),
                "hp_kwh": round(breakdown.hp_kwh, 1),
                "hc_kwh": round(breakdown.hc_kwh, 1),
                "hp_pct": round(breakdown.hp_kwh / breakdown.total_kwh * 100, 1)
                if breakdown.total_kwh > 0 else 0,
                "hc_pct": round(breakdown.hc_kwh / breakdown.total_kwh * 100, 1)
                if breakdown.total_kwh > 0 else 0,
                "weekday_kwh": round(breakdown.weekday_kwh, 1),
                "weekend_kwh": round(breakdown.weekend_kwh, 1),
                "data_days": breakdown.n_days,
                "start_date": str(breakdown.start_date) if breakdown.start_date else None,
                "end_date": str(breakdown.end_date) if breakdown.end_date else None,
            },
            "cheapest": results[0]["name"] if results else None,
        }

    def _analyze_from_db(self, conn, days: int) -> ConsumptionBreakdown:
        """Analyze from local SQLite linky_readings table."""
        cutoff = str(date.today() - timedelta(days=days))
        rows = conn.execute(
            "SELECT timestamp, wh FROM linky_readings "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (cutoff,)
        ).fetchall()

        b = ConsumptionBreakdown()
        dates_seen = set()

        for ts_raw, wh in rows:
            ts = str(ts_raw)
            try:
                if "T" in ts:
                    dt = datetime.fromisoformat(ts.replace("Z", ""))
                elif " " in ts:
                    dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                else:
                    continue
            except (ValueError, IndexError):
                continue

            kwh = wh / 1000.0
            d = dt.date()
            h = dt.hour
            is_weekend = dt.weekday() >= 5
            is_hc = _is_hc(h, self.hc_start, self.hc_end)

            b.total_kwh += kwh
            dates_seen.add(d)

            if is_hc:
                b.hc_kwh += kwh
            else:
                b.hp_kwh += kwh

            if is_weekend:
                b.weekend_kwh += kwh
            else:
                b.weekday_kwh += kwh

            # Tempo breakdown
            tempo_color = _estimate_tempo_color(d)
            b.tempo[f"{tempo_color}_{'hc' if is_hc else 'hp'}"] += kwh

        if dates_seen:
            b.start_date = min(dates_seen)
            b.end_date = max(dates_seen)
            b.n_days = (b.end_date - b.start_date).days + 1

        return b

    def _analyze_from_api(self, days: int) -> ConsumptionBreakdown:
        """Fetch and analyze from Conso API."""
        if not _HAS_REQUESTS:
            return ConsumptionBreakdown()

        b = ConsumptionBreakdown()
        end_d = date.today()
        start_d = end_d - timedelta(days=days)
        dates_seen = set()

        # Fetch in 7-day chunks
        cur = start_d
        while cur < end_d:
            nxt = min(cur + timedelta(days=7), end_d)
            try:
                resp = requests.get(
                    f"{API_BASE}/consumption_load_curve",
                    headers={"Authorization": f"Bearer {self.token}",
                             "User-Agent": "airzone/1.0"},
                    params={"prm": self.prm, "start": str(cur), "end": str(nxt)},
                    timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    intervals = (data.get("meter_reading", {})
                                 .get("interval_reading", []))
                    if not intervals:
                        intervals = data.get("interval_reading", [])
                    for iv in intervals:
                        try:
                            dt = datetime.fromisoformat(
                                iv["date"].replace("Z", ""))
                            watts = float(iv["value"])
                            kwh = watts * 0.5 / 1000.0
                            d = dt.date()
                            h = dt.hour
                            dates_seen.add(d)

                            b.total_kwh += kwh
                            if _is_hc(h, self.hc_start, self.hc_end):
                                b.hc_kwh += kwh
                            else:
                                b.hp_kwh += kwh
                            if dt.weekday() >= 5:
                                b.weekend_kwh += kwh
                            else:
                                b.weekday_kwh += kwh

                            tc = _estimate_tempo_color(d)
                            is_hc = _is_hc(h, self.hc_start, self.hc_end)
                            b.tempo[f"{tc}_{'hc' if is_hc else 'hp'}"] += kwh
                        except (ValueError, KeyError):
                            continue
            except Exception as e:
                log.warning("Best Price API error: %s", e)

            cur = nxt
            time.sleep(0.5)

        if dates_seen:
            b.start_date = min(dates_seen)
            b.end_date = max(dates_seen)
            b.n_days = (b.end_date - b.start_date).days + 1

        return b

    def _calc_cost(self, offer: dict,
                   b: ConsumptionBreakdown) -> Optional[dict]:
        """Calculate annual cost for one offer."""
        sub_tbl = offer["sub"]
        if self.kva not in sub_tbl:
            return None

        monthly_sub = sub_tbl[self.kva]
        annual_sub = monthly_sub * 12
        p = offer["p"]
        t = offer["type"]
        nd = b.n_days

        if t == "base":
            if "base_kva" in p:
                pr = p["base_kva"].get(self.kva)
                if pr is None:
                    return None
            else:
                pr = p["base"]
            energy = _annualize(b.total_kwh, nd) * pr / 100

        elif t == "hc":
            energy = (_annualize(b.hp_kwh, nd) * p["hp"] +
                      _annualize(b.hc_kwh, nd) * p["hc"]) / 100

        elif t == "tempo":
            energy = 0
            for key in ("bleu_hp", "bleu_hc", "blanc_hp", "blanc_hc",
                        "rouge_hp", "rouge_hc"):
                energy += _annualize(b.tempo.get(key, 0), nd) * p.get(key, 0) / 100

        elif t == "we":
            energy = (_annualize(b.weekday_kwh, nd) * p["sem"] +
                      _annualize(b.weekend_kwh, nd) * p["we"]) / 100

        elif t == "hcwe":
            # Simplified: weekday HP/HC + weekend flat
            energy = (_annualize(b.hp_kwh - b.weekend_kwh * 0.5, nd) * p.get("hp_s", 0) +
                      _annualize(b.hc_kwh - b.weekend_kwh * 0.5, nd) * p.get("hc_s", 0) +
                      _annualize(b.weekend_kwh, nd) * p.get("hp_w", 0)) / 100

        else:
            return None

        annual_total = round(annual_sub + energy, 2)
        monthly_total = round(annual_total / 12, 2)

        return {
            "name": offer["name"],
            "type": t,
            "annual_sub": round(annual_sub, 2),
            "annual_energy": round(energy, 2),
            "annual_cost": annual_total,
            "monthly_cost": monthly_total,
        }


# ── Convenience function ─────────────────────────────────────────────────────

def run_best_price_analysis(token: str = "", prm: str = "",
                            kva: int = 9, hc_schedule: str = "22-6",
                            days: int = 365, conn=None) -> dict:
    """
    Convenience function for quick tariff comparison.

    Can use either API credentials or a local SQLite connection.
    """
    analyzer = BestPriceAnalyzer(token, prm, kva, hc_schedule)
    return analyzer.run_analysis(days=days, conn=conn)

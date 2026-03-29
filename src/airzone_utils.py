"""
Airzone Shared Utilities
========================
Common helper functions used across multiple Airzone modules.
"""
from __future__ import annotations

import math


def calc_dewpoint(temp_c: float, rh: float) -> float:
    """Magnus-Tetens dew point from temperature (°C) and RH (%).

    Returns dew point in °C rounded to 1 decimal place.
    Returns 0.0 for invalid inputs (None temp, non-positive RH).
    """
    if temp_c is None or rh is None or rh <= 0:
        return 0.0
    a, b = 17.625, 243.04
    gamma = (a * temp_c) / (b + temp_c) + math.log(max(rh, 1) / 100)
    return round((b * gamma) / (a - gamma), 1)

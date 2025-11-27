"""
Regime detection logic used by the signal engine.
"""


def detect_regime(sigma_short: float, sigma_avg: float, velocity: float, acceleration: float) -> str:
    if sigma_avg <= 0:
        return "NORMAL"

    vol_ratio = sigma_short / sigma_avg
    if vol_ratio > 1.8:
        return "HIGH_CHOP"

    if acceleration > 0 and velocity > 0:
        return "UP_TREND"

    if acceleration < 0 and velocity < 0:
        return "DOWN_TREND"

    return "NORMAL"


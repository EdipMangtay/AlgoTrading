"""
Mathematical helpers used by the Ultimate Reversal Engine.
"""

from __future__ import annotations

from typing import Iterable, Sequence, Tuple

import numpy as np

from .config import SETTINGS


class UltimateMath:
    """Collection of static helper methods."""

    @staticmethod
    def ema(prices: Sequence[float], alpha: float | None = None) -> np.ndarray:
        alpha = alpha or SETTINGS["EMA_ALPHA"]
        prices_arr = np.array(prices, dtype=float)
        ema = np.zeros_like(prices_arr)
        ema[0] = prices_arr[0]
        for idx in range(1, len(prices_arr)):
            ema[idx] = alpha * prices_arr[idx] + (1 - alpha) * ema[idx - 1]
        return ema

    @staticmethod
    def get_kinematics(prices: Sequence[float]) -> Tuple[float, float]:
        arr = np.array(prices, dtype=float)
        velocity = np.diff(arr)
        acceleration = np.diff(velocity)
        v_curr = float(velocity[-1]) if len(velocity) > 0 else 0.0
        a_curr = float(acceleration[-1]) if len(acceleration) > 0 else 0.0
        return v_curr, a_curr

    @staticmethod
    def rolling_vwap(prices: Iterable[float], volumes: Iterable[float]) -> float:
        p = np.array(prices, dtype=float)
        v = np.array(volumes, dtype=float)
        pv = np.sum(p * v)
        vv = np.sum(v)
        if vv == 0:
            return float(p[-1])
        return float(pv / vv)

    @staticmethod
    def z_score(window_prices: Sequence[float], current_price: float):
        arr = np.array(window_prices, dtype=float)
        mean = float(arr.mean())
        std = float(arr.std())
        if std == 0:
            std = max(abs(mean) * 1e-4, 1e-6)
        z_val = (current_price - mean) / std
        return z_val, mean, std

    @staticmethod
    def get_sigma(window_prices: Sequence[float]):
        arr = np.array(window_prices, dtype=float)
        mean = float(arr.mean())
        std = float(arr.std())
        if std == 0:
            std = max(abs(mean) * 1e-4, 1e-6)
        return mean, std

    @staticmethod
    def get_dynamic_reversal_threshold(move_pct: float) -> float:
        abs_move = abs(move_pct)
        for limit, threshold in SETTINGS["DYNAMIC_THRESHOLDS"]:
            if abs_move >= limit:
                return max(threshold, SETTINGS["REVERSAL_CONFIRM_PCT_MIN"])
        return 999.0

    @staticmethod
    def poly_curve_signal(prices_tail: Sequence[float], side: str):
        poly_window = SETTINGS["POLY_WINDOW"]
        if len(prices_tail) < poly_window:
            return False, 0.0, 0.0, 0.0

        y = np.array(prices_tail[-poly_window:], dtype=float)
        x = np.arange(len(y))
        try:
            coeffs = np.polyfit(x, y, 2)
        except Exception:
            return False, 0.0, 0.0, 0.0

        a, b, _ = coeffs
        curvature = a
        slope = b
        current_slope = 2 * a * (len(x) - 1) + b
        min_curvature = SETTINGS["MIN_CURVATURE"]

        if side == "SHORT":
            broken = (curvature < -min_curvature) and (current_slope < 0)
        else:
            broken = (curvature > min_curvature) and (current_slope > 0)

        return broken, curvature, slope, current_slope


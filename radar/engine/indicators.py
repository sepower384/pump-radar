"""순수 파이썬 지표 (numpy/pandas 불필요)."""
from __future__ import annotations

import math


def ema(vals: list[float], period: int) -> list[float]:
    if not vals:
        return []
    k = 2 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(vals: list[float], period: int) -> float:
    if not vals:
        return 0.0
    w = vals[-period:]
    return sum(w) / len(w)


def stdev(vals: list[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


def linreg_slope(vals: list[float]) -> float:
    """단위: 봉당 평균 변화량. 로그값을 넣으면 봉당 복리수익률이 된다."""
    n = len(vals)
    if n < 3:
        return 0.0
    xm = (n - 1) / 2
    ym = sum(vals) / n
    num = sum((i - xm) * (v - ym) for i, v in enumerate(vals))
    den = sum((i - xm) ** 2 for i in range(n))
    return num / den if den else 0.0


def r_squared(vals: list[float]) -> float:
    """추세의 '깔끔함'. 1에 가까울수록 계단식 우상향."""
    n = len(vals)
    if n < 3:
        return 0.0
    slope = linreg_slope(vals)
    xm = (n - 1) / 2
    ym = sum(vals) / n
    intercept = ym - slope * xm
    ss_res = sum((v - (slope * i + intercept)) ** 2 for i, v in enumerate(vals))
    ss_tot = sum((v - ym) ** 2 for v in vals)
    return max(0.0, 1 - ss_res / ss_tot) if ss_tot else 0.0


def max_drawdown(vals: list[float]) -> float:
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def scale(x: float, lo: float, hi: float) -> float:
    """lo→0, hi→1 선형 정규화 후 0~1로 자름."""
    if hi == lo:
        return 0.0
    return clamp01((x - lo) / (hi - lo))


def zscore(x: float, series: list[float]) -> float:
    s = stdev(series)
    if s == 0:
        return 0.0
    return (x - sum(series) / len(series)) / s

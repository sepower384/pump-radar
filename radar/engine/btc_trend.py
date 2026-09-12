"""BTC 페어 우상향 발굴.

핵심: USDT 가격이 아니라 **코인/BTC 비율 차트**가 우상향인지를 본다.
(비트코인보다 강한 코인 = 알트 순환매의 선두)
"""
from __future__ import annotations

from ..sources import binance
from .indicators import (clamp01, ema, linreg_slope, max_drawdown, pct,
                         r_squared, scale)
import math

BARS_PER_DAY = {"1h": 24, "2h": 12, "4h": 6, "6h": 4, "12h": 2, "1d": 1}


def ratio_series(coin_closes: list[float], btc_closes: list[float]) -> list[float]:
    n = min(len(coin_closes), len(btc_closes))
    if n == 0:
        return []
    c, b = coin_closes[-n:], btc_closes[-n:]
    return [x / y for x, y in zip(c, b) if y > 0]


def score_series(ratio: list[float], interval: str = "4h") -> dict | None:
    """코인/BTC 비율 시계열 → 0~100 우상향 점수 + 근거."""
    bpd = BARS_PER_DAY.get(interval, 6)
    need = bpd * 10
    if len(ratio) < need:
        return None

    e20, e50 = ema(ratio, 20), ema(ratio, 50)
    last = ratio[-1]

    d7 = min(len(ratio) - 1, bpd * 7)
    d30 = min(len(ratio) - 1, bpd * 30)
    rs7 = pct(last, ratio[-1 - d7])
    rs30 = pct(last, ratio[-1 - d30])

    win = ratio[-(bpd * 7):]
    logs = [math.log(v) for v in win if v > 0]
    slope_per_bar = linreg_slope(logs)
    slope_daily_pct = (math.exp(slope_per_bar * bpd) - 1) * 100
    fit = r_squared(logs)

    hi_win = ratio[-(bpd * 30):]
    hi = max(hi_win)
    near_high = last / hi if hi else 0.0

    above = sum(1 for v, e in zip(ratio[-(bpd * 7):], e20[-(bpd * 7):]) if v > e) / max(1, len(win))
    stack = 1.0 if (e20[-1] > e50[-1]) else 0.0
    mdd = max_drawdown(ratio[-(bpd * 14):])

    # ── 점수 구성 (합 100) ──
    s_rs7 = scale(rs7, 0, 25) * 20            # 최근 7일 BTC 대비 초과수익
    s_rs30 = scale(rs30, -5, 60) * 15         # 한 달 추세
    s_slope = scale(slope_daily_pct, 0, 3.5) * 20   # 일평균 기울기
    s_fit = fit * 15                          # 추세의 깔끔함(계단식 우상향)
    s_high = scale(near_high, 0.80, 1.0) * 15  # 30일 고점 근접
    s_above = above * 8                       # EMA20 위에 머문 비율
    s_stack = stack * 7                       # 정배열
    penalty = scale(-mdd, 0.25, 0.6) * 15     # 변동성 과대 감점

    total = s_rs7 + s_rs30 + s_slope + s_fit + s_high + s_above + s_stack - penalty
    total = round(max(0.0, min(100.0, total)), 1)

    return {
        "score": total,
        "rs7": round(rs7, 1),
        "rs30": round(rs30, 1),
        "slope_daily_pct": round(slope_daily_pct, 2),
        "fit": round(fit, 2),
        "near_high": round(near_high, 3),
        "above_ema20_ratio": round(above, 2),
        "ema_stacked": bool(stack),
        "mdd14": round(mdd * 100, 1),
        "parts": {
            "rs7": round(s_rs7, 1), "rs30": round(s_rs30, 1), "slope": round(s_slope, 1),
            "fit": round(s_fit, 1), "high": round(s_high, 1), "above": round(s_above, 1),
            "stack": round(s_stack, 1), "penalty": round(-penalty, 1),
        },
    }


def scan(cfg: dict, workers: int = 8) -> list[dict]:
    """상위 유동성 USDT 페어 전체를 BTC 기준으로 채점해 정렬 반환."""
    interval = cfg.get("interval", "4h")
    bars = int(cfg.get("bars", 180))
    min_qvol = float(cfg.get("min_quote_volume_usdt", 3_000_000))
    top_n = int(cfg.get("universe_top_n", 250))

    tickers = binance.ticker_24hr()
    uni = binance.usdt_universe(tickers, min_qvol)
    uni.sort(key=lambda t: t["_qvol"], reverse=True)
    uni = [t for t in uni if t["_base"] != "BTC"][:top_n]

    btc_kl = binance.klines("BTCUSDT", interval, bars)
    btc_closes = binance.closes(btc_kl)

    symbols = [t["symbol"] for t in uni]
    kl_map = binance.klines_many(symbols, interval, bars, workers=workers)

    by_symbol = {t["symbol"]: t for t in uni}
    rows: list[dict] = []
    for sym, kl in kl_map.items():
        r = score_series(ratio_series(binance.closes(kl), btc_closes), interval)
        if not r:
            continue
        t = by_symbol[sym]
        r.update({
            "symbol": sym, "base": t["_base"], "price": t["_last"],
            "qvol": t["_qvol"], "chg24": t["_chg24"],
        })
        rows.append(r)

    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows

"""코인 급등/급락 감지.

두 층으로 본다.
 1) 스냅샷 층: 매 사이클 전체 시세를 저장해 5분/15분/1시간 변동률을 싸게 계산.
 2) 확인 층: 후보만 5분봉을 받아 '거래량이 실제로 터졌는지'를 검증(가짜 급등 제거).
"""
from __future__ import annotations

import time

from ..sources import binance, bitget
from .. import store
from .indicators import sma, stdev


def _chg(now: float, then: float) -> float:
    return (now / then - 1) * 100 if then else 0.0


def collect_and_detect(con, cfg: dict, workers: int = 8) -> list[dict]:
    min_qvol = float(cfg.get("min_quote_volume_usdt", 800_000))
    th5 = float(cfg.get("chg_5m_pct", 3.0))
    th15 = float(cfg.get("chg_15m_pct", 5.0))
    th1h = float(cfg.get("chg_1h_pct", 8.0))
    surge_x = float(cfg.get("volume_surge_x", 3.0))
    pool = int(cfg.get("candidate_pool", 40))
    include_dumps = bool(cfg.get("include_dumps", True))
    dump_pct = float(cfg.get("dump_pct", -8.0))

    tickers = binance.ticker_24hr()
    uni = binance.usdt_universe(tickers, min_qvol)
    now_map = {t["symbol"]: (t["_last"], t["_qvol"]) for t in uni}

    prev5 = store.price_at(con, "binance", 5, tolerance_min=6)
    prev15 = store.price_at(con, "binance", 15, tolerance_min=10)
    prev60 = store.price_at(con, "binance", 60, tolerance_min=20)
    store.save_snapshot(con, "binance", [(s, p, q) for s, (p, q) in now_map.items()])

    cands: list[dict] = []
    for t in uni:
        s, last = t["symbol"], t["_last"]
        c5 = _chg(last, prev5.get(s, (0, 0))[0]) if s in prev5 else 0.0
        c15 = _chg(last, prev15.get(s, (0, 0))[0]) if s in prev15 else 0.0
        c60 = _chg(last, prev60.get(s, (0, 0))[0]) if s in prev60 else 0.0
        up = (c5 >= th5) or (c15 >= th15) or (c60 >= th1h)
        down = include_dumps and ((c5 <= dump_pct) or (c15 <= dump_pct * 1.5))
        # 스냅샷이 아직 없는 첫 실행이면 24h 변동률로 대체 판정
        cold_start = not prev15 and t["_chg24"] >= float(cfg.get("chg_24h_pct_newcoin", 20.0))
        if not (up or down or cold_start):
            continue
        cands.append({
            "market": "binance", "symbol": s, "base": t["_base"], "price": last,
            "chg5m": round(c5, 2), "chg15m": round(c15, 2), "chg1h": round(c60, 2),
            "chg24h": round(t["_chg24"], 2), "qvol": t["_qvol"],
            "direction": "down" if down and not up else "up",
            "cold_start": cold_start,
        })

    cands.sort(key=lambda c: max(abs(c["chg5m"]), abs(c["chg15m"]), abs(c["chg1h"]),
                                 abs(c["chg24h"]) / 4), reverse=True)
    cands = cands[:pool]

    # ── 거래량 서지 검증 ──
    kl_map = binance.klines_many([c["symbol"] for c in cands], "5m", 100, workers=workers)
    out: list[dict] = []
    for c in cands:
        kl = kl_map.get(c["symbol"])
        if not kl or len(kl) < 30:
            c["vol_x"] = 0.0
            out.append(c)
            continue
        qv = binance.quote_volumes(kl)
        recent = sum(qv[-3:]) / 3
        base = sma(qv[:-3], 60) or 1e-9
        c["vol_x"] = round(recent / base, 1)
        c["vol_sigma"] = round((recent - base) / (stdev(qv[:-3]) or 1e-9), 1)
        c["bar_high"] = max(float(r[2]) for r in kl[-12:])
        c["bar_low"] = min(float(r[3]) for r in kl[-12:])
        out.append(c)

    confirmed = [c for c in out
                 if c["vol_x"] >= surge_x or c["cold_start"] or abs(c["chg15m"]) >= th15 * 2]
    confirmed.sort(key=lambda c: (c["vol_x"] * 0.5 + abs(c["chg15m"]) + abs(c["chg5m"]) * 2),
                   reverse=True)
    return confirmed


def bitget_only(con, cfg: dict, binance_bases: set[str]) -> list[dict]:
    """바이낸스에 없는 코인(신규 밈코인 등) 중 급등한 것."""
    if not cfg.get("include_bitget_only", True):
        return []
    min_qvol = float(cfg.get("min_quote_volume_usdt", 800_000))
    th24 = float(cfg.get("chg_24h_pct_newcoin", 20.0))
    try:
        rows = bitget.normalized(min_qvol)
    except Exception:  # noqa: BLE001
        return []

    now_map = {r["symbol"]: (r["last"], r["qvol"]) for r in rows}
    prev15 = store.price_at(con, "bitget", 15, tolerance_min=10)
    store.save_snapshot(con, "bitget", [(s, p, q) for s, (p, q) in now_map.items()])

    out = []
    for r in rows:
        if r["base"] in binance_bases:
            continue
        c15 = _chg(r["last"], prev15.get(r["symbol"], (0, 0))[0]) if r["symbol"] in prev15 else 0.0
        if c15 < float(cfg.get("chg_15m_pct", 5.0)) and r["chg24"] < th24:
            continue
        out.append({
            "market": "bitget", "symbol": r["symbol"], "base": r["base"], "price": r["last"],
            "chg5m": 0.0, "chg15m": round(c15, 2), "chg1h": 0.0,
            "chg24h": round(r["chg24"], 2), "qvol": r["qvol"], "vol_x": 0.0,
            "direction": "up", "cold_start": False, "bitget_only": True,
        })
    out.sort(key=lambda c: max(c["chg15m"], c["chg24h"] / 3), reverse=True)
    return out[:5]

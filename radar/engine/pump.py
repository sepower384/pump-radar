"""코인 급등/급락 감지.

두 층으로 본다.
 1) 스냅샷 층: 매 사이클 전체 시세를 저장해 5분/15분/1시간 변동률을 싸게 계산.
 2) 확인 층: 후보만 5분봉을 받아 '거래량이 실제로 터졌는지'를 검증(가짜 급등 제거).
"""
from __future__ import annotations

import time

from ..sources import binance, bitget
from .. import store
from ..http import pmap
from .indicators import sma, stdev


def _chg(now: float, then: float) -> float:
    return (now / then - 1) * 100 if then else 0.0


def bar_stats(kl: list[list]) -> dict:
    """5분봉(바이낸스·비트겟 공통: [ts, o, h, l, c, ...], 오래된 것 → 최신)으로
    최근 1·2시간 변동률, 최근 고점 대비 위치, 거래량 배수를 구한다.
    클라우드는 2시간마다 돌아 5·15·60분 전 스냅샷이 없으므로 이 값이 '지금 움직이는지'의 근거다."""
    if not kl or len(kl) < 30:
        return {}
    cl = [float(r[4]) for r in kl]
    hi = [float(r[2]) for r in kl]
    qv = [float(r[7]) if len(r) > 7 else 0.0 for r in kl]
    last = cl[-1]
    base = sma(qv[:-3], 60) or 0.0
    recent = sum(qv[-3:]) / 3
    return {
        "chg1h_k": round(_chg(last, cl[-13]), 2),
        "chg2h": round(_chg(last, cl[-25]), 2),
        "off_high": round(_chg(last, max(hi)), 2),   # 최근 약 8시간 고점 대비(음수 = 고점에서 밀림)
        "vol_x": round(recent / base, 1) if base > 0 else 0.0,
        "vol_sigma": round((recent - base) / (stdev(qv[:-3]) or 1e-9), 1) if base > 0 else 0.0,
        "closes": cl[-48:],  # 텔레그램 차트용 최근 4시간
        "bar_high": max(hi[-12:]),
        "bar_low": min(float(r[3]) for r in kl[-12:]),
    }


def still_moving(c: dict, cfg: dict) -> bool:
    """24시간 급등만으로 잡힌 후보가 '지금도' 살아 있는지.
    하루 전에 오르고 이미 식은 코인이 2시간마다 반복해서 알림으로 나가던 문제를 막는다."""
    if c.get("chg2h") is None:
        return False  # 봉 데이터가 없으면 확인 불가 → 보내지 않는다
    recent = float(cfg.get("recent_2h_pct", 4.0))
    surge_x = float(cfg.get("volume_surge_x", 3.0))
    if c["off_high"] < float(cfg.get("max_off_high_pct", -15.0)):
        return False  # 고점에서 이미 크게 밀려 급등이 끝난 상태
    return c["chg2h"] >= recent or c["chg1h_k"] >= recent * 0.75 or c.get("vol_x", 0) >= surge_x


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
    confirmed: list[dict] = []
    for c in cands:
        st = bar_stats(kl_map.get(c["symbol"]) or [])
        c["vol_x"] = 0.0
        c.update(st)
        if st and not c["chg1h"]:
            c["chg1h"] = st["chg1h_k"]
        if c["cold_start"]:
            ok = still_moving(c, cfg)
        else:
            ok = c["vol_x"] >= surge_x or abs(c["chg15m"]) >= th15 * 2
        if ok:
            confirmed.append(c)
    confirmed.sort(key=lambda c: (c["vol_x"] * 0.5 + abs(c["chg15m"]) + abs(c["chg5m"]) * 2
                                  + abs(c.get("chg2h") or 0) * 0.5), reverse=True)
    return confirmed


def bitget_only(con, cfg: dict, binance_bases: set[str], workers: int = 8) -> list[dict]:
    """바이낸스에 없는 코인(신규 밈코인 등) 중 지금 급등 중인 것. 주식 토큰(rNVDA 등)은 뺀다."""
    if not cfg.get("include_bitget_only", True):
        return []
    min_qvol = float(cfg.get("min_quote_volume_usdt", 800_000))
    th24 = float(cfg.get("chg_24h_pct_newcoin", 20.0))
    th15 = float(cfg.get("chg_15m_pct", 5.0))
    try:
        rows = bitget.normalized(min_qvol)
    except Exception:  # noqa: BLE001
        return []
    skip = bitget.non_crypto_bases()

    now_map = {r["symbol"]: (r["last"], r["qvol"]) for r in rows}
    prev15 = store.price_at(con, "bitget", 15, tolerance_min=10)
    store.save_snapshot(con, "bitget", [(s, p, q) for s, (p, q) in now_map.items()])

    cands = []
    for r in rows:
        if r["base"] in binance_bases or r["base"] in skip:
            continue
        c15 = _chg(r["last"], prev15.get(r["symbol"], (0, 0))[0]) if r["symbol"] in prev15 else 0.0
        if c15 < th15 and r["chg24"] < th24:
            continue
        cands.append({
            "market": "bitget", "symbol": r["symbol"], "base": r["base"], "price": r["last"],
            "chg5m": 0.0, "chg15m": round(c15, 2), "chg1h": 0.0,
            "chg24h": round(r["chg24"], 2), "qvol": r["qvol"], "vol_x": 0.0,
            "direction": "up", "cold_start": c15 < th15, "bitget_only": True,
        })
    cands.sort(key=lambda c: max(c["chg15m"], c["chg24h"] / 3), reverse=True)
    cands = cands[:12]

    kls = pmap(lambda sym: bitget.candles(sym, "5min", 100), [c["symbol"] for c in cands], workers=workers)
    out = []
    for c, kl in zip(cands, kls):
        st = bar_stats(kl or [])
        c.update(st)
        if st:
            c["chg1h"] = st["chg1h_k"]
        if (not c["cold_start"] and st) or still_moving(c, cfg):
            out.append(c)
    return out[:5]

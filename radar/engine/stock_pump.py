"""주식 급등 감지 — 미국/한국."""
from __future__ import annotations

from ..sources import stocks


def us(cfg: dict) -> list[dict]:
    if not cfg.get("enabled", True):
        return []
    rows = stocks.us_movers(cfg.get("screeners", ["day_gainers"]), 50)
    min_chg = float(cfg.get("min_change_pct", 8.0))
    min_px = float(cfg.get("min_price", 1.0))
    min_vol = float(cfg.get("min_volume", 300_000))

    out = []
    for r in rows:
        chg = r["chg"]
        session = "정규장"
        if abs(r.get("pre_chg") or 0) > abs(chg):
            chg, session = r["pre_chg"], "프리마켓"
        elif abs(r.get("post_chg") or 0) > max(abs(chg), min_chg):
            chg, session = r["post_chg"], "애프터마켓"
        if chg < min_chg or r["price"] < min_px or r["volume"] < min_vol:
            continue
        r["chg_eff"] = round(chg, 2)
        r["session"] = session
        r["vol_x"] = round(r["volume"] / r["avg_volume"], 1) if r.get("avg_volume") else 0.0
        r["market"] = "US"
        r["url"] = f"https://finance.yahoo.com/quote/{r['symbol']}"
        out.append(r)

    out.sort(key=lambda r: (r["vol_x"] * 2 + r["chg_eff"]), reverse=True)
    return out[: int(cfg.get("max_alerts", 6)) * 2]


def kr(cfg: dict) -> list[dict]:
    if not cfg.get("enabled", True):
        return []
    try:
        rows = stocks.kr_movers(1)
    except Exception:  # noqa: BLE001
        return []
    min_chg = float(cfg.get("min_change_pct", 8.0))
    min_val = float(cfg.get("min_trade_value_eok", 50))

    out = [r for r in rows if r["chg"] >= min_chg and r["trade_value_eok"] >= min_val]
    out.sort(key=lambda r: (r["chg"], r["trade_value_eok"]), reverse=True)
    for r in out:
        r["chg_eff"] = r["chg"]
        r["session"] = "정규장"
    return out[: int(cfg.get("max_alerts", 6)) * 2]

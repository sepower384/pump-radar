"""비트겟 공개 API — 바이낸스 미상장 밈코인/신규코인 커버용."""
from __future__ import annotations

from ..http import get_json

BASE = "https://api.bitget.com"


def spot_tickers() -> list[dict]:
    d = get_json(f"{BASE}/api/v2/spot/market/tickers", timeout=25)
    return d.get("data") or []


def normalized(min_qvol: float = 500_000) -> list[dict]:
    """{symbol, base, last, chg24, qvol} 형태로 정리."""
    out = []
    for t in spot_tickers():
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            last = float(t.get("lastPr") or 0)
            qvol = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
            chg = float(t.get("change24h") or 0) * 100
        except ValueError:
            continue
        if last <= 0 or qvol < min_qvol:
            continue
        out.append({"symbol": s, "base": s[:-4], "last": last, "chg24": chg, "qvol": qvol})
    return out


def candles(symbol: str, granularity: str = "5min", limit: int = 60) -> list[list]:
    d = get_json(f"{BASE}/api/v2/spot/market/candles",
                 params={"symbol": symbol, "granularity": granularity, "limit": limit}, timeout=15)
    return d.get("data") or []

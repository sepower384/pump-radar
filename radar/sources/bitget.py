"""비트겟 공개 API — 바이낸스 미상장 밈코인/신규코인 커버용."""
from __future__ import annotations

import json
import re
import time

from ..config import DATA_DIR
from ..http import get_json

BASE = "https://api.bitget.com"
_COINS_CACHE = DATA_DIR / "bitget_coins.json"

# 코인이 아닌 상품: rNVDA·rGNRC(미국 주식 토큰), preSPCX(상장 전 주식), USDCx(스테이블 래핑) 등.
# 비트겟은 이런 상품의 coin 이름에 소문자를 섞는다(티커 목록에선 RGNRCUSDT 처럼 대문자라 구분 불가).
_NON_CRYPTO = re.compile(r"[a-z]")


def non_crypto_bases(max_age_h: float = 24) -> set[str]:
    """주식 토큰 등 '코인 아님' base 집합(대문자). 하루 캐시, 조회 실패 시 옛 캐시 → 빈 집합."""
    cached: dict = {}
    try:
        cached = json.loads(_COINS_CACHE.read_text(encoding="utf-8"))
        if time.time() - float(cached.get("ts", 0)) < max_age_h * 3600:
            return set(cached.get("bases") or [])
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = get_json(f"{BASE}/api/v2/spot/public/coins", timeout=30).get("data") or []
        bases = sorted({str(r.get("coin", "")).upper() for r in rows
                        if _NON_CRYPTO.search(str(r.get("coin", "")))})
        if len(rows) > 100:  # 응답이 비정상적으로 작으면 캐시를 덮어쓰지 않는다
            _COINS_CACHE.write_text(json.dumps({"ts": time.time(), "bases": bases}), encoding="utf-8")
        return set(bases)
    except Exception:  # noqa: BLE001
        return set(cached.get("bases") or [])


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
    """[ts, open, high, low, close, baseVol, usdtVol, quoteVol] 오래된 것 → 최신 순."""
    d = get_json(f"{BASE}/api/v2/spot/market/candles",
                 params={"symbol": symbol, "granularity": granularity, "limit": limit}, timeout=15)
    return sorted(d.get("data") or [], key=lambda r: int(r[0]))

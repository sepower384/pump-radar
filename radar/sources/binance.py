"""바이낸스 공개 API (키 불필요)."""
from __future__ import annotations

from ..http import get_json, pmap

# api.binance.com 은 미국 IP 를 451 로 막는다 → GitHub Actions(미국 서버)에선 전멸.
# data-api.binance.vision 은 바이낸스 공식 공개시세 미러라 지역 차단이 없다.
SPOT_HOSTS = ("https://api.binance.com", "https://data-api.binance.vision")
FUT = "https://fapi.binance.com"  # 선물은 미러가 없다 — 막히면 futures_context 가 빈 dict


_good_host: str | None = None  # 한 번 통한 호스트를 기억 — 매 호출마다 451 재시도(sleep)로 수 분 날리지 않게


def _spot(path: str, **kw):
    global _good_host
    hosts = [_good_host] + [h for h in SPOT_HOSTS if h != _good_host] if _good_host else list(SPOT_HOSTS)
    last: Exception | None = None
    for host in hosts:
        try:
            data = get_json(f"{host}{path}", **kw)
            _good_host = host
            return data
        except Exception as e:  # noqa: BLE001
            last = e
    raise last  # type: ignore[misc]

STABLES = {
    "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "UST", "USDD",
    "EUR", "TRY", "BRL", "ARS", "GBP", "AUD", "JPY", "RUB", "ZAR", "PLN",
    "USD1", "XUSD", "AEUR", "USDE",
}
LEVERAGED_SUFFIX = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def ticker_24hr() -> list[dict]:
    return _spot("/api/v3/ticker/24hr", timeout=25)


def usdt_universe(tickers: list[dict], min_qvol: float) -> list[dict]:
    """USDT 마켓에서 스테이블/레버리지토큰 제외 + 거래대금 필터."""
    out = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT") or s.endswith(LEVERAGED_SUFFIX):
            continue
        base = s[:-4]
        if base in STABLES or not base:
            continue
        try:
            qv = float(t["quoteVolume"])
            last = float(t["lastPrice"])
        except (KeyError, ValueError):
            continue
        if qv < min_qvol or last <= 0:
            continue
        t["_base"] = base
        t["_qvol"] = qv
        t["_last"] = last
        t["_chg24"] = float(t.get("priceChangePercent") or 0)
        out.append(t)
    return out


def klines(symbol: str, interval: str = "4h", limit: int = 180) -> list[list]:
    return _spot(
        "/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=20,
    )


def klines_many(symbols: list[str], interval: str, limit: int, workers: int = 8) -> dict[str, list]:
    res = pmap(lambda s: klines(s, interval, limit), symbols, workers=workers)
    return {s: k for s, k in zip(symbols, res) if k}


def closes(kl: list[list]) -> list[float]:
    return [float(r[4]) for r in kl]


def quote_volumes(kl: list[list]) -> list[float]:
    return [float(r[7]) for r in kl]


def futures_context(symbol: str) -> dict:
    """펀딩비/미결제약정 — 급등 원인 판별용. 선물 미상장이면 빈 dict."""
    out: dict = {}
    try:
        pi = get_json(f"{FUT}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=10)
        out["funding"] = float(pi.get("lastFundingRate") or 0)
        out["mark"] = float(pi.get("markPrice") or 0)
    except Exception:  # noqa: BLE001
        return {}
    try:
        oi = get_json(f"{FUT}/futures/data/openInterestHist",
                      params={"symbol": symbol, "period": "1h", "limit": 6}, timeout=10)
        if len(oi) >= 2:
            first = float(oi[0]["sumOpenInterest"])
            last = float(oi[-1]["sumOpenInterest"])
            if first > 0:
                out["oi_chg_pct"] = (last / first - 1) * 100
    except Exception:  # noqa: BLE001
        pass
    try:
        ls = get_json(f"{FUT}/futures/data/topLongShortAccountRatio",
                      params={"symbol": symbol, "period": "1h", "limit": 1}, timeout=10)
        if ls:
            out["long_short"] = float(ls[0]["longShortRatio"])
    except Exception:  # noqa: BLE001
        pass
    return out

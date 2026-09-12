"""주식 급등 소스 — 미국(야후 파이낸스 스크리너) + 한국(네이버 금융). 둘 다 무료·무키."""
from __future__ import annotations

import html
import re

from ..http import get_json

YH = "https://query1.finance.yahoo.com"
YH2 = "https://query2.finance.yahoo.com"


# ────────────────────────── 미국 ──────────────────────────
def us_screener(scr_id: str = "day_gainers", count: int = 50) -> list[dict]:
    d = get_json(f"{YH}/v1/finance/screener/predefined/saved", timeout=20,
                 params={"scrIds": scr_id, "count": count, "start": 0})
    res = (d.get("finance") or {}).get("result") or []
    if not res:
        return []
    out = []
    for q in res[0].get("quotes", []):
        try:
            out.append({
                "symbol": q.get("symbol"),
                "name": q.get("shortName") or q.get("longName") or q.get("symbol"),
                "price": float(q.get("regularMarketPrice") or 0),
                "chg": float(q.get("regularMarketChangePercent") or 0),
                "volume": float(q.get("regularMarketVolume") or 0),
                "avg_volume": float(q.get("averageDailyVolume3Month") or 0),
                "mcap": float(q.get("marketCap") or 0),
                "exchange": q.get("fullExchangeName") or "",
                "state": q.get("marketState") or "",
                "pre_chg": float(q.get("preMarketChangePercent") or 0),
                "post_chg": float(q.get("postMarketChangePercent") or 0),
                "src": scr_id,
            })
        except (TypeError, ValueError):
            continue
    return out


def us_movers(screeners: list[str], count: int = 50) -> list[dict]:
    seen: dict[str, dict] = {}
    for s in screeners:
        try:
            rows = us_screener(s, count)
        except Exception:  # noqa: BLE001
            continue
        for r in rows:
            if r["symbol"] and r["symbol"] not in seen:
                seen[r["symbol"]] = r
    return list(seen.values())


def us_intraday(symbol: str) -> dict:
    """프리/애프터 포함 당일 흐름 — 급등이 장중인지 시간외인지 구분."""
    try:
        d = get_json(f"{YH2}/v8/finance/chart/{symbol}", timeout=15,
                     params={"range": "1d", "interval": "5m", "includePrePost": "true"})
        meta = ((d.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
        return {
            "prev_close": meta.get("chartPreviousClose"),
            "price": meta.get("regularMarketPrice"),
            "state": meta.get("marketState") if "marketState" in meta else None,
        }
    except Exception:  # noqa: BLE001
        return {}


# ────────────────────────── 한국 ──────────────────────────
# 2026-09 네이버 금융 PC가 Next.js로 개편되며 sise_rise.naver HTML 테이블이 사라졌다.
# 모바일 공개 API(키 불필요)가 같은 '상승' 리스트를 JSON으로 준다.
NV = "https://m.stock.naver.com/api/stocks/up"
KR_MARKETS = ("KOSPI", "KOSDAQ")


_TAG = re.compile(r"<[^>]+>")


def _f(s: object) -> float:
    """'2,390' · '29.89' · '+1.2%' · 태그 섞인 값을 숫자로."""
    if isinstance(s, (int, float)):
        return float(s)
    t = html.unescape(_TAG.sub("", str(s or "")))
    t = t.replace(",", "").replace("%", "").replace("+", "").strip()
    try:
        return float(t)
    except ValueError:
        return 0.0


def _kr_page(market: str, page: int, page_size: int = 100) -> list[dict]:
    d = get_json(f"{NV}/{market}", timeout=15, retries=1,
                 params={"page": page, "pageSize": page_size})
    out: list[dict] = []
    for q in d.get("stocks") or []:
        code = str(q.get("itemCode") or "")
        if not code.isdigit():
            continue
        price = _f(q.get("closePrice"))
        if price <= 0:
            continue
        # accumulatedTradingValue 단위는 백만원 → 억원으로 환산
        val_eok = _f(q.get("accumulatedTradingValue")) / 100.0
        volume = _f(q.get("accumulatedTradingVolume"))
        if val_eok <= 0:  # 값이 비면 가격×거래량으로 근사
            val_eok = price * volume / 1e8
        out.append({
            "code": code,
            "symbol": code,
            "name": (q.get("stockName") or code).strip(),
            "price": price,
            "chg": _f(q.get("fluctuationsRatio")),
            "volume": volume,
            "trade_value_eok": round(val_eok, 1),
            "mcap_eok": _f(q.get("marketValue")),
            "limit_up": (q.get("compareToPreviousPrice") or {}).get("name") == "UPPER_LIMIT",
            "exchange": market,
            "url": f"https://finance.naver.com/item/main.naver?code={code}",
            "market": "KR",
        })
    return out


def kr_movers(pages: int = 1, page_size: int = 100) -> list[dict]:
    """네이버 금융 '상승' 종목 (코스피+코스닥). pages=1이면 시장별 상위 100종목."""
    seen: dict[str, dict] = {}
    for market in KR_MARKETS:
        for page in range(1, max(1, pages) + 1):
            try:
                rows = _kr_page(market, page, page_size)
            except Exception:  # noqa: BLE001
                break
            if not rows:
                break
            for r in rows:
                seen.setdefault(r["code"], r)
    return sorted(seen.values(), key=lambda r: r["chg"], reverse=True)

"""업비트 — 원화 마켓 시세 + 공지(상장 이벤트 감지). 김프/상장빔 원인 판별에 씀."""
from __future__ import annotations

import re

from ..http import get_json

BASE = "https://api.upbit.com/v1"
NOTICE = "https://api-manager.upbit.com/api/v1/announcements"


def krw_markets() -> list[str]:
    rows = get_json(f"{BASE}/market/all", params={"isDetails": "false"}, timeout=15)
    return [r["market"] for r in rows if r["market"].startswith("KRW-")]


def krw_tickers() -> dict[str, dict]:
    """{BASE심볼: {price, chg24}}"""
    mk = krw_markets()
    out: dict[str, dict] = {}
    for i in range(0, len(mk), 100):
        chunk = ",".join(mk[i:i + 100])
        try:
            for t in get_json(f"{BASE}/ticker", params={"markets": chunk}, timeout=15):
                out[t["market"].split("-")[1]] = {
                    "price": t.get("trade_price"),
                    "chg24": (t.get("signed_change_rate") or 0) * 100,
                    "value24": t.get("acc_trade_price_24h") or 0,
                }
        except Exception:  # noqa: BLE001
            continue
    return out


def recent_notices(limit: int = 30) -> list[dict]:
    limit = max(1, min(30, limit))  # 업비트 per_page 상한 30
    try:
        d = get_json(NOTICE, timeout=15,
                     params={"os": "web", "page": 1, "per_page": limit, "category": "all"})
    except Exception:  # noqa: BLE001
        return []
    notices = (d.get("data") or {}).get("notices") or []
    return [{"title": n.get("title", ""), "at": n.get("listed_at", ""),
             "url": f"https://upbit.com/service_center/notice?id={n.get('id')}"} for n in notices]


_TICKER_IN_TITLE = re.compile(r"\(([A-Z0-9]{2,10})\)")


def listing_events() -> dict[str, dict]:
    """최근 공지에서 '(SYMBOL)' 패턴을 뽑아 심볼→공지 매핑."""
    out: dict[str, dict] = {}
    for n in recent_notices(30):
        title = n["title"]
        kind = None
        if any(k in title for k in ("디지털 자산 추가", "신규 거래지원", "거래 지원 개시", "마켓 추가")):
            kind = "업비트 신규상장"
        elif "유의" in title or "거래 지원 종료" in title:
            kind = "업비트 유의/상폐"
        elif "이벤트" in title:
            kind = "업비트 이벤트"
        if not kind:
            continue
        for sym in _TICKER_IN_TITLE.findall(title):
            out.setdefault(sym, {"kind": kind, "title": title, "url": n["url"], "at": n["at"]})
    return out

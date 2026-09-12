"""무료 뉴스 RSS 수집 + 심볼 매칭 (급등 '이유' 근거)."""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from ..http import get_text, pmap

CRYPTO_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("TheBlock", "https://www.theblock.co/rss.xml"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ("코인니스", "https://kr.cointelegraph.com/rss"),
]

_ITEM = re.compile(r"<item[ >].*?</item>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def _field(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S | re.I)
    if not m:
        return ""
    v = m.group(1)
    v = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", v, flags=re.S)
    return html.unescape(_TAG.sub("", v)).strip()


def _parse_feed(item: tuple[str, str]) -> list[dict]:
    source, url = item
    txt = get_text(url, timeout=15, retries=1)
    out = []
    for block in _ITEM.findall(txt)[:40]:
        title = _field(block, "title")
        if not title:
            continue
        link = _field(block, "link")
        pub = _field(block, "pubDate")
        ts = 0.0
        if pub:
            try:
                ts = parsedate_to_datetime(pub).timestamp()
            except Exception:  # noqa: BLE001
                ts = 0.0
        out.append({"source": source, "title": title, "url": link, "ts": ts})
    return out


def crypto_headlines(lookback_hours: int = 24) -> list[dict]:
    res = pmap(_parse_feed, CRYPTO_FEEDS, workers=6)
    cutoff = time.time() - lookback_hours * 3600
    items: list[dict] = []
    for r in res:
        for it in (r or []):
            if it["ts"] == 0 or it["ts"] >= cutoff:
                items.append(it)
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


_WORD = re.compile(r"[A-Za-z0-9$가-힣]+")


def match_symbol(headlines: list[dict], symbol: str, name: str = "") -> list[dict]:
    """헤드라인에서 해당 코인 언급 기사만 골라낸다. 짧은 티커 오탐 방지 포함."""
    sym = symbol.upper()
    name_l = (name or "").lower()
    hits = []
    for h in headlines:
        words = {w.upper() for w in _WORD.findall(h["title"])}
        low = h["title"].lower()
        ok = False
        if len(sym) >= 3 and (sym in words or f"${sym}" in words):
            ok = True
        elif len(sym) <= 2 and (f"${sym}" in words):
            ok = True
        if not ok and name_l and len(name_l) >= 4 and name_l in low:
            ok = True
        if ok:
            hits.append(h)
    return hits[:3]


def stock_headlines(symbol: str, lookback_hours: int = 48) -> list[dict]:
    """야후 파이낸스 종목별 RSS (미국주식)."""
    url = ("https://feeds.finance.yahoo.com/rss/2.0/headline"
           f"?s={symbol}&region=US&lang=en-US")
    try:
        return [h for h in _parse_feed(("Yahoo", url))
                if h["ts"] == 0 or h["ts"] >= time.time() - lookback_hours * 3600][:3]
    except Exception:  # noqa: BLE001
        return []


_NAVER_NEWS = re.compile(
    r'<a href="(/item/news_read[^"]+)"[^>]*>\s*(?:<[^>]+>)*\s*([^<]{4,80})', re.S)


def kr_stock_headlines(code: str, limit: int = 3) -> list[dict]:
    """네이버 금융 종목뉴스 (국내주식)."""
    url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1"
    try:
        txt = get_text(url, timeout=12, retries=1,
                       headers={"Referer": f"https://finance.naver.com/item/main.naver?code={code}"})
    except Exception:  # noqa: BLE001
        return []
    out = []
    for href, title in _NAVER_NEWS.findall(txt)[: limit * 3]:
        t = html.unescape(title).strip()
        if not t or t in [o["title"] for o in out]:
            continue
        out.append({"source": "네이버금융", "title": t,
                    "url": "https://finance.naver.com" + html.unescape(href), "ts": 0})
        if len(out) >= limit:
            break
    return out


# ── 구글 뉴스 RSS: 종목 단위 타깃 검색 (무료·무키). 급등 이유 적중률이 제일 높다 ──
_JUNK = ("binance square", "community insights", "market sentiment", "'s insights",
         "price of ", "price prediction", "how to buy", "what is ", "perpetual chart",
         "usdⓈ-margined", "spot |", "trade ", "mortgage rates", "best ", "top 10")

_CRYPTO_CTX = ("코인", "가상자산", "암호화폐", "블록체인", "비트코인", "토큰", "상장", "에어드랍",
               "crypto", "token", "blockchain", "bitcoin", "listing", "defi", "airdrop",
               "rally", "surge", "soar", "jump")


def google_news(query: str, lang: str = "ko", within_days: int = 2, limit: int = 3,
                must: list[str] | None = None, context: list[str] | None = None) -> list[dict]:
    """must: 제목에 반드시 하나는 들어가야 하는 단어. context: 주제 확인용 단어(오탐 제거)."""
    import urllib.parse as up

    q = up.quote(f"{query} when:{within_days}d")
    if lang == "ko":
        url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    else:
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        items = _parse_feed(("구글뉴스", url))
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        title = it["title"]
        low = title.lower()
        if any(j in low for j in _JUNK):
            continue
        if must and not any(m.lower() in low for m in must if m):
            continue
        if context and not any(c.lower() in low for c in context):
            continue
        core = re.sub(r"\s*-\s*[^-]{2,30}$", "", title).strip()  # 끝의 " - 매체명" 제거
        if core in seen or len(core) < 8:
            continue
        seen.add(core)
        it["title"] = core
        it["outlet"] = (title[len(core):].lstrip(" -") or "구글뉴스")[:20]
        out.append(it)
        if len(out) >= limit:
            break
    return out


def coin_news(symbol: str, name: str = "") -> list[dict]:
    """코인 급등 이유용 — 한국어 우선, 부족하면 영어. 주제 무관 기사는 걸러낸다."""
    label = name or symbol
    must = [m for m in {symbol, name, label} if m and len(m) >= 2]
    hits = google_news(f"{label} 코인", "ko", must=must, context=_CRYPTO_CTX)
    if len(hits) < 2:
        hits += google_news(f"{symbol} crypto", "en", limit=2, must=must, context=_CRYPTO_CTX)
    seen: set[str] = set()
    out = []
    for h in hits:
        if h["title"] in seen:
            continue
        seen.add(h["title"])
        out.append(h)
    return out[:3]


def kr_stock_news(name: str) -> list[dict]:
    return google_news(f"{name} 주가", "ko", must=[name], limit=3)


def us_stock_news(symbol: str, company: str = "") -> list[dict]:
    must = [symbol] + ([company.split()[0]] if company else [])
    return google_news(f"{company or symbol} stock", "en", must=must, limit=3)

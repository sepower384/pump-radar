"""테마 데이터 — 한국(네이버 증권 모바일 테마 API) / 미국(큐레이션 테마 사전 + 야후 업종·시세). 전부 무료·무키.

한국 (2026-09 실측)
  GET m.stock.naver.com/api/stocks/theme?page=1&pageSize=100     → 테마 266개 {no,name,changeRate,riseCount,...}
  GET m.stock.naver.com/api/stocks/theme/{no}?page=1&pageSize=100 → 구성종목(시세·거래대금·시총) + themeItemInfoMap(편입 사유)
  GET m.stock.naver.com/api/stock/{code}/price?pageSize=21        → 일별 시가·고가·거래량 (평소 대비 거래량 배수)
  GET api.stock.naver.com/chart/domestic/item/{code}/minute       → 당일 1분봉 (차트용)
미국
  GET query1.finance.yahoo.com/v1/finance/search?q=SYM   → sector/industry (6시간 캐시)
  GET query1.finance.yahoo.com/v7/finance/spark?symbols= → 20종목 한 번에 현재가·전일종가·거래량·장중 흐름
  GET query2.finance.yahoo.com/v8/finance/chart/SYM?range=1mo&interval=1d → 일별 거래량(평소 대비 배수)
  (quoteSummary·v7 quote 는 crumb 필요 → 401, 사용하지 않음. 그래서 미국 동종 종목 시가총액은 모른다)
"""
from __future__ import annotations

import json
import re
import time

from ..config import DATA_DIR
from ..http import get_json, pmap
from .stocks import _f

NV = "https://m.stock.naver.com/api"
NV_CHART = "https://api.stock.naver.com/chart/domestic/item"
YH = "https://query1.finance.yahoo.com"
YH2 = "https://query2.finance.yahoo.com"
_CACHE = DATA_DIR / "theme_cache.json"
STATIC_TTL = 6 * 3600


def _cached(key: str, ttl: int, fetch):
    now = time.time()
    blob: dict = {}
    if _CACHE.exists():
        try:
            blob = json.loads(_CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            blob = {}
    ent = blob.get(key)
    if ent and now - ent.get("ts", 0) < ttl:
        return ent["v"]
    try:
        v = fetch()
    except Exception:  # noqa: BLE001
        return ent["v"] if ent else None
    blob[key] = {"ts": now, "v": v}
    try:
        _CACHE.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return v


# ────────────────────────── 한국 ──────────────────────────
def kr_keywords(theme_name: str) -> list[str]:
    """'로봇(산업용/협동로봇 등)' → ['로봇'], '양자암호/양자컴퓨팅' → ['양자암호', '양자컴퓨팅']"""
    head = re.split(r"[(\[]", theme_name or "")[0]
    return [w.strip() for w in re.split(r"[/,·]", head) if len(w.strip()) >= 2]


def kr_theme_list(pages: int = 3) -> list[dict]:
    out: list[dict] = []
    for page in range(1, pages + 1):
        d = get_json(f"{NV}/stocks/theme", params={"page": page, "pageSize": 100}, timeout=15, retries=1)
        groups = d.get("groups") or []
        for g in groups:
            total = int(g.get("totalCount") or 0)
            rise = int(g.get("riseCount") or 0)
            out.append({"no": g.get("no"), "name": g.get("name") or "", "chg": _f(g.get("changeRate")),
                        "rise": rise, "total": total, "breadth": (rise / total) if total else 0.0})
        if len(out) >= int(d.get("totalCount") or 0) or not groups:
            break
    return out


def kr_row(q: dict, why: str = "") -> dict:
    code = str(q.get("itemCode") or "")
    return {
        "market": "KR", "symbol": code, "code": code, "name": (q.get("stockName") or code).strip(),
        "price": _f(q.get("closePriceRaw") or q.get("closePrice")),
        "chg": _f(q.get("fluctuationsRatio")),
        # accumulatedTradingValue 는 백만원 단위 → 억 원
        "trade_value": round(_f(q.get("accumulatedTradingValue")) / 100.0, 1),
        "mcap": _f(q.get("marketValue")),  # 억 원
        "limit_up": (q.get("compareToPreviousPrice") or {}).get("name") == "UPPER_LIMIT",
        "tradable": (q.get("tradeStopType") or {}).get("name", "TRADING") == "TRADING",
        "why": (why or "").strip(),
        "url": f"https://finance.naver.com/item/main.naver?code={code}",
        "vol_x": 0.0, "open": 0.0, "high": 0.0, "series": [],
    }


def kr_theme_members(no: int) -> dict:
    d = get_json(f"{NV}/stocks/theme/{no}", params={"page": 1, "pageSize": 100}, timeout=15, retries=1)
    info = d.get("themeItemInfoMap") or {}
    g = d.get("groupInfo") or {}
    total = int(g.get("totalCount") or 0)
    rise = int(g.get("riseCount") or 0)
    members = [kr_row(q, info.get(str(q.get("itemCode")), "")) for q in d.get("stocks") or []]
    members = [m for m in members if m["price"] > 0 and m["tradable"]]
    name = g.get("name") or ""
    return {"id": f"KR{no}", "no": no, "name": name, "chg": _f(g.get("changeRate")), "rise": rise,
            "total": total, "breadth": (rise / total) if total else 0.0,
            "description": (d.get("themeDescription") or "")[:200], "keywords": kr_keywords(name),
            "members": members}


def kr_daily(code: str, n: int = 21) -> dict:
    """오늘 시가·고가·현재가 + 평소(직전 n-1일 평균) 대비 거래량 배수."""
    rows = get_json(f"{NV}/stock/{code}/price", params={"page": 1, "pageSize": n}, timeout=12, retries=1)
    if not rows:
        return {}
    today = rows[0]
    vols = [_f(r.get("accumulatedTradingVolume")) for r in rows[1:]]
    avg = sum(vols) / len(vols) if vols else 0
    return {"open": _f(today.get("openPrice")), "high": _f(today.get("highPrice")),
            "last": _f(today.get("closePrice")), "date": today.get("localTradedAt", ""),
            "vol_x": round(_f(today.get("accumulatedTradingVolume")) / avg, 1) if avg else 0.0}


def kr_intraday_pct(code: str, price: float, chg: float) -> list[float]:
    """당일 1분봉 → 전일 종가 대비 등락률(%) 목록 (차트용)."""
    prev = price / (1 + chg / 100) if price and chg > -100 else 0
    if not prev:
        return []
    rows = get_json(f"{NV_CHART}/{code}/minute", timeout=12, retries=1) or []
    if not rows:
        return []
    day = str(rows[-1].get("localDateTime", ""))[:8]
    return [round((float(r["currentPrice"]) / prev - 1) * 100, 2)
            for r in rows if str(r.get("localDateTime", "")).startswith(day) and r.get("currentPrice")]


# ────────────────────────── 미국 ──────────────────────────
# 큐레이션 테마 사전 — 종목은 대략 시가총액 큰 순. 새 테마는 여기 한 줄 추가하면 된다.
US_THEMES: dict[str, dict] = {
    "quantum": {"name": "양자컴퓨팅", "en": "Quantum computing",
                "keywords": ["quantum", "qubit", "양자"],
                "members": ["IONQ", "QBTS", "RGTI", "QUBT", "ARQQ"]},
    "space": {"name": "우주항공", "en": "Space",
              "keywords": ["space", "satellite", "rocket", "launch", "lunar", "우주", "위성"],
              "members": ["RKLB", "ASTS", "PL", "LUNR", "RDW", "BKSY", "SPCE"]},
    "robotics": {"name": "로봇·휴머노이드", "en": "Robotics",
                 "keywords": ["robot", "humanoid", "automation", "로봇"],
                 "members": ["ISRG", "TER", "SYM", "SERV", "RR"]},
    "ai_semi": {"name": "AI 반도체", "en": "AI semiconductors",
                "keywords": ["AI chip", "semiconductor", "GPU", "HBM", "chip", "반도체"],
                "members": ["NVDA", "AVGO", "TSM", "AMD", "MU", "ARM", "MRVL", "SMCI"]},
    "nuclear": {"name": "원전·SMR", "en": "Nuclear / SMR",
                "keywords": ["nuclear", "uranium", "SMR", "reactor", "원전", "우라늄"],
                "members": ["CEG", "CCJ", "BWXT", "OKLO", "SMR", "LEU", "NNE", "UEC"]},
    "obesity": {"name": "비만치료제", "en": "Obesity drugs",
                "keywords": ["obesity", "GLP-1", "weight loss", "Wegovy", "Zepbound", "비만"],
                "members": ["LLY", "NVO", "VKTX", "HIMS", "ALT", "TERN"]},
    "crypto": {"name": "크립토 관련주", "en": "Crypto stocks",
               "keywords": ["bitcoin", "crypto", "ethereum", "stablecoin", "비트코인", "코인"],
               "members": ["COIN", "MSTR", "HOOD", "CRCL", "MARA", "RIOT", "CLSK", "GLXY"]},
    "drone_defense": {"name": "드론·방산", "en": "Drones / defense",
                      "keywords": ["drone", "defense", "Pentagon", "military", "missile", "드론", "방산"],
                      "members": ["LMT", "PLTR", "KTOS", "AVAV", "RCAT", "ONDS", "UMAC"]},
    "power_infra": {"name": "전력 인프라", "en": "Power infrastructure",
                    "keywords": ["power", "grid", "electricity", "data center", "utility", "전력"],
                    "members": ["GEV", "VST", "ETN", "VRT", "NRG", "TLN", "PWR"]},
}

# 사전에 없는 급등주는 야후 업종으로 테마를 추정한다
US_INDUSTRY_THEMES: dict[str, str] = {
    "Uranium": "nuclear",
    "Utilities - Independent Power Producers": "power_infra",
    "Utilities - Renewable": "power_infra",
    "Electrical Equipment & Parts": "power_infra",
    "Semiconductors": "ai_semi",
    "Semiconductor Equipment & Materials": "ai_semi",
    "Aerospace & Defense": "drone_defense",
    "Capital Markets": "",
}


def us_theme_of(symbol: str, industry: str = "") -> str:
    sym = (symbol or "").upper()
    for tid, t in US_THEMES.items():
        if sym in t["members"]:
            return tid
    return US_INDUSTRY_THEMES.get(industry or "", "") or ""


def us_industry(symbol: str) -> str:
    """야후 검색 API 의 industry. 종목별 6시간 캐시."""
    def fetch():
        d = get_json(f"{YH}/v1/finance/search", params={"q": symbol, "quotesCount": 1, "newsCount": 0},
                     timeout=12, retries=1)
        q = (d.get("quotes") or [{}])[0]
        return q.get("industry") or "" if (q.get("symbol") or "").upper() == symbol.upper() else ""
    v = _cached(f"us_industry:{symbol.upper()}", STATIC_TTL, fetch)
    return v if isinstance(v, str) else ""


def us_spark(symbols: list[str], interval: str = "5m") -> dict[str, dict]:
    """{sym: {price, prev, chg, volume, high, low, open, trade_value, series(등락률%), name}}"""
    out: dict[str, dict] = {}
    syms = [s for s in dict.fromkeys(s.upper() for s in symbols) if s]
    for i in range(0, len(syms), 20):
        d = get_json(f"{YH}/v7/finance/spark", params={"symbols": ",".join(syms[i:i + 20]),
                                                         "range": "1d", "interval": interval},
                     timeout=20, retries=1)
        for x in (d.get("spark") or {}).get("result") or []:
            try:
                resp = (x.get("response") or [{}])[0]
                m = resp.get("meta") or {}
                prev = float(m.get("chartPreviousClose") or m.get("previousClose") or 0)
                price = float(m.get("regularMarketPrice") or 0)
                closes = [c for c in ((resp.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
                          if c is not None]
                if not prev or not price:
                    continue
                vol = float(m.get("regularMarketVolume") or 0)
                out[x["symbol"]] = {
                    "price": price, "prev": prev, "chg": round((price / prev - 1) * 100, 2),
                    "volume": vol, "trade_value": price * vol,
                    "high": float(m.get("regularMarketDayHigh") or 0),
                    "low": float(m.get("regularMarketDayLow") or 0),
                    "open": float(closes[0]) if closes else 0.0,   # 첫 5분봉 종가를 시가 근사치로
                    "series": [round((c / prev - 1) * 100, 2) for c in closes],
                    "name": m.get("shortName") or m.get("longName") or "",
                    "time": float(m.get("regularMarketTime") or 0),  # 시세 시각 → 거래일 판정
                }
            except (TypeError, ValueError, KeyError, IndexError):
                continue
    return out


def us_vol_x(symbol: str) -> float:
    """오늘 거래량 / 직전 약 20거래일 평균."""
    try:
        d = get_json(f"{YH2}/v8/finance/chart/{symbol}", params={"range": "1mo", "interval": "1d"},
                     timeout=12, retries=1)
        vols = [v for v in d["chart"]["result"][0]["indicators"]["quote"][0]["volume"] if v]
    except Exception:  # noqa: BLE001
        return 0.0
    if len(vols) < 5:
        return 0.0
    avg = sum(vols[:-1]) / len(vols[:-1])
    return round(vols[-1] / avg, 1) if avg else 0.0


def many(fn, items, workers: int = 6) -> list:
    return pmap(fn, items, workers=workers)

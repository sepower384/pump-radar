"""급등 '이유' 추론 엔진.

증거를 세기 순으로 쌓는다:
  상장공지 > 뉴스 > 파생(선물) > 테마 동반 > 김프 > 시장전체 > 거래량뿐(=원인불명)
LLM은 있으면 문장만 다듬는 용도. 없어도 규칙기반 설명이 그대로 나간다.
"""
from __future__ import annotations

import json
import os

from ..sources import binance, news, upbit


class MarketContext:
    """사이클당 1회만 만드는 공용 시장 맥락."""

    def __init__(self) -> None:
        self.headlines: list[dict] = []
        self.listings: dict[str, dict] = {}
        self.trending: set[str] = set()
        self.themes: dict[str, list[str]] = {}
        self.meta: dict[str, dict] = {}
        self.upbit_krw: dict[str, dict] = {}
        self.usdkrw: float = 0.0
        self.btc_chg24: float = 0.0
        self.eth_chg24: float = 0.0
        self.pumping_themes: dict[str, int] = {}
        self.offline: bool = False  # True면 종목별 네트워크 조회를 건너뛴다

    @classmethod
    def build(cls, lookback_hours: int = 24) -> "MarketContext":
        from ..sources import coingecko as cg

        c = cls()
        for attr, fn in (
            ("headlines", lambda: news.crypto_headlines(lookback_hours)),
            ("listings", upbit.listing_events),
            ("trending", cg.trending_symbols),
            ("themes", cg.theme_map),
            ("meta", cg.symbol_meta),
            ("upbit_krw", upbit.krw_tickers),
        ):
            try:
                setattr(c, attr, fn())
            except Exception:  # noqa: BLE001
                pass

        usdt = c.upbit_krw.get("USDT") or {}
        c.usdkrw = float(usdt.get("price") or 0)
        try:
            for t in binance.ticker_24hr():
                if t["symbol"] == "BTCUSDT":
                    c.btc_chg24 = float(t["priceChangePercent"])
                elif t["symbol"] == "ETHUSDT":
                    c.eth_chg24 = float(t["priceChangePercent"])
        except Exception:  # noqa: BLE001
            pass
        return c

    def note_pump(self, base: str) -> None:
        """같은 사이클에 급등한 코인들의 테마를 세어 '섹터 순환매'를 판별."""
        for th in self.themes.get(base.upper(), []):
            self.pumping_themes[th] = self.pumping_themes.get(th, 0) + 1

    def kimchi_premium(self, base: str, usd_price: float) -> float | None:
        row = self.upbit_krw.get(base.upper())
        if not row or not self.usdkrw or not usd_price:
            return None
        krw = float(row.get("price") or 0)
        if krw <= 0:
            return None
        return (krw / (usd_price * self.usdkrw) - 1) * 100


_NEWS_KIND = [
    (("listing", "lists", "listed", "상장"), "거래소 상장"),
    (("partnership", "partners", "파트너"), "파트너십"),
    (("etf",), "ETF"),
    (("upgrade", "mainnet", "hard fork", "메인넷"), "네트워크 업그레이드"),
    (("burn", "buyback", "소각"), "소각/바이백"),
    (("hack", "exploit", "해킹"), "해킹/보안사고"),
    (("sec ", "lawsuit", "regulat", "규제"), "규제/소송"),
    (("unlock", "vesting", "언락"), "물량 언락"),
    (("airdrop", "에어드랍"), "에어드랍"),
    (("whale", "고래"), "고래 매집"),
]


def _classify(title: str, table: list) -> str:
    low = title.lower()
    for keys, label in table:
        if any(k.lower() in low for k in keys):
            return label
    return "뉴스"


def explain_crypto(c: dict, ctx: MarketContext) -> dict:
    base = c["base"].upper()
    meta = ctx.meta.get(base, {})
    name = meta.get("name", base)
    up = c.get("direction", "up") == "up"
    ev: list[dict] = []

    li = ctx.listings.get(base)
    if li:
        ev.append({"w": 100, "tag": "상장/공지", "icon": "🏛",
                   "text": f"{li['kind']} — {li['title'][:60]}", "url": li.get("url", "")})

    seen_titles: set[str] = set()
    for h in news.match_symbol(ctx.headlines, base, name)[:2]:
        seen_titles.add(h["title"][:40])
        ev.append({"w": 88, "tag": _classify(h["title"], _NEWS_KIND), "icon": "📰",
                   "text": f"{h['title'][:95]} ({h['source']})", "url": h.get("url", "")})

    # 종목 단위 타깃 검색 — 글로벌 RSS가 안 잡는 국내발 재료까지 잡는다
    for h in ([] if ctx.offline else news.coin_news(base, name)[:2]):
        if h["title"][:40] in seen_titles:
            continue
        ev.append({"w": 84, "tag": _classify(h["title"], _NEWS_KIND), "icon": "📰",
                   "text": f"{h['title'][:95]} ({h.get('outlet', '구글뉴스')})",
                   "url": h.get("url", "")})

    fut = ({} if ctx.offline or c.get("market") != "binance"
           else binance.futures_context(c["symbol"]))
    if fut:
        oi = fut.get("oi_chg_pct")
        fr = (fut.get("funding") or 0) * 100
        if oi is not None and oi > 8 and up:
            ev.append({"w": 70, "tag": "선물 자금유입", "icon": "📈",
                       "text": f"미결제약정 6h {oi:+.1f}% · 펀딩 {fr:+.3f}% → 신규 롱이 밀어올리는 중"})
        elif oi is not None and oi < -8 and up:
            ev.append({"w": 75, "tag": "숏스퀴즈 의심", "icon": "🔥",
                       "text": f"가격은 오르는데 미결제약정 {oi:+.1f}% → 숏 청산 연쇄"})
        elif fr > 0.05:
            ev.append({"w": 55, "tag": "롱 과열", "icon": "⚠️",
                       "text": f"펀딩비 {fr:+.3f}% — 롱이 비용 내며 버티는 구간(되돌림 주의)"})

    for th in ctx.themes.get(base, []):
        n = ctx.pumping_themes.get(th, 0)
        if n >= 2:
            ev.append({"w": 60, "tag": "테마 순환매", "icon": "🧩",
                       "text": f"'{th}' 섹터에서 동시에 {n}종목 급등 — 개별 재료보다 섹터 자금"})
            break

    kp = ctx.kimchi_premium(base, c["price"])
    if kp is not None and kp >= 2.5:
        ev.append({"w": 50, "tag": "국내 매수세", "icon": "🇰🇷",
                   "text": f"업비트 김프 {kp:+.1f}% — 국내가 먼저 사들이는 중"})
    elif kp is not None and kp <= -2.5:
        ev.append({"w": 22, "tag": "역프", "icon": "🇰🇷",
                   "text": f"업비트 대비 {kp:+.1f}% — 해외 주도(국내는 따라가는 중)"})

    if base in ctx.trending:
        ev.append({"w": 45, "tag": "검색 급증", "icon": "🔎",
                   "text": "코인게코 트렌딩 진입 — 개인 관심 유입"})

    if ctx.btc_chg24 >= 2 and abs(c.get("chg24h", 0)) < ctx.btc_chg24 * 2.5:
        ev.append({"w": 30, "tag": "시장 전체", "icon": "🌊",
                   "text": f"BTC 24h {ctx.btc_chg24:+.1f}% — 시장 전반 상승분이 큼(개별 재료 약함)"})

    vx = c.get("vol_x") or 0
    if vx >= 3:
        ev.append({"w": 35, "tag": "거래량 폭증", "icon": "📊",
                   "text": f"최근 15분 거래대금이 평소의 {vx:.1f}배"})

    ev.sort(key=lambda e: e["w"], reverse=True)
    top = ev[:4]
    if not top:
        top = [{"w": 0, "tag": "원인 미확인", "icon": "❓",
                "text": "공개 뉴스·공지·파생 지표에 뚜렷한 재료 없음. 수급(세력) 주도 가능성."}]

    conf = "높음" if top[0]["w"] >= 80 else ("보통" if top[0]["w"] >= 55 else "낮음")
    return {"evidence": top, "confidence": conf, "name": name,
            "headline": _one_liner(c, top, name)}


def _one_liner(c: dict, ev: list[dict], name: str) -> str:
    tag = ev[0]["tag"]
    verb = "급등" if c.get("direction", "up") == "up" else "급락"
    mapping = {
        "거래소 상장": "거래소 상장 재료", "상장/공지": "거래소 상장/공지 재료",
        "숏스퀴즈 의심": "숏스퀴즈", "선물 자금유입": "선물 자금 유입",
        "테마 순환매": "섹터 순환매", "국내 매수세": "국내 매수세",
        "시장 전체": "시장 전체 상승", "검색 급증": "관심 급증",
        "거래량 폭증": "수급 주도", "롱 과열": "레버리지 과열",
        "원인 미확인": "재료 불명 · 수급 주도 의심",
    }
    return f"{name} {verb} — {mapping.get(tag, tag + ' 재료')}"


_US_NEWS_KIND = [
    (("earnings", "results", "beats", "misses", "revenue"), "실적"),
    (("fda", "phase", "trial", "approval"), "FDA/임상"),
    (("acquire", "acquisition", "merger", "buyout", "takeover"), "M&A"),
    (("upgrade", "price target", "initiates", "raises"), "증권사 의견"),
    (("offering", "dilution", "shelf", "convertible"), "유상증자/희석"),
    (("contract", "deal", "order", "partnership"), "수주/계약"),
    (("guidance", "outlook", "forecast"), "가이던스"),
    (("split", "dividend", "buyback"), "주주환원"),
    (("short squeeze", "short interest"), "숏스퀴즈"),
]

_KR_NEWS_KIND = [
    (("실적", "영업이익", "흑자", "적자", "매출"), "실적"),
    (("수주", "계약", "공급", "납품"), "수주/계약"),
    (("무상증자", "유상증자", "전환사채", "CB", "BW"), "증자/CB"),
    (("임상", "식약처", "품목허가", "FDA"), "임상/허가"),
    (("인수", "합병", "지분", "최대주주"), "M&A/지분"),
    (("테마", "관련주", "수혜"), "테마"),
    (("상한가", "급등", "급락"), "수급"),
]


def explain_stock(s: dict, offline: bool = False) -> dict:
    ev: list[dict] = []
    if s.get("market", "US") == "US":
        seen: set[str] = set()
        feeds = [] if offline else (news.us_stock_news(s["symbol"], s.get("name", ""))[:2]
                                    + news.stock_headlines(s["symbol"])[:2])
        for h in feeds:
            k = h["title"][:40]
            if k in seen:
                continue
            seen.add(k)
            ev.append({"w": 86, "tag": _classify(h["title"], _US_NEWS_KIND), "icon": "📰",
                       "text": h["title"][:100], "url": h.get("url", "")})
            if len(seen) >= 2:
                break
        vx = s.get("vol_x") or 0
        if vx >= 3:
            ev.append({"w": 50, "tag": "거래량 폭증", "icon": "📊",
                       "text": f"평소(3개월 평균) 거래량의 {vx:.1f}배"})
        mcap = s.get("mcap") or 0
        if 0 < mcap < 3e8 and (s.get("chg_eff") or 0) >= 15:
            ev.append({"w": 45, "tag": "초소형주", "icon": "🎢",
                       "text": f"시총 ${mcap / 1e6:.0f}M — 스퀴즈성 급등 가능성"})
        if s.get("session") in ("프리마켓", "애프터마켓"):
            ev.append({"w": 40, "tag": "시간외", "icon": "🌙",
                       "text": f"{s['session']} 움직임 — 정규장 갭 여부 확인 필요"})
    else:
        seen = set()
        nm = s.get("name", "")
        # 종목 무관 시황·증시요약 기사는 이유가 될 수 없다
        market_wide = ("시황", "코스피", "코스닥", "증시", "마감", "개장", "환율")
        feeds = [] if offline else (news.kr_stock_news(nm)[:2]
                                    + news.kr_stock_headlines(s.get("code", ""))[:3])
        for h in feeds:
            title = h["title"]
            k = title[:30]
            if k in seen or "주가," in title:  # "주가, N원 상승 마감" 같은 시세중계 기사 제외
                continue
            if nm not in title and any(w in title for w in market_wide):
                continue
            seen.add(k)
            ev.append({"w": 86, "tag": _classify(h["title"], _KR_NEWS_KIND), "icon": "📰",
                       "text": h["title"][:100], "url": h.get("url", "")})
            if len(seen) >= 2:
                break
        if s.get("limit_up") or s.get("chg", 0) >= 29:
            ev.append({"w": 60, "tag": "상한가", "icon": "🚀", "text": "가격제한폭(+30%) 도달"})
        tv = s.get("trade_value_eok") or 0
        if tv >= 500:
            ev.append({"w": 50, "tag": "거래대금 급증", "icon": "📊",
                       "text": f"거래대금 {tv:,.0f}억 — 대형 수급 유입"})

    ev.sort(key=lambda e: e["w"], reverse=True)
    if not ev:
        ev = [{"w": 0, "tag": "원인 미확인", "icon": "❓",
               "text": "공개 뉴스에 재료 없음 — 수급/테마 주도 가능성"}]
    conf = "높음" if ev[0]["w"] >= 80 else ("보통" if ev[0]["w"] >= 50 else "낮음")
    return {"evidence": ev[:3], "confidence": conf}


def llm_summarize(items: list[dict], model: str = "gemini-2.5-flash-lite") -> dict[str, str]:
    """{key: 한 줄 해설}. 키 없거나 실패하면 빈 dict — 호출부는 규칙기반 문장을 그대로 쓴다."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not items:
        return {}

    lines = []
    for it in items:
        ev = " / ".join(f"{e['tag']}: {e['text']}" for e in it["reason"]["evidence"])
        lines.append(f"[{it['key']}] {it.get('title', '')} | 근거: {ev}")

    prompt = (
        "너는 트레이딩 데스크 애널리스트다. 아래 각 종목이 왜 움직였는지 "
        "한국어 한 문장(40자 내외)으로 정리해라. 근거에 없는 사실은 절대 지어내지 마라. "
        "근거가 약하면 '재료 불명, 수급 주도'라고 써라.\n"
        'JSON 객체 하나로만 출력: {"키": "한 문장"}\n\n' + "\n".join(lines)
    )
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={key}")
    try:
        import requests

        r = requests.post(url, timeout=25, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        })
        r.raise_for_status()
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        out = json.loads(txt)
        return {str(k): str(v) for k, v in out.items()} if isinstance(out, dict) else {}
    except Exception:  # noqa: BLE001
        return {}

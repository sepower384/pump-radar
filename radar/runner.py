"""한 사이클 실행 — 스캔 → 이유 추론 → 메시지 조립 → 슬랙+텔레그램 전송 → 쿨다운 기록.

각 알림은 compose_*(메시지 조립, 전송 없음) 와 run_*(전송+기록) 로 나뉜다.
미리보기(radar/preview.py)는 compose_* 만 쓰므로 알림기록을 건드리지 않는다.
"""
from __future__ import annotations

import html
import math
import re
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import markets, store
from .config import CFG
from .engine import btc_trend, pump, reason, stock_pump, theme_follow
from .http import get_json
from .notify import charts, deliver
from .notify.message import Msg
from .sources import binance
from .sources import themes as themes_src

KST = timezone(timedelta(hours=9))


def now_kst() -> str:
    return datetime.now(KST).strftime("%m/%d %H:%M")


def _fmt_qvol(v: float) -> str:
    if v >= 1e9:
        return f"{v / 1e9:.1f}B"
    if v >= 1e6:
        return f"{v / 1e6:.0f}M"
    return f"{v / 1e3:.0f}K"


def _price(p: float) -> str:
    if p >= 100:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:.3f}"
    if p >= 0.001:
        return f"${p:.5f}"
    return f"${p:.8f}".rstrip("0")


CONF_KR = {"높음": "근거가 꽤 확실합니다", "보통": "그럴듯한 편입니다", "낮음": "추정에 가깝습니다"}


def _why_header(rsn: dict, up: bool = True) -> list[str]:
    word = "상승" if up else "하락"
    if not rsn.get("evidence"):
        return [f"🔎 *{word} 이유* — 아직 뚜렷한 뉴스나 이유를 찾지 못했습니다. "
                "이유 없는 급등락은 더 조심하시는 것이 좋습니다."]
    conf = rsn.get("confidence") or ""
    return [f"🔎 *{word} 이유* (확신도: {CONF_KR.get(conf, conf)})"]


def _size_word(mcap: float) -> str:
    if mcap <= 0:
        return ""
    b = mcap / 1e9
    kind = "초소형주(변동이 매우 큼)" if b < 0.3 else "소형주" if b < 2 else "중형주" if b < 10 else "대형주"
    return f"시가총액 ${b:.2f}B · {kind}"


_HANGUL = re.compile(r"[가-힣]")
_GTX = "https://translate.googleapis.com/translate_a/single"


def _ko(text: str) -> str:
    """영어 뉴스 제목 → 한국어 (구글 공개 번역, 키 불필요). 실패하면 원문."""
    if not text or _HANGUL.search(text):
        return text
    try:
        data = get_json(_GTX, params={"client": "gtx", "sl": "auto", "tl": "ko", "dt": "t", "q": text},
                        timeout=10)
        return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip() or text
    except Exception:  # noqa: BLE001
        return text


def _evidence_lines(rsn: dict, translate: bool = True) -> list[str]:
    out = []
    for e in rsn["evidence"]:
        body = _ko(e["text"]) if translate and e.get("icon") == "📰" else e["text"]
        txt = f"   ◦ *{e['tag']}* — {body}"
        if e.get("url"):
            txt += f" <{e['url']}|기사 보기>"
        out.append(txt)
    return out


def _finish(kind: str, con, msg: Msg | None, marks: list[tuple[str, str]], info: dict) -> dict:
    """전송하고, 슬랙·텔레그램 둘 중 하나라도 성공했을 때만 쿨다운을 기록한다."""
    if msg is None:
        return info
    res = deliver(msg)
    info.update(sent=res["delivered"], backend=res["slack"], telegram=res["telegram"])
    if res.get("slack_errors"):
        info["slack_errors"] = res["slack_errors"]
    if res["delivered"]:
        for key, payload in marks:
            store.mark_alerted(con, kind, key, payload)
        info["marked"] = len(marks)
    else:
        info["marked"] = 0
        info["note"] = "슬랙·텔레그램 모두 실패 — 쿨다운 미기록(다음 사이클 재시도), 원문은 outbox 보관"
    return info


# ─────────────────────────── 1. BTC 우상향 ───────────────────────────

def build_trend_msg(top: list[dict], fresh_syms: set[str], cfg: dict, th: float,
                    when: str = "") -> Msg:
    n = len(top)
    intro = [f"*비트코인보다 더 잘 오르고 있는 코인* {n}개를 정리했습니다.",
             "_비트코인이 오를 때 더 많이 오르고, 빠질 때 덜 빠지는 '체력 좋은 코인'을 고른 목록입니다._"]
    units: list[list[str]] = [intro]
    for i, r in enumerate(top, 1):
        new = " 🆕 새로 들어왔습니다" if r["symbol"] in fresh_syms else ""
        fit = r["fit"]
        shape = ("자로 그은 듯 꾸준히 오르는 모양" if fit >= 0.8 else
                 "대체로 꾸준히 오르는 모양" if fit >= 0.6 else "오르긴 하지만 들쭉날쭉한 모양")
        stack = " 이동평균선도 정배열 상태입니다." if r.get("ema_stacked") else ""
        units.append([
            f"*{i}. {r['base']}*{new} — 종합점수 *{r['score']:.0f}점*/100",
            f"• BTC 대비 한 달 `{r['rs30']:+.1f}%`, 일주일 `{r['rs7']:+.1f}%` 더 올랐습니다.",
            f"• 추세 R² {fit:.2f}로 {shape}이며, 한 달 최고가의 {r['near_high'] * 100:.0f}% 위치까지 올라와 있습니다.{stack}",
            f"• 최근 2주 MDD는 {abs(r['mdd14']):.0f}%입니다. 작을수록 안정적인 흐름입니다.",
            f"• 현재가 {_price(r['price'])} · 하루 {r['chg24']:+.1f}% · 하루 거래대금 ${_fmt_qvol(r['qvol'])} · "
            f"<https://www.binance.com/en/trade/{r['base']}_BTC|비트코인 기준 차트 보기>",
        ])
    footer = [f"{when or now_kst()} 기준 · 거래가 많은 상위 {cfg.get('universe_top_n')}개 코인 중 {th:.0f}점 이상만 담았습니다.",
              "_점수는 비트코인보다 더 오른 정도 + 꾸준함 + 고점 근접도 − 출렁임으로 계산합니다. "
              "매수 추천이 아니라 관찰 목록입니다._"]
    msg = Msg("trend", f"BTC 대비 우상향 코인 {n}개", units, footer,
              summary=f"📈 BTC 대비 우상향 코인 {n}종목")
    lead = next((r for r in top if len(r.get("ratio_tail") or []) >= 8), None)
    if lead:
        tail = lead["ratio_tail"]
        base0 = tail[0] or 1.0
        norm = [v / base0 * 100 for v in tail]
        hours = (len(tail) - 1) * 4
        msg.photo = charts.line_chart_url(
            norm, f"{lead['base']}/BTC ratio, 4h bars (start=100)",
            first_label=f"-{hours // 24}d" if hours >= 24 else f"-{hours}h", color="rgb(46,125,50)")
        if msg.photo:
            msg.caption = (f"<b>📈 {lead['base']}의 BTC 대비 비율 추이</b>\n"
                           f"최근 약 {max(1, hours // 24)}일(4시간봉), 시작점을 100으로 맞춘 그래프입니다.")
    return msg


def compose_trend(con, respect_cooldown: bool = True, preview: bool = False
                  ) -> tuple[Msg | None, list[tuple[str, str]], dict]:
    """preview=True: 쿨다운 무시 + 기준 점수 넘는 코인이 없으면 상위 3개로 레이아웃 확인."""
    cfg = CFG.section("btc_trend")
    if not cfg.get("enabled", True):
        return None, [], {"skipped": True}

    rows = btc_trend.scan(cfg, workers=int(CFG.get("scan.max_workers", 8)))
    th = float(cfg.get("score_threshold", 65))
    cool = int(cfg.get("cooldown_min", 720))
    top = [r for r in rows if r["score"] >= th][: int(cfg.get("max_alerts", 8))]
    sample = False
    if not top and preview and rows:
        top, sample = rows[:3], True
    if not top:
        return None, [], {"count": 0, "sent": False}

    fresh = [r for r in top if not store.recently_alerted(con, "trend", r["symbol"], cool)]
    if not fresh and respect_cooldown:
        return None, [], {"count": len(top), "sent": False, "reason": "쿨다운"}

    msg = build_trend_msg(top, {r["symbol"] for r in fresh}, cfg, th)
    marks = [(r["symbol"], f"{r['score']}") for r in fresh]
    info = {"count": len(top), "new": len(fresh)}
    if sample:
        info["sample"] = f"기준 {th:.0f}점 이상 코인이 없어 점수 상위 3개로 대체"
    return msg, marks, info


def run_btc_trend(con) -> dict:
    msg, marks, info = compose_trend(con)
    return _finish("trend", con, msg, marks, info)


# ─────────────────────────── 2. 코인 급등 ───────────────────────────

def build_pump_msg(items: list[dict], when: str = "", translate: bool = True) -> Msg:
    units: list[list[str]] = []
    n_up = sum(1 for it in items if it["raw"].get("direction", "up") == "up")
    intro = f"지금 급하게 움직이는 코인 *{len(items)}개*를 알려드립니다."
    if n_up != len(items):
        intro += f" 급등 {n_up}개, 급락 {len(items) - n_up}개입니다."
    units.append([intro])
    for it in items:
        h, rsn = it["raw"], it["reason"]
        up = h.get("direction", "up") == "up"
        arrow = "🚀" if up else "🔻"
        ex = "비트겟" if h.get("market") == "bitget" else "바이낸스"
        chg_bits = []
        for label, key in (("5분 만에", "chg5m"), ("15분 만에", "chg15m"), ("1시간 동안", "chg1h"),
                           ("2시간 동안", "chg2h"), ("하루 동안", "chg24h")):
            v = h.get(key) or 0
            if abs(v) >= 0.5:
                chg_bits.append(f"{label} `{v:+.1f}%`")
        move = "올랐습니다" if up else "빠졌습니다"
        vol = ""
        if (h.get("vol_x") or 0) >= 2:
            vol = (f" 거래량도 평소의 *{h['vol_x']:.1f}배*로, "
                   f"{'매수세' if up else '매도세'}가 크게 몰린 것으로 보입니다.")
        unit = [f"{arrow} *{h['base']}* — {it.get('llm') or rsn['headline']}",
                f"• {', '.join(chg_bits) or '짧은 시간에 크게'} {move}.{vol}",
                f"• 현재가 {_price(h['price'])} ({ex}) · 하루 거래대금 ${_fmt_qvol(h['qvol'])}"]
        unit += _why_header(rsn, up)
        unit += _evidence_lines(rsn, translate)
        if up and (h.get("off_high") or 0) <= -5:
            unit.append(f"• 최근 고점보다 `{h['off_high']:.1f}%` 내려온 자리입니다. 급등 뒤 되밀리는 중일 수 있습니다.")
        chart = (f"https://www.bitget.com/spot/{h['base']}USDT" if h.get("market") == "bitget"
                 else f"https://www.binance.com/en/trade/{h['base']}_USDT")
        unit.append(f"• <{chart}|차트 보기> · "
                    f"<https://www.coingecko.com/en/search?query={h['base']}|코인 정보>")
        units.append(unit)
    footer = ["_⚠️ 급등 직후에 따라 사면 꼭대기에 물리기 쉽습니다. 이유가 확실한 종목 위주로 지켜보시는 것이 좋아 보입니다._",
              f"{when or now_kst()} 기준 · 자동 스캔"]
    msg = Msg("pump", f"지금 급하게 움직이는 코인 {len(items)}개", units, footer,
              summary="🚀 코인 급등 감지 " + ", ".join(i["key"] for i in items))

    lead = next((it["raw"] for it in items if len(it["raw"].get("closes") or []) >= 8), None)
    if lead:
        closes = lead["closes"]
        up = lead.get("direction", "up") == "up"
        mins = (len(closes) - 1) * 5
        msg.photo = charts.line_chart_url(
            closes, f"{lead['base']}/USDT 5m price (last {mins // 60}h)",
            first_label=f"-{mins // 60}h", color="rgb(229,57,53)" if up else "rgb(30,136,229)")
        if msg.photo:
            msg.caption = (f"<b>{'🚀' if up else '🔻'} {lead['base']} 최근 {max(1, mins // 60)}시간 가격 흐름</b>\n"
                           f"5분봉 기준 그래프입니다.")
    return msg


def _sample_pump_hits(cfg: dict, n: int = 3) -> list[dict]:
    """미리보기 전용: 감지된 급등이 없을 때 24시간 상승률 상위 코인으로 레이아웃을 확인한다."""
    uni = binance.usdt_universe(binance.ticker_24hr(), float(cfg.get("min_quote_volume_usdt", 800_000)))
    uni.sort(key=lambda t: t["_chg24"], reverse=True)
    uni = uni[:n]
    kl_map = binance.klines_many([t["symbol"] for t in uni], "5m", 100)
    out = []
    for t in uni:
        kl = kl_map.get(t["symbol"]) or []
        qv = binance.quote_volumes(kl)
        vx = 0.0
        if len(qv) > 10:
            vx = round((sum(qv[-3:]) / 3) / ((sum(qv[:-3]) / (len(qv) - 3)) or 1e-9), 1)
        out.append({"market": "binance", "symbol": t["symbol"], "base": t["_base"], "price": t["_last"],
                    "chg5m": 0.0, "chg15m": 0.0, "chg1h": 0.0, "chg24h": round(t["_chg24"], 2),
                    "qvol": t["_qvol"], "vol_x": vx, "direction": "up", "cold_start": True,
                    "closes": binance.closes(kl)[-48:]})
    return out


def compose_pump(con, ctx: reason.MarketContext, respect_cooldown: bool = True, preview: bool = False
                 ) -> tuple[Msg | None, list[tuple[str, str]], dict]:
    """preview=True: 쿨다운 무시 + 감지 0건이면 24h 상승률 상위 3개로 대체."""
    cfg = CFG.section("pump_crypto")
    if not cfg.get("enabled", True):
        return None, [], {"skipped": True}

    workers = int(CFG.get("scan.max_workers", 8))
    hits = pump.collect_and_detect(con, cfg, workers=workers)

    try:
        bases = {t["_base"] for t in binance.usdt_universe(binance.ticker_24hr(), 0)}
    except Exception:  # noqa: BLE001
        bases = set()
    hits += pump.bitget_only(con, cfg, bases, workers=workers)

    cool = int(cfg.get("cooldown_min", 60))
    if respect_cooldown:
        fresh = [h for h in hits if not store.recently_alerted(con, "pump", h["symbol"], cool)]
    else:
        fresh = list(hits)
    fresh = fresh[: int(cfg.get("max_alerts", 6))]
    sample = False
    if not fresh and preview:
        fresh, sample = _sample_pump_hits(cfg), True
    if not fresh:
        return None, [], {"count": len(hits), "sent": False}

    for h in fresh:
        ctx.note_pump(h["base"])

    items = []
    for h in fresh:
        rsn = reason.explain_crypto(h, ctx)
        items.append({"key": h["base"], "title": rsn["headline"], "reason": rsn, "raw": h})

    if CFG.get("reason.use_llm", True):
        polished = reason.llm_summarize(items, CFG.get("reason.llm_model",
                                                       "gemini-2.5-flash-lite"))
        for it in items:
            if it["key"] in polished:
                it["llm"] = polished[it["key"]]

    msg = build_pump_msg(items)
    marks = [(h["symbol"], str(h.get("chg15m"))) for h in fresh]
    info = {"count": len(hits), "alerted": len(fresh)}
    if sample:
        info["sample"] = "감지된 급등이 없어 24시간 상승률 상위 3개로 대체"
    return msg, marks, info


def run_pump_crypto(con, ctx: reason.MarketContext, respect_cooldown: bool = True) -> dict:
    msg, marks, info = compose_pump(con, ctx, respect_cooldown=respect_cooldown)
    return _finish("pump", con, msg, marks, info)


# ─────────────────────────── 3. 주식 테마 추적 + 관찰 콜 ───────────────────────────
# 미국·한국을 따로 본다. 테마 대장주 → 2·3등 → 따라감 판정 → 관찰 콜(근거 3줄 + 무효 조건) → 결과 추적.

STOCK_NOTICE = "_이 알림은 매수·매도 추천이 아니라 관찰 대상 종목 목록입니다. 투자 판단과 책임은 본인에게 있습니다._"
MARKET_LABEL = {"US": "🇺🇸 미국", "KR": "🇰🇷 한국"}
STATUS_LABEL = {"hit": "✅ 적중", "miss": "❌ 빗나감", "pending": "⏳ 진행 중"}


def _px(market: str, p: float) -> str:
    if not p:
        return "-"
    return f"{p:,.0f}원" if market == "KR" else f"${p:,.2f}"


def _money(market: str, v: float) -> str:
    """거래대금·시가총액 표기. 한국은 억 원 단위 값, 미국은 달러 값."""
    if market == "KR":
        return f"{v / 10000:,.1f}조 원" if v >= 10000 else f"{v:,.0f}억 원"
    return f"${_fmt_qvol(v)}"


def _title(c: dict, translate: bool) -> str:
    return _ko(c["title"]) if translate else c["title"]


def _catalyst_lines(leader: dict, translate: bool) -> list[str]:
    cat = leader.get("catalyst") or []
    if not cat:
        return ["   ◦ 재료: 종목명이나 테마 키워드가 들어간 뉴스는 찾지 못했습니다. "
                "뚜렷한 재료 없이 수급만으로 오른 것일 수 있습니다."]
    out = []
    for c in cat[:2]:
        line = f"   ◦ 재료: {_title(c, translate)}"
        if c.get("url"):
            line += f" <{c['url']}|기사 보기>"
        out.append(line)
    return out


def _verdict_note(p: dict, v: str, s: dict) -> str:
    pchg, vx = p.get("chg") or 0, p.get("vol_x") or 0
    if v == "following":
        return f"대장주와 같은 방향으로 {pchg:+.1f}% 올랐고, 거래량도 평소의 {vx:.1f}배로 늘었습니다."
    if v == "overheated":
        return f"이미 {pchg:+.1f}%로 대장주만큼 올라, 지금 따라붙기에는 부담이 큰 자리입니다."
    extra = " 오르긴 했지만 거래량이 아직 붙지 않았습니다." if pchg >= s["follow_min_chg"] else ""
    return f"아직 {pchg:+.1f}%로 테마 움직임이 덜 반영됐습니다.{extra} 뒤늦게 따라오는지 지켜볼 만합니다."


def _call_lines(market: str, theme: dict, leader: dict, p: dict, s: dict, translate: bool) -> list[str]:
    cat = leader.get("catalyst") or []
    if cat:
        why1 = f"① 재료: 대장주 {leader['name']}에 「{_title(cat[0], translate)}」 소식이 나왔습니다."
    else:
        why1 = (f"① 재료: 뚜렷한 뉴스는 확인되지 않았고, 대장주 {leader['name']}에 거래대금 "
                f"{_money(market, leader.get('trade_value') or 0)}이 몰린 수급이 근거입니다.")
    why2 = f"② 연결고리: {p['name']} 역시 같은 '{theme['name']}' 테마로 묶여 있어 같은 재료에 함께 반응할 수 있습니다."
    if p.get("why"):
        why2 += f" 네이버 테마 편입 사유는 '{p['why'][:70].rstrip('. ')}'입니다."
    if leader.get("series") and p.get("series") and p.get("co_move") is not None:
        why2 += f" 오늘 장중 흐름의 상관도는 {p['co_move']:.2f}입니다."
    lchg, pchg = leader.get("chg") or 0, p.get("chg") or 0
    why3 = f"③ 숫자: 대장주 `{lchg:+.1f}%`, {p['name']} `{pchg:+.1f}%`로 격차가 {lchg - pchg:.1f}%p입니다."
    if p.get("vol_x"):
        why3 += f" 거래량은 평소의 {p['vol_x']:.1f}배입니다."
    if leader.get("mcap") and p.get("mcap"):
        why3 += f" 시가총액은 대장주의 {p['mcap'] / leader['mcap']:.1f}배입니다."
    return [f"📣 *관찰 콜* — {p['name']}",
            f"   {why1}", f"   {why2}", f"   {why3}",
            f"   ⛔ 무효 조건: {theme_follow.invalidation_text(leader, s, lambda x: _px(market, x))}",
            f"   🎯 결과 기준: 기준가 {_px(market, p.get('price') or 0)}에서 {s['hit_window_trading_days']}거래일 안에 "
            f"+{s['hit_pct']:g}% 이상 오르면 적중으로 기록합니다."]


def _theme_unit(b: dict, s: dict, translate: bool, show_calls: bool) -> list[str]:
    market, theme, leader = b["market"], b["theme"], b["leader"]
    head = f"🧩 *{theme['name']}* 테마"
    if theme.get("total"):
        head += f" · 종목 {theme['total']}개 중 {theme.get('rise', 0)}개 상승"
    if theme.get("chg") is not None:
        head += f" · 테마 평균 {theme['chg']:+.1f}%"
    tag = "" if market == "KR" else f" ({leader['symbol']})"
    limit_up = " 🔒상한가" if leader.get("limit_up") else ""
    mcap = f" · 시가총액 {_money(market, leader['mcap'])}" if leader.get("mcap") else ""
    lines = [head,
             f"👑 대장주 *{leader['name']}*{tag} `{leader['chg']:+.1f}%`{limit_up} · "
             f"거래대금 {_money(market, leader.get('trade_value') or 0)}{mcap}"]
    lines += _catalyst_lines(leader, translate)
    for rank, p in zip(("🥈 2등", "🥉 3등"), b["peers"]):
        ptag = "" if market == "KR" else f" ({p['symbol']})"
        lines.append(f"{rank} *{p['name']}*{ptag} `{p['chg']:+.1f}%` — {theme_follow.VERDICT_LABEL[p['verdict']]}")
        lines.append(f"   └ {_verdict_note(p, p['verdict'], s)}")
    if not b["peers"]:
        lines.append("• 같은 테마에서 비교할 종목을 찾지 못했습니다.")
    calls = [p for p in b["peers"] if p["symbol"] in b["calls"]] if show_calls else []
    if b["mom"]["weak"]:
        lines.append("⚠️ 대장주만 오르고 같은 테마 종목은 거의 따라오지 않아 테마 힘이 약합니다. "
                     "이번에는 관찰 콜을 내지 않습니다.")
    elif b.get("invalid"):
        lines.append(f"⚠️ 대장주가 이미 {b['invalid']} 상태라 이번에는 관찰 콜을 내지 않습니다.")
    elif show_calls and b["peers"]:
        skipped = [f"{p['name']}: {p.get('blocker') or '조건 미충족'}" for p in b["peers"]
                   if p["symbol"] not in b["calls"]]
        if skipped:
            lines.append("• 관찰 콜에서 제외했습니다 — " + " · ".join(skipped))
    for p in calls:
        lines += _call_lines(market, theme, leader, p, s, translate)
    return lines


def _results_unit(market: str, rows: list[dict]) -> list[str]:
    if not rows:
        return []
    out = [f"📊 *지난 관찰 콜 결과* ({MARKET_LABEL[market]})"]
    for c in rows:
        pct = c.get("result_pct")
        pct_txt = f"{pct:+.1f}%" if pct is not None else "-"
        name = c.get("name") or c["symbol"]
        if c["status"] == "pending":
            out.append(f"• {STATUS_LABEL['pending']} *{name}* 현재 {pct_txt} · 기준가 {_px(market, c['ref_price'])} · "
                       f"판정 마감 {markets.fmt_kst(c['deadline'])}")
        else:
            out.append(f"• {STATUS_LABEL.get(c['status'], c['status'])} *{name}* {pct_txt} · "
                       f"기준가 {_px(market, c['ref_price'])} · {markets.fmt_kst(c['ts'])} 콜 (대장주 {c.get('leader') or '-'})")
    return out


def build_stock_market_msg(market: str, sess: str, blocks: list[dict], results: list[dict], stats: dict,
                           s: dict, when: str = "", translate: bool = True, calls_allowed: bool = True,
                           preview_closed: bool = False) -> Msg:
    label = MARKET_LABEL[market]
    if blocks and preview_closed:
        intro = (f"{label} 시장은 지금 *{sess}* 상태입니다. 미리보기라서 마지막 거래일 기준 테마 흐름을 보여드리며, "
                 "실제 실행에서는 새 관찰 콜을 내지 않습니다.")
    elif blocks and calls_allowed:
        intro = (f"{label} 시장은 지금 *{sess}*입니다. 오늘 돈이 몰린 테마의 대장주와, "
                 "그 뒤를 따르는 2·3등 종목을 정리했습니다.")
    elif blocks:
        intro = (f"{label} 시장은 지금 *{sess}*입니다. 시세 기준이 정규장과 달라 테마 흐름만 보여드리고 "
                 "새 관찰 콜은 내지 않습니다.")
    elif sess == markets.CLOSED:
        intro = f"{label} 시장은 지금 장이 닫혀 있어 새 관찰 콜은 내지 않고, 지난 콜 결과만 정리했습니다."
    else:
        intro = f"{label} 시장에서 조건을 만족한 테마 급등이 없어 지난 콜 결과만 정리했습니다."
    units: list[list[str]] = [[intro]]
    for b in blocks:
        units.append(_theme_unit(b, s, translate, show_calls=calls_allowed or preview_closed))
    res = _results_unit(market, results)
    units.append(res or ["📊 *지난 관찰 콜 결과*", "• 아직 결과를 확인할 관찰 콜이 없습니다."])
    rate = (f"누적 적중률 {stats['hits']}/{stats['resolved']} ({stats['rate']:.0f}%)" if stats.get("resolved")
            else "누적 적중률은 판정이 끝난 콜이 생기면 집계합니다")
    footer = [STOCK_NOTICE,
              f"{rate} · 적중 기준: 콜 이후 {s['hit_window_trading_days']}거래일 안에 기준가 대비 +{s['hit_pct']:g}% 이상",
              f"{when or now_kst()} 기준 · {label} {sess}"]
    names = ", ".join(b["theme"]["name"] for b in blocks)
    msg = Msg("stock", f"{label} 테마 추적 · 관찰 콜", units, footer,
              summary=f"💹 {label} 테마 추적 · " + (names or "관찰 콜 결과"))

    lead = next((b for b in blocks if len(b["leader"].get("series") or []) >= 8), None)
    if lead:
        series = {f"Leader {lead['leader']['symbol']}": lead["leader"]["series"]}
        for tag, p in zip(("#2", "#3"), lead["peers"]):
            if len(p.get("series") or []) >= 8:
                series[f"{tag} {p['symbol']}"] = p["series"]
        if len(series) >= 2:
            msg.photo = charts.multi_line_chart_url(series, "Intraday change % (leader vs #2, #3)")
            if msg.photo:
                msg.caption = (f"<b>💹 {html.escape(lead['theme']['name'])} 테마 · 대장주와 2·3등 장중 등락률</b>\n"
                               "빨간 선이 대장주, 파란 선이 2등, 회색 선이 3등입니다.")
    return msg


def _block(market: str, theme: dict, leader: dict, peers: list[dict], members: list[dict], s: dict,
           offline: bool = False) -> dict:
    for p in peers:
        p["verdict"] = theme_follow.verdict(leader, p, s)
    mom = theme_follow.momentum(leader, members, s, breadth=theme.get("breadth"))
    for p in peers:
        p["blocker"] = theme_follow.call_blocker(leader, p, p["verdict"], mom, s)
    try:
        leader["catalyst"] = reason.stock_catalyst(leader, theme.get("keywords") or [], offline=offline)
    except Exception:  # noqa: BLE001
        leader["catalyst"] = []
    return {"market": market, "key": f"theme:{market}:{theme['id']}:{leader['symbol']}", "theme": theme,
            "leader": leader, "peers": peers, "mom": mom, "invalid": theme_follow.leader_invalid(leader, s),
            "calls": [p["symbol"] for p in peers if not p["blocker"]]}


def _kr_blocks(cfg: dict, s: dict) -> list[dict]:
    try:
        movers = {r["code"] for r in stock_pump.kr(cfg.get("kr", {}))}   # 기존 급등주(상승률·거래대금 기준)
    except Exception:  # noqa: BLE001
        movers = set()
    cands = sorted([t for t in themes_src.kr_theme_list() if t["total"] >= 4],
                   key=lambda t: t["chg"], reverse=True)[: int(s["kr_theme_scan"])]
    details = themes_src.many(lambda t: themes_src.kr_theme_members(t["no"]), cands)
    scored = []
    for d in details:
        if not d:
            continue
        leader = theme_follow.pick_leader(d["members"], s, "KR")
        if not leader or (movers and leader["code"] not in movers):
            continue
        scored.append((d["chg"] + math.log10(1 + leader["trade_value"]) * 2, d, leader))
    scored.sort(key=lambda x: x[0], reverse=True)
    blocks: list[dict] = []
    used: set[str] = set()
    for _score, d, leader in scored:
        if leader["code"] in used:
            continue
        used.add(leader["code"])
        peers = theme_follow.rank_followers(leader, d["members"], s, "KR", n=2)
        rows = [leader] + peers
        for r, extra in zip(rows, themes_src.many(lambda r: themes_src.kr_daily(r["code"]), rows)):
            for k in ("open", "high", "vol_x"):
                if extra and extra.get(k):
                    r[k] = extra[k]
        blocks.append(_block("KR", d, leader, peers, d["members"], s))
        if len(blocks) >= int(s["max_themes"]):
            break
    if blocks:  # 첫 테마만 장중 차트용 1분봉
        b = blocks[0]
        rows = [b["leader"]] + b["peers"]
        for r, ser in zip(rows, themes_src.many(
                lambda r: themes_src.kr_intraday_pct(r["code"], r["price"], r["chg"]), rows)):
            r["series"] = ser or []
    return blocks


def _us_blocks(cfg: dict, s: dict) -> list[dict]:
    movers = stock_pump.us(cfg.get("us", {}))
    groups: dict[str, list[dict]] = {}
    for r in movers:
        tid = themes_src.us_theme_of(r["symbol"]) or themes_src.us_theme_of(
            r["symbol"], themes_src.us_industry(r["symbol"]))
        if tid:
            groups.setdefault(tid, []).append(r)
    blocks: list[dict] = []
    for tid, rs in sorted(groups.items(), key=lambda kv: max(x["chg_eff"] for x in kv[1]), reverse=True):
        t = themes_src.US_THEMES[tid]
        by_sym = {x["symbol"]: x for x in rs}
        syms = list(dict.fromkeys(t["members"] + list(by_sym)))
        live = themes_src.us_spark(syms)
        members = []
        for sym in syms:
            lv, mv = live.get(sym), by_sym.get(sym)
            if not lv and not mv:
                continue
            members.append({
                "market": "US", "symbol": sym,
                "name": ((mv or {}).get("name") or (lv or {}).get("name") or sym)[:28],
                "price": lv["price"] if lv else mv["price"],
                "chg": lv["chg"] if lv else mv["chg_eff"],
                "trade_value": lv["trade_value"] if lv else mv["price"] * (mv.get("volume") or 0),
                "mcap": (mv or {}).get("mcap") or 0, "vol_x": (mv or {}).get("vol_x") or 0,
                "open": (lv or {}).get("open") or 0, "high": (lv or {}).get("high") or 0,
                "series": (lv or {}).get("series") or [], "url": f"https://finance.yahoo.com/quote/{sym}"})
        leader = theme_follow.pick_leader(members, s, "US")
        if not leader:
            continue
        peers = theme_follow.rank_followers(leader, members, s, "US", n=2)
        need = [r for r in [leader] + peers if not r.get("vol_x")]
        for r, vx in zip(need, themes_src.many(lambda r: themes_src.us_vol_x(r["symbol"]), need)):
            r["vol_x"] = vx or 0.0
        theme = {"id": tid, "name": t["name"], "keywords": t["keywords"], "total": len(members),
                 "rise": sum(1 for m in members if m["chg"] > 0), "breadth": None, "chg": None}
        blocks.append(_block("US", theme, leader, peers, members, s))
        if len(blocks) >= int(s["max_themes"]):
            break
    return blocks


def _resolve_calls(con, market: str, s: dict, now: float) -> int:
    """진행 중인 콜을 현재 시세로 판정. 새로 판정이 끝난(hit/miss) 개수를 돌려준다."""
    opens = store.open_calls(con, market)
    if not opens:
        return 0
    quotes: dict[str, tuple] = {}
    if market == "US":
        for sym, q in themes_src.us_spark([c["symbol"] for c in opens]).items():
            quotes[sym] = (q["price"], q["high"], markets.local_date("US", q["time"]) if q.get("time") else None)
    else:
        codes = list(dict.fromkeys(c["symbol"] for c in opens))
        for code, d in zip(codes, themes_src.many(themes_src.kr_daily, codes)):
            if d:
                qd = None
                try:
                    qd = datetime.strptime(str(d.get("date"))[:10], "%Y-%m-%d").date()
                except ValueError:
                    pass
                quotes[code] = (d.get("last"), d.get("high"), qd)
    done = 0
    for c in opens:
        price, high, qdate = quotes.get(c["symbol"], (None, None, None))
        if price is None and now < c["deadline"]:
            continue
        use_high = bool(qdate) and qdate != markets.local_date(market, c["ts"])  # 콜 당일 고가는 반영 안 함
        status, pct, best = theme_follow.evaluate_call(c, price, high, now, use_high)
        store.update_call(con, c["id"], status=status, result_pct=pct, best_price=best, last_price=price, now=now)
        done += status != "pending"
    return done


def compose_stock(con, respect_cooldown: bool = True, preview: bool = False) -> tuple[list[dict], dict]:
    """시장별 작업 [{market, msg, marks, calls, report_ids}] 과 요약 info. 전송·기록은 하지 않는다.
    preview=True: 쿨다운 무시 + 장이 닫힌 시장도 테마 흐름을 보여준다(콜은 기록하지 않음)."""
    cfg = CFG.section("pump_stock")
    if not cfg.get("enabled", True):
        return [], {"skipped": True}
    s = theme_follow.settings(cfg.get("theme"))
    cool = int(cfg.get("cooldown_min", 720))
    now = time.time()
    jobs: list[dict] = []
    info: dict = {}
    for market in ("US", "KR"):
        if not cfg.get(market.lower(), {}).get("enabled", True):
            continue
        sess = markets.session(market, now)
        can_call = (sess in s["us_call_sessions"]) if market == "US" else sess == "정규장"
        minfo: dict = {"session": sess, "new_calls_allowed": can_call}
        try:
            minfo["resolved"] = _resolve_calls(con, market, s, now)
        except Exception as e:  # noqa: BLE001
            minfo["resolve_error"] = f"{type(e).__name__}: {e}"
        blocks: list[dict] = []
        if sess != markets.CLOSED or preview:
            try:
                blocks = _us_blocks(cfg, s) if market == "US" else _kr_blocks(cfg, s)
            except Exception as e:  # noqa: BLE001
                minfo["error"] = f"{type(e).__name__}: {e}"
        fresh = [b for b in blocks
                 if not respect_cooldown or not store.recently_alerted(con, "stock", b["key"], cool)]
        open_syms = {c["symbol"] for c in store.open_calls(con, market)}
        for b in fresh:  # 이미 진행 중인 콜이 있는 종목은 다시 콜하지 않는다
            for p in b["peers"]:
                if p["symbol"] in b["calls"] and p["symbol"] in open_syms:
                    p["blocker"] = "이미 진행 중인 콜 있음"
            b["calls"] = [x for x in b["calls"] if x not in open_syms]
        results = store.calls_for_report(con, market)
        unreported = [c for c in results if c["status"] != "pending" and not c["reported"]]
        minfo.update(themes=len(blocks), fresh=len(fresh), new_results=len(unreported))
        info[market] = minfo
        if not fresh and not unreported and not preview:
            continue
        msg = build_stock_market_msg(market, sess, fresh, results, store.call_stats(con, market), s,
                                     calls_allowed=can_call, preview_closed=preview and not can_call)
        calls = []
        deadline = markets.hit_deadline(market, now, int(s["hit_window_trading_days"]))
        for b in fresh:
            for p in b["peers"]:
                if p["symbol"] in b["calls"]:
                    calls.append({"market": market, "symbol": p["symbol"], "name": p["name"],
                                  "theme": b["theme"]["name"], "leader": b["leader"]["name"],
                                  "ref_price": p["price"], "target_pct": s["hit_pct"], "deadline": deadline})
        jobs.append({"market": market, "msg": msg, "marks": [(b["key"], f"{b['leader']['chg']}") for b in fresh],
                     "calls": calls if (can_call or preview) else [],
                     "report_ids": [c["id"] for c in unreported]})
        minfo["calls"] = len(calls) if can_call else 0
    return jobs, info


def run_pump_stock(con) -> dict:
    jobs, info = compose_stock(con)
    sent = False
    for job in jobs:
        res = _finish("stock", con, job["msg"], job["marks"], {})
        if res.get("sent"):
            sent = True
            for c in job["calls"]:
                store.add_call(con, **c)
            store.mark_reported(con, job["report_ids"])
            res["calls_recorded"] = len(job["calls"])
        info.setdefault(job["market"], {})["delivery"] = res
    info["sent"] = sent
    return info


# ─────────────────────────── 사이클 ───────────────────────────

def cycle(only: str = "all", verbose: bool = True, force: bool = False) -> dict:
    """force=True: 코인 급등 쿨다운을 무시하고 지금 감지된 것을 보낸다(수동 실행 전용)."""
    t0 = time.time()
    con = store.connect()
    store.prune(con, keep_hours=8)
    out: dict = {"at": now_kst()}

    ctx = None
    if only in ("all", "pump"):
        try:
            ctx = reason.MarketContext.build(int(CFG.get("reason.news_lookback_hours", 24)))
        except Exception:  # noqa: BLE001
            ctx = reason.MarketContext()

    for name, fn in (
        ("pump", lambda: run_pump_crypto(con, ctx, respect_cooldown=not force)),
        ("trend", lambda: run_btc_trend(con)),
        ("stock", lambda: run_pump_stock(con)),
    ):
        if only not in ("all", name):
            continue
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": f"{type(e).__name__}: {e}"}
            if verbose:
                traceback.print_exc()

    out["elapsed"] = round(time.time() - t0, 1)
    con.close()
    return out

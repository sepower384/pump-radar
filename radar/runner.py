"""한 사이클 실행 — 스캔 → 이유 추론 → 슬랙 전송."""
from __future__ import annotations

import re
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import store
from .config import CFG
from .engine import btc_trend, pump, reason, stock_pump
from .http import get_json
from .notify import send
from .sources import binance

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


def _blocks(header: str, body_lines: list[str], footer: str = "") -> list:
    blocks: list = [{"type": "header", "text": {"type": "plain_text", "text": header[:150],
                                                "emoji": True}}]
    chunk: list[str] = []
    size = 0
    for line in body_lines:
        if size + len(line) > 2700:
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


CONF_KR = {"높음": "꽤 확실해요", "보통": "그럴듯해요", "낮음": "추정이에요"}


def _why_header(rsn: dict, indent: str = "        ") -> list[str]:
    if not rsn.get("evidence"):
        return [f"{indent}🤔 *왜 올랐을까?* 아직 뚜렷한 뉴스나 이유를 못 찾았어요 — 이유 없는 급등은 더 조심하세요"]
    conf = rsn.get("confidence") or ""
    return [f"{indent}🤔 *왜 올랐을까?* (이유 확신도: {CONF_KR.get(conf, conf)})"]


def _size_word(mcap: float) -> str:
    if mcap <= 0:
        return ""
    b = mcap / 1e9
    kind = "초소형주(변동 매우 큼)" if b < 0.3 else "소형주" if b < 2 else "중형주" if b < 10 else "대형주"
    return f"회사 규모 ${b:.2f}B · {kind}"


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


def _evidence_lines(rsn: dict, indent: str = "        ") -> list[str]:
    out = []
    for e in rsn["evidence"]:
        body = _ko(e["text"]) if e.get("icon") == "📰" else e["text"]
        txt = f"{indent}{e.get('icon', '•')} *{e['tag']}* — {body}"
        if e.get("url"):
            txt += f" <{e['url']}|기사 보기>"
        out.append(txt)
    return out


# ─────────────────────────── 1. BTC 우상향 ───────────────────────────

def run_btc_trend(con) -> dict:
    cfg = CFG.section("btc_trend")
    if not cfg.get("enabled", True):
        return {"skipped": True}

    rows = btc_trend.scan(cfg, workers=int(CFG.get("scan.max_workers", 8)))
    th = float(cfg.get("score_threshold", 65))
    cool = int(cfg.get("cooldown_min", 720))
    top = [r for r in rows if r["score"] >= th][: int(cfg.get("max_alerts", 8))]
    if not top:
        return {"count": 0, "sent": False}

    fresh = [r for r in top if not store.recently_alerted(con, "trend", r["symbol"], cool)]
    if not fresh:
        return {"count": len(top), "sent": False, "reason": "쿨다운"}

    lines: list[str] = [
        "*비트코인보다 더 잘 오르고 있는 코인*들이에요.",
        "_비트코인이 오를 때 같이 더 많이 오르고, 빠질 때 덜 빠지는 '체력 좋은 코인'을 고른 거예요._", ""]
    for i, r in enumerate(top, 1):
        new = " 🆕 새로 들어옴" if r in fresh else ""
        fit = r["fit"]
        shape = ("자로 그은 듯 꾸준히 오르는 모양" if fit >= 0.8 else
                 "대체로 꾸준히 오르는 모양" if fit >= 0.6 else "오르긴 하는데 들쭉날쭉한 모양")
        lines.append(f"*{i}. {r['base']}*{new}  —  종합점수 *{r['score']:.0f}점*/100")
        lines.append(f"        👉 비트코인과 비교하면 한 달 동안 `{r['rs30']:+.1f}%`, 일주일 동안 `{r['rs7']:+.1f}%` 더 올랐어요")
        lines.append(f"        👉 {shape}이고, 한 달 최고가의 {r['near_high'] * 100:.0f}% 위치까지 올라와 있어요")
        lines.append(f"        👉 최근 2주 중 제일 크게 빠졌을 때가 {abs(r['mdd14']):.0f}%였어요 (작을수록 안정적)")
        lines.append(f"        💵 지금 {_price(r['price'])} · 하루 {r['chg24']:+.1f}% · 하루 거래대금 ${_fmt_qvol(r['qvol'])} · "
                     f"<https://www.binance.com/en/trade/{r['base']}_BTC|비트코인 기준 차트 보기>")
        lines.append("")

    footer = (f"{now_kst()} 기준 · 거래 많은 상위 {cfg.get('universe_top_n')}개 코인 중 {th:.0f}점 이상만\n"
              "_점수 = 비트코인보다 더 오른 정도 + 꾸준함 + 고점 근처인지 − 출렁임. 매수 추천이 아니라 관찰 목록이에요._")

    text = f"📈 BTC 대비 우상향 코인 {len(top)}종목"
    backend = send("trend", text + "\n" + "\n".join(lines), _blocks("📈 BTC 대비 우상향 코인",
                                                                    lines, footer))
    for r in fresh:
        store.mark_alerted(con, "trend", r["symbol"], f"{r['score']}")
    return {"count": len(top), "new": len(fresh), "sent": True, "backend": backend}


# ─────────────────────────── 2. 코인 급등 ───────────────────────────

def run_pump_crypto(con, ctx: reason.MarketContext) -> dict:
    cfg = CFG.section("pump_crypto")
    if not cfg.get("enabled", True):
        return {"skipped": True}

    workers = int(CFG.get("scan.max_workers", 8))
    hits = pump.collect_and_detect(con, cfg, workers=workers)

    try:
        bases = {t["_base"] for t in binance.usdt_universe(binance.ticker_24hr(), 0)}
    except Exception:  # noqa: BLE001
        bases = set()
    hits += pump.bitget_only(con, cfg, bases)

    cool = int(cfg.get("cooldown_min", 60))
    fresh = [h for h in hits if not store.recently_alerted(con, "pump", h["symbol"], cool)]
    fresh = fresh[: int(cfg.get("max_alerts", 6))]
    if not fresh:
        return {"count": len(hits), "sent": False}

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

    lines: list[str] = []
    for it in items:
        h, rsn = it["raw"], it["reason"]
        arrow = "🚀" if h.get("direction") == "up" else "🔻"
        ex = "비트겟" if h.get("market") == "bitget" else "바이낸스"
        chg_bits = []
        for label, key in (("5분 만에", "chg5m"), ("15분 만에", "chg15m"), ("1시간 동안", "chg1h"),
                           ("하루 동안", "chg24h")):
            v = h.get(key) or 0
            if abs(v) >= 0.5:
                chg_bits.append(f"{label} `{v:+.1f}%`")
        move = "올랐어요" if h.get("direction") == "up" else "빠졌어요"
        vol = ""
        if (h.get("vol_x") or 0) >= 2:
            vol = f" 거래량도 평소의 *{h['vol_x']:.1f}배*로 사람들이 확 몰렸어요."

        lines.append(f"{arrow} *{h['base']}* — {it.get('llm') or rsn['headline']}")
        lines.append(f"        👉 {', '.join(chg_bits)} {move}.{vol}")
        lines.append(f"        💵 지금 {_price(h['price'])} ({ex}) · 하루 거래대금 ${_fmt_qvol(h['qvol'])}")
        lines += _why_header(rsn)
        lines += _evidence_lines(rsn, indent="            ")
        lines.append(f"        🔗 <https://www.binance.com/en/trade/{h['base']}_USDT|차트 보기> · "
                     f"<https://www.coingecko.com/en/search?query={h['base']}|코인 정보>")
        lines.append("")
    lines.append("_⚠️ 급등 직후 따라 사면 꼭대기에 물리기 쉬워요. 이유가 확실한 것 위주로 지켜보세요._")

    text = "🚨 코인 급등 감지 " + ", ".join(i["key"] for i in items)
    backend = send("pump", text + "\n" + "\n".join(lines),
                   _blocks(f"🚨 지금 급하게 움직이는 코인 {len(items)}개", lines, f"{now_kst()} 기준 · 자동 스캔"))
    for h in fresh:
        store.mark_alerted(con, "pump", h["symbol"], str(h.get("chg15m")))
    return {"count": len(hits), "sent": True, "alerted": len(fresh), "backend": backend}


# ─────────────────────────── 3. 주식 급등 ───────────────────────────

def run_pump_stock(con) -> dict:
    cfg = CFG.section("pump_stock")
    if not cfg.get("enabled", True):
        return {"skipped": True}

    cool = int(cfg.get("cooldown_min", 240))
    groups: list[tuple[str, list[dict]]] = []
    for label, rows, limit in (
        ("🇺🇸 미국", stock_pump.us(cfg.get("us", {})), int(cfg.get("us", {}).get("max_alerts", 6))),
        ("🇰🇷 한국", stock_pump.kr(cfg.get("kr", {})), int(cfg.get("kr", {}).get("max_alerts", 6))),
    ):
        fresh = [r for r in rows
                 if not store.recently_alerted(con, "stock", f"{r['market']}:{r['symbol']}", cool)]
        if fresh:
            groups.append((label, fresh[:limit]))

    if not groups:
        return {"sent": False}

    items = []
    for _label, rows in groups:
        for r in rows:
            rsn = reason.explain_stock(r)
            items.append({"key": r["symbol"], "title": r.get("name", ""), "reason": rsn})
            r["_rsn"] = rsn

    if CFG.get("reason.use_llm", True):
        polished = reason.llm_summarize(items, CFG.get("reason.llm_model",
                                                       "gemini-2.5-flash-lite"))
    else:
        polished = {}

    lines: list[str] = []
    for label, rows in groups:
        lines.append(f"*{label}*")
        for r in rows:
            rsn = r["_rsn"]
            if r["market"] == "US":
                head = f"🚀 *{r.get('name', '')[:28] or r['symbol']}* ({r['symbol']}) `{r['chg_eff']:+.1f}%`"
                when = {"프리마켓": "정규장 열리기 전(프리마켓)에", "애프터마켓": "장 마감 후(애프터마켓)에"}.get(
                    r["session"], "오늘 정규장에서")
                vx = r.get("vol_x") or 0
                vol_txt = f" 거래량은 평소의 {vx:.1f}배예요." if vx >= 1.5 else ""
                sub = [f"        👉 {when} {abs(r['chg_eff']):.1f}% 올랐어요.{vol_txt}",
                       f"        💵 지금 ${r['price']:,.2f} · {_size_word(r.get('mcap') or 0)}"]
                link = f"        🔗 <{r['url']}|차트·기업정보 보기>"
            else:
                limit_up = r["chg"] >= 29.5
                head = f"🚀 *{r['name']}* `{r['chg']:+.1f}%`" + (" 🔒상한가" if limit_up else "")
                sub = [f"        👉 오늘 {r['chg']:.1f}% 올랐어요" +
                       (" — 하루에 오를 수 있는 최대치(상한가)까지 갔어요." if limit_up else "."),
                       f"        💵 지금 {r['price']:,.0f}원 · 오늘 거래된 돈 {r['trade_value_eok']:,.0f}억원"]
                link = f"        🔗 <{r['url']}|네이버 금융에서 보기>"
            one = polished.get(r["symbol"])
            if one:
                head += f"\n        💬 {one}"
            lines.append(head)
            lines += sub
            ev = [e for e in rsn.get("evidence", []) if "상한가" not in e.get("tag", "")]
            lines += _why_header({**rsn, "evidence": ev})
            lines += _evidence_lines({**rsn, "evidence": ev}, indent="            ")
            lines.append(link)
            lines.append("")
    lines.append("_⚠️ 급등한 종목은 다음 날 크게 되돌리는 경우도 많아요. 뉴스 내용을 먼저 확인하세요._")

    text = "📊 주식 급등 감지"
    backend = send("stock", text + "\n" + "\n".join(lines),
                   _blocks("📊 오늘 크게 오른 주식", lines, f"{now_kst()} 기준"))
    for label, rows in groups:
        for r in rows:
            store.mark_alerted(con, "stock", f"{r['market']}:{r['symbol']}", str(r.get("chg_eff")))
    return {"sent": True, "backend": backend,
            "alerted": sum(len(r) for _, r in groups)}


# ─────────────────────────── 사이클 ───────────────────────────

def cycle(only: str = "all", verbose: bool = True) -> dict:
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
        ("pump", lambda: run_pump_crypto(con, ctx)),
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

"""한 사이클 실행 — 스캔 → 이유 추론 → 슬랙 전송."""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta, timezone

from . import store
from .config import CFG
from .engine import btc_trend, pump, reason, stock_pump
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


def _evidence_lines(rsn: dict, indent: str = "        ") -> list[str]:
    out = []
    for e in rsn["evidence"]:
        txt = f"{indent}{e.get('icon', '•')} *{e['tag']}* — {e['text']}"
        if e.get("url"):
            txt += f" <{e['url']}|↗>"
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
        "_비트코인 대비_ 우상향 중인 코인 (USDT 가격이 아니라 *코인/BTC 차트* 기준)", ""]
    for i, r in enumerate(top, 1):
        new = " `NEW`" if r in fresh else ""
        lines.append(
            f"*{i}. {r['base']}*{new}  —  점수 *{r['score']:.0f}*/100\n"
            f"        BTC대비 7일 `{r['rs7']:+.1f}%` · 30일 `{r['rs30']:+.1f}%` · "
            f"일평균기울기 `{r['slope_daily_pct']:+.2f}%`\n"
            f"        추세정합도 `{r['fit']:.2f}` · 30일고점대비 `{r['near_high'] * 100:.0f}%` · "
            f"14일MDD `{r['mdd14']:.0f}%`\n"
            f"        현재 {_price(r['price'])} · 24h {r['chg24']:+.1f}% · "
            f"거래대금 {_fmt_qvol(r['qvol'])} · "
            f"<https://www.binance.com/en/trade/{r['base']}_BTC|BTC페어 차트>")
        lines.append("")

    footer = (f"기준: {cfg.get('interval')}봉 {cfg.get('bars')}개 · "
              f"유동성 상위 {cfg.get('universe_top_n')}종목 · 점수 {th:.0f}점 이상 · {now_kst()} KST\n"
              "_점수는 BTC 대비 초과수익 + 추세 기울기/정합도 + 고점근접 + 정배열 − 변동성 페널티_")

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
        for label, key in (("5분", "chg5m"), ("15분", "chg15m"), ("1시간", "chg1h"),
                           ("24시간", "chg24h")):
            v = h.get(key) or 0
            if abs(v) >= 0.5:
                chg_bits.append(f"{label} `{v:+.1f}%`")
        vol = f" · 거래량 *{h['vol_x']:.1f}배*" if (h.get("vol_x") or 0) >= 2 else ""

        lines.append(f"{arrow} *{h['base']}* — {it.get('llm') or rsn['headline']}")
        lines.append(f"        {' · '.join(chg_bits)}{vol}")
        lines.append(f"        {_price(h['price'])} · {ex} · 24h거래대금 {_fmt_qvol(h['qvol'])} "
                     f"· 신뢰도 *{rsn['confidence']}*")
        lines += _evidence_lines(rsn)
        lines.append(f"        <https://www.binance.com/en/trade/{h['base']}_USDT|차트> · "
                     f"<https://www.coingecko.com/en/search?query={h['base']}|코인게코>")
        lines.append("")

    text = "🚨 코인 급등 감지 " + ", ".join(i["key"] for i in items)
    backend = send("pump", text + "\n" + "\n".join(lines),
                   _blocks("🚨 코인 급등 감지", lines, f"{now_kst()} KST · 자동 스캔"))
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
                head = (f"🚀 *{r['symbol']}* ({r.get('name', '')[:28]}) `{r['chg_eff']:+.1f}%`"
                        f" · {r['session']}")
                vx = r.get("vol_x") or 0
                vol_txt = f"{r['volume'] / 1e6:.1f}M" + (f" ({vx:.1f}배)" if vx else "")
                sub = (f"        ${r['price']:,.2f} · 거래량 {vol_txt} · "
                       f"시총 ${(r.get('mcap') or 0) / 1e9:.2f}B")
                link = f"        <{r['url']}|야후 차트>"
            else:
                head = f"🚀 *{r['name']}* `{r['chg']:+.1f}%`"
                sub = (f"        {r['price']:,.0f}원 · 거래대금 {r['trade_value_eok']:,.0f}억")
                link = f"        <{r['url']}|네이버 금융>"
            one = polished.get(r["symbol"])
            if one:
                head += f"\n        💬 {one}"
            lines.append(head)
            lines.append(sub)
            lines += _evidence_lines(rsn)
            lines.append(link)
            lines.append("")

    text = "📊 주식 급등 감지"
    backend = send("stock", text + "\n" + "\n".join(lines),
                   _blocks("📊 주식 급등 감지", lines, f"{now_kst()} KST"))
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

"""급등탐정 정기 보고서 — 주간·월간·분기·연간 PDF.

원재료는 history(보낸 알림 기록)와 calls.json(관찰 콜 판정)이다.
코인 급등은 '알림 뒤 24시간 동안 실제로 어떻게 됐는지'를 봉 데이터로 채점해 성적표를 만든다.

    python run_once.py report week|month|quarter|year [--send] [--at 2026-09-21]
    python run_once.py reports            # 기한이 된 보고서를 만들어 보낸다(클라우드 2시간 주기)
    python run_once.py reports-due        # 기한이 된 보고서 목록만 출력
"""
from __future__ import annotations

import html
import json
import math
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import followup, history
from .config import DATA_DIR
from .history import KST
from .http import get_json, pmap
from .sources import binance, bitget

KINDS = ("week", "month", "quarter", "year")
KIND_KO = {"week": "주간", "month": "월간", "quarter": "분기", "year": "연간"}
SEND_HOUR = 9           # KST 이 시각 이후 첫 실행에서 지난 기간 보고서를 보낸다
SUCCESS_PCT = 5.0       # 알림 뒤 24시간 안에 +5% 이상 더 오르면 '추가 상승'
FADE_PCT = -10.0        # 24시간 뒤 알림가 대비 -10% 이하면 '되밀림'
OUT_DIR = DATA_DIR / "reports"
# 기록이 기간의 이 비율 이상을 덮을 때만 정기 발송 (2026-09-13 기록 시작 → 9월 월간은 발송, 3분기는 건너뜀)
MIN_COVERAGE = {"week": 0.85, "month": 0.5, "quarter": 0.5, "year": 0.25}

WEEKDAY = "월화수목금토일"

REASON_GROUP = [
    ("상장·공지", ("상장/공지", "거래소 상장")),
    ("뉴스·재료", ("뉴스", "파트너십", "ETF", "네트워크 업그레이드", "소각/바이백", "에어드랍",
               "규제/소송", "물량 언락", "해킹/보안사고", "고래 매집")),
    ("선물 자금", ("선물 자금유입", "숏 스퀴즈 의심", "롱 과열")),
    ("테마 순환매", ("테마 순환매",)),
    ("국내 수급", ("국내 매수세", "역프")),
    ("관심 급증", ("검색 급증",)),
    ("거래량만", ("거래량 폭증", "거래대금 급증")),
    ("시장 전체", ("시장 전체",)),
]
UNKNOWN = "원인 미확인"


def reason_group(tag: str) -> str:
    for name, tags in REASON_GROUP:
        if tag in tags:
            return name
    return UNKNOWN


# ─────────────────────────── 기간 ───────────────────────────
def _kst(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, KST)


def _month_start(d: datetime) -> datetime:
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_months(d: datetime, n: int) -> datetime:
    y, m = divmod(d.month - 1 + n, 12)
    return d.replace(year=d.year + y, month=m + 1, day=1)


def last_period(kind: str, at: datetime) -> dict:
    """at 시점 기준으로 '가장 최근에 끝난' 기간."""
    day0 = at.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "week":
        end = day0 - timedelta(days=day0.weekday())          # 이번 주 월요일 0시
        start = end - timedelta(days=7)
        iso = start.isocalendar()
        key = f"W-{iso[0]}-{iso[1]:02d}"
        last = end - timedelta(days=1)
        label = (f"{iso[0]}년 {iso[1]}주차 · {start:%m.%d}({WEEKDAY[start.weekday()]}) ~ "
                 f"{last:%m.%d}({WEEKDAY[last.weekday()]})")
    elif kind == "month":
        end = _month_start(day0)
        start = _add_months(end, -1)
        key = f"M-{start:%Y-%m}"
        label = f"{start.year}년 {start.month}월"
    elif kind == "quarter":
        cur_q_start = _month_start(day0).replace(month=(day0.month - 1) // 3 * 3 + 1)
        end = cur_q_start
        start = _add_months(end, -3)
        q = (start.month - 1) // 3 + 1
        key = f"Q-{start.year}-{q}"
        label = f"{start.year}년 {q}분기 ({start.month}~{start.month + 2}월)"
    elif kind == "year":
        end = day0.replace(month=1, day=1)
        start = end.replace(year=end.year - 1)
        key = f"Y-{start.year}"
        label = f"{start.year}년 한 해"
    else:
        raise ValueError(kind)
    return {"kind": kind, "key": key, "label": label, "start": start.timestamp(), "end": end.timestamp()}


def due(now: float | None = None) -> list[dict]:
    """보낼 때가 된(기간이 끝나고 SEND_HOUR 가 지났고, 아직 안 보낸) 보고서."""
    at = _kst(now or time.time())
    sent = history.sent_reports()
    first = history.first_ts()
    out = []
    for kind in KINDS:
        p = last_period(kind, at)
        if p["key"] in sent:
            continue
        if at < _kst(p["end"]) + timedelta(hours=SEND_HOUR):
            continue
        if first is None or first >= p["end"]:
            continue  # 기록이 시작되기 전 기간
        cover = (p["end"] - max(first, p["start"])) / (p["end"] - p["start"])
        if cover < MIN_COVERAGE[kind]:
            continue  # 기록이 기간 일부만 덮으면 빈약한 보고서가 나가므로 건너뛴다(9/7~13 주간 사례)
        out.append(p)
    return out


# ─────────────────────────── 사후 성과 ───────────────────────────
def _bars_after(market: str, symbol: str, ts: float, hours: int = 25) -> list[list]:
    t0 = int(ts * 1000)
    if market == "bitget":
        d = get_json(f"{bitget.BASE}/api/v2/spot/market/history-candles",
                     params={"symbol": symbol, "granularity": "1h",
                             "endTime": str(t0 + hours * 3600_000), "limit": str(hours + 1)}, timeout=20)
        rows = sorted(d.get("data") or [], key=lambda r: int(r[0]))
    else:
        rows = binance._spot("/api/v3/klines", params={"symbol": symbol, "interval": "1h",
                                                       "startTime": t0, "limit": hours + 1}, timeout=20)
    return [r for r in rows if int(r[0]) >= t0 - 3600_000]


def outcome(e: dict, now: float) -> dict | None:
    """알림 뒤 24시간: 최고 +%, 최저 %, 24시간 뒤 종가 %. 24시간이 안 지났으면 None."""
    if now - e["ts"] < 24 * 3600 or not e.get("price"):
        return None
    rows = _bars_after(e.get("market", "binance"), e.get("symbol") or f"{e['base']}USDT", e["ts"])
    if len(rows) < 12:
        return None
    p0 = float(e["price"])
    hi = max(float(r[2]) for r in rows)
    lo = min(float(r[3]) for r in rows)
    end_idx = min(len(rows) - 1, 24)
    return {"max24": round((hi / p0 - 1) * 100, 2), "min24": round((lo / p0 - 1) * 100, 2),
            "r24": round((float(rows[end_idx][4]) / p0 - 1) * 100, 2)}


def score_outcomes(events: list[dict], now: float | None = None, workers: int = 6) -> None:
    """events 에 'out' 을 채운다. 확정된 결과는 캐시에 남겨 다음 보고서에서 다시 받지 않는다."""
    now = now or time.time()
    cache = history.outcome_cache()
    todo = []
    for e in events:
        k = f"{e.get('market')}|{e.get('symbol') or e.get('base')}|{e['ts']}"
        if k in cache:
            e["out"] = cache[k]
        else:
            todo.append((k, e))
    res = pmap(lambda ke: outcome(ke[1], now), todo, workers=workers)
    for (k, e), o in zip(todo, res):
        if o:
            e["out"] = o
            cache[k] = o
    history.save_outcomes(cache)


# ─────────────────────────── 집계 ───────────────────────────
def _pct(a: int, b: int) -> float | None:
    return round(a / b * 100, 1) if b else None


def aggregate(period: dict, now: float | None = None, score: bool = True) -> dict:
    ev = history.load(period["start"], period["end"])
    try:
        skip = bitget.non_crypto_bases() | binance.STABLES
    except Exception:  # noqa: BLE001
        skip = set(binance.STABLES)
    pumps = [e for e in ev if e["t"] == "pump" and str(e.get("base", "")).upper() not in skip]
    ups = [e for e in pumps if e.get("direction", "up") == "up"]
    downs = [e for e in pumps if e.get("direction") == "down"]
    if score:
        score_outcomes(ups, now)
    for e in ups:
        e["group"] = reason_group(e.get("tag", ""))

    scored = [e for e in ups if e.get("out")]
    succ = [e for e in scored if e["out"]["max24"] >= SUCCESS_PCT]
    fade = [e for e in scored if e["out"]["r24"] <= FADE_PCT]
    known = [e for e in ups if e["group"] != UNKNOWN]

    groups = []
    for name in [g for g, _ in REASON_GROUP] + [UNKNOWN]:
        rows = [e for e in ups if e["group"] == name]
        if not rows:
            continue
        sc = [e for e in rows if e.get("out")]
        groups.append({
            "name": name, "n": len(rows), "scored": len(sc),
            "success": _pct(sum(e["out"]["max24"] >= SUCCESS_PCT for e in sc), len(sc)),
            "fade": _pct(sum(e["out"]["r24"] <= FADE_PCT for e in sc), len(sc)),
            "avg_r24": round(sum(e["out"]["r24"] for e in sc) / len(sc), 1) if sc else None,
        })
    groups.sort(key=lambda g: -g["n"])

    hours = Counter(_kst(e["ts"]).hour for e in pumps)
    weekdays = Counter(_kst(e["ts"]).weekday() for e in pumps)

    # 같은 코인이 여러 번 잡혔으면 가장 크게 오른 알림 하나만 순위에
    best_by_coin: dict[str, dict] = {}
    for e in ups:
        b = best_by_coin.get(e["base"])
        if not b or (e.get("chg24h") or 0) > (b.get("chg24h") or 0):
            best_by_coin[e["base"]] = e
    top = sorted(best_by_coin.values(), key=lambda e: -(e.get("chg24h") or 0))
    repeat = Counter(e["base"] for e in ups).most_common(8)
    follow = sorted(scored, key=lambda e: -e["out"]["max24"])
    faded = sorted(scored, key=lambda e: e["out"]["r24"])

    # 기간 안 흐름(주간=일별, 월간=주별, 분기·연간=월별)
    buckets: dict[str, dict] = defaultdict(lambda: {"n": 0, "scored": 0, "succ": 0})
    for e in ups:
        d = _kst(e["ts"])
        if period["kind"] == "week":
            b = f"{d:%m.%d}"
        elif period["kind"] == "month":
            b = f"{(d - timedelta(days=d.weekday())):%m.%d}주"
        else:
            b = f"{d.month}월"
        buckets[b]["n"] += 1
        if e.get("out"):
            buckets[b]["scored"] += 1
            buckets[b]["succ"] += e["out"]["max24"] >= SUCCESS_PCT
    trend = [{"label": k, **v, "rate": _pct(v["succ"], v["scored"])} for k, v in sorted(buckets.items())]

    # 주식
    stocks = [e for e in ev if e["t"] == "stock"]
    markets = {}
    for mk in ("US", "KR"):
        rows = [e for e in stocks if e.get("market") == mk]
        themes = Counter(e["theme"] for e in rows)
        leaders = sorted(rows, key=lambda e: -((e.get("leader") or {}).get("chg") or 0))
        seen, lead_rows = set(), []
        for e in leaders:
            nm = (e.get("leader") or {}).get("name")
            if not nm or nm in seen:
                continue
            seen.add(nm)
            lead_rows.append(e)
        markets[mk] = {"n": len(rows), "themes": themes.most_common(8), "leaders": lead_rows[:8]}
    calls = history.load_calls(period["start"], period["end"])
    done = [c for c in calls if c.get("status") in ("hit", "miss")]
    hits = [c for c in done if c["status"] == "hit"]

    fu_cache = followup.load_cache()
    return {
        "period": period,
        "generated": time.time(),
        "since": history.first_ts(),
        "followup": {
            "coin": followup.summary(start=period["start"], end=period["end"], cache=fu_cache, asset="coin"),
            "stock": followup.summary(start=period["start"], end=period["end"], cache=fu_cache, asset="stock"),
            "horizons": [{"h": h, "label": followup.hlabel(h)} for h in followup.horizons()],
        },
        "coin": {
            "alerts": len(pumps), "ups": len(ups), "downs": len(downs),
            "coins": len({e["base"] for e in pumps}),
            "known_rate": _pct(len(known), len(ups)),
            "scored": len(scored), "success": len(succ), "fade": len(fade),
            "success_rate": _pct(len(succ), len(scored)), "fade_rate": _pct(len(fade), len(scored)),
            "avg_max24": round(sum(e["out"]["max24"] for e in scored) / len(scored), 1) if scored else None,
            "groups": groups, "hours": dict(hours), "weekdays": dict(weekdays),
            "top": top, "repeat": repeat, "follow": follow[:5], "faded": faded[:5],
            "trend": trend,
            "bitget_share": _pct(sum(e.get("market") == "bitget" for e in ups), len(ups)),
        },
        "stock": {
            "markets": markets,
            "calls": calls, "calls_done": len(done), "calls_hit": len(hits),
            "hit_rate": _pct(len(hits), len(done)),
            "avg_result": round(sum(c.get("result_pct") or 0 for c in done) / len(done), 1) if done else None,
        },
    }


def insights(a: dict) -> list[str]:
    """숫자에서 바로 나오는 문장만 쓴다(지어내지 않는다). 합니다체."""
    c, s = a["coin"], a["stock"]
    kind = KIND_KO[a["period"]["kind"]]
    out = []
    if c["ups"]:
        out.append(f"이번 {kind} 급등 알림은 <b>{c['ups']}건</b>(코인 {c['coins']}종)이었고, "
                   f"그중 오른 이유가 확인된 비율은 <b>{c['known_rate']:.0f}%</b>입니다.")
    else:
        out.append(f"이번 {kind}에는 조건을 넘는 코인 급등이 없었습니다.")
    if c["scored"] >= 3:
        out.append(f"알림 뒤 24시간 안에 <b>+{SUCCESS_PCT:.0f}% 이상 더 오른 비율은 {c['success_rate']:.0f}%</b>, "
                   f"24시간 뒤 {abs(FADE_PCT):.0f}% 넘게 되밀린 비율은 {c['fade_rate']:.0f}%입니다.")
    fu = [x for x in (((a.get("followup") or {}).get("coin") or {}).get("stats") or []) if x.get("n", 0) >= 3]
    if fu:
        head = fu[0]
        tail = fu[-1]
        out.append(f"처음 포착한 가격에서 <b>{head['label']} 뒤 평균 {head['avg']:+.1f}%</b>"
                   + (f", <b>{tail['label']} 뒤 평균 {tail['avg']:+.1f}%</b>" if tail is not head else "")
                   + f"였고, 구간 안 최고가는 평균 {head['avg_hi']:+.1f}%까지 올랐습니다.")
    rated = [g for g in c["groups"] if g["scored"] >= 3 and g["avg_r24"] is not None]
    if len(rated) >= 2:
        best = max(rated, key=lambda g: g["avg_r24"])
        worst = min(rated, key=lambda g: g["avg_r24"])
        if best["name"] != worst["name"] and best["avg_r24"] >= 0:
            out.append(f"하루 뒤 성적은 <b>'{best['name']}'</b> 급등이 평균 {_fmt_pct(best['avg_r24'])}로 가장 좋았고, "
                       f"'{worst['name']}' 급등이 {_fmt_pct(worst['avg_r24'])}로 가장 나빴습니다.")
        elif best["name"] != worst["name"]:
            out.append(f"하루 뒤에는 모든 유형이 평균 마이너스였습니다. '{best['name']}'({_fmt_pct(best['avg_r24'])})이 "
                       f"가장 덜 빠졌고, <b>'{worst['name']}'({_fmt_pct(worst['avg_r24'])})</b>이 가장 많이 빠졌습니다.")
    if (c["scored"] >= 5 and (c["success_rate"] or 0) >= 50 and (c["fade_rate"] or 0) >= 40):
        out.append("알림 뒤 한 번 더 튀는 경우가 많았지만 하루 뒤에는 크게 되밀린 경우도 많았습니다. "
                   "<b>짧게 튀고 빠지는 흐름</b>이 우세했던 기간입니다.")
    unk = next((g for g in c["groups"] if g["name"] == UNKNOWN and g["scored"] >= 3), None)
    if unk and unk["fade"] is not None:
        out.append(f"이유 없이 오른 급등은 {unk['fade']:.0f}%가 하루 만에 크게 되밀렸습니다. "
                   "이유가 확인되지 않은 급등은 추격하지 않는 것이 좋아 보입니다.")
    if c["hours"]:
        h, n = max(c["hours"].items(), key=lambda kv: kv[1])
        out.append(f"급등이 가장 많이 잡힌 시간대는 <b>{int(h):02d}시대</b>({n}건)입니다.")
    if c["repeat"] and c["repeat"][0][1] >= 3:
        b, n = c["repeat"][0]
        out.append(f"<b>{b}</b>은 기간 중 {n}번 잡혀 가장 자주 급등한 코인입니다.")
    for mk, name in (("US", "미국"), ("KR", "한국")):
        th = s["markets"][mk]["themes"]
        if th:
            out.append(f"{name} 주식에서 가장 자주 불붙은 테마는 <b>{th[0][0]}</b>({th[0][1]}회)입니다.")
    if s["calls_done"]:
        out.append(f"주식 관찰 콜은 판정이 끝난 {s['calls_done']}건 중 {s['calls_hit']}건이 적중해 "
                   f"<b>적중률 {s['hit_rate']:.0f}%</b>입니다.")
    return out


def watchlist(a: dict) -> list[str]:
    c, s = a["coin"], a["stock"]
    out = []
    rep_coins = [f"{b}({n}회)" for b, n in c["repeat"] if n >= 2][:5]
    if rep_coins:
        out.append(f"<b>반복 급등 코인</b> · {', '.join(rep_coins)} — 여러 번 불붙은 코인은 재료가 이어지는지 확인해 볼 만합니다.")
    rated = [g for g in c["groups"] if g["scored"] >= 3 and g["avg_r24"] is not None]
    good = sorted([g for g in rated if g["avg_r24"] > -2 and (g["success"] or 0) >= 40], key=lambda g: -g["avg_r24"])
    spiky = [g for g in rated if g not in good and (g["success"] or 0) >= 50 and g["avg_r24"] <= -2]
    bad = [g for g in rated if g not in good and g not in spiky and g["avg_r24"] <= -2]
    if good:
        g = good[0]
        out.append(f"<b>성적이 좋았던 급등 유형</b> · '{g['name']}' (추가 상승 {g['success']:.0f}%, "
                   f"24시간 뒤 평균 {_fmt_pct(g['avg_r24'])}) — 같은 유형 알림을 우선해서 살펴보십시오.")
    if spiky:
        out.append("<b>짧게 튀고 빠진 유형</b> · " + ", ".join(
            f"'{g['name']}'(한 번 더 오름 {g['success']:.0f}% · 24시간 뒤 평균 {_fmt_pct(g['avg_r24'])})" for g in spiky)
            + " — 추가 상승이 나와도 오래 버티지 못했습니다. 들고 가기보다 짧게 보는 편이 맞아 보입니다.")
    if bad:
        out.append("<b>조심할 급등 유형</b> · " + ", ".join(
            f"'{g['name']}'(24시간 뒤 평균 {_fmt_pct(g['avg_r24'])})" for g in bad)
            + " — 알림 직후 추격하면 물리기 쉬운 유형입니다.")
    if c["hours"]:
        top_h = sorted(c["hours"].items(), key=lambda kv: -kv[1])[:3]
        out.append("<b>급등이 몰린 시간대</b> · " + ", ".join(f"{int(h):02d}시" for h, _ in top_h)
                   + " — 이 시간대 알림은 켜 두시는 것이 좋아 보입니다.")
    for mk, name in (("US", "미국"), ("KR", "한국")):
        th = [f"{t}({n}회)" for t, n in s["markets"][mk]["themes"][:3]]
        if th:
            out.append(f"<b>{name} 주도 테마</b> · {', '.join(th)} — 대장주 뒤 2·3등이 따라오는지 계속 추적합니다.")
    if not out:
        out.append("이번 기간에는 반복 신호가 뚜렷하지 않았습니다. 기록이 더 쌓이면 이 칸이 채워집니다.")
    return out


# ─────────────────────────── 그림(SVG) ───────────────────────────
def _e(x) -> str:
    return html.escape(str(x), quote=True)


def svg_hbars(rows: list[tuple[str, float, str]], width: int = 520, unit: str = "", max_v: float | None = None) -> str:
    """가로 막대. rows = [(라벨, 값, 보조문구)]."""
    if not rows:
        return '<div class="empty">기록이 없습니다</div>'
    bh, gap = 18, 8
    lw = max(70, min(118, 11 * max(len(r[0]) for r in rows) + 8))
    mv = max_v or max(v for _, v, _ in rows) or 1
    h = len(rows) * (bh + gap)
    parts = [f'<svg viewBox="0 0 {width} {h}" width="100%" role="img">']
    for i, (lab, v, note) in enumerate(rows):
        y = i * (bh + gap)
        w = max(2, (width - lw - 110) * (v / mv))
        parts.append(f'<text x="0" y="{y + 13}" class="lab">{_e(lab)}</text>')
        parts.append(f'<rect x="{lw}" y="{y}" width="{w:.1f}" height="{bh}" rx="5" class="bar{i % 3}"/>')
        parts.append(f'<text x="{lw + w + 6:.1f}" y="{y + 13}" class="val">{_e(f"{v:g}{unit}")}'
                     f'<tspan class="note"> {_e(note)}</tspan></text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_hours(hours: dict, width: int = 330) -> str:
    vals = [int(hours.get(h, hours.get(str(h), 0))) for h in range(24)]
    mv = max(vals) or 1
    bw, ch = width / 24, 110
    parts = [f'<svg viewBox="0 0 {width} {ch + 22}" width="100%" role="img">']
    for h, v in enumerate(vals):
        bh = ch * v / mv
        cls = "hbar hot" if v == mv and v else "hbar"
        parts.append(f'<rect x="{h * bw + 2:.1f}" y="{ch - bh:.1f}" width="{bw - 4:.1f}" height="{bh:.1f}" rx="3" class="{cls}"/>')
        if h % 3 == 0:
            parts.append(f'<text x="{h * bw + bw / 2:.1f}" y="{ch + 16}" class="axis" text-anchor="middle">{h}시</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_trend(trend: list[dict], width: int = 700) -> str:
    if not trend:
        return '<div class="empty">기록이 없습니다</div>'
    ch = 110
    mv = max(t["n"] for t in trend) or 1
    bw = width / len(trend)
    parts = [f'<svg viewBox="0 0 {width} {ch + 26}" width="100%" role="img">']
    pts = []
    for i, t in enumerate(trend):
        bh = (ch - 20) * t["n"] / mv
        x = i * bw
        parts.append(f'<rect x="{x + bw * 0.2:.1f}" y="{ch - bh:.1f}" width="{bw * 0.6:.1f}" height="{bh:.1f}" rx="4" class="tbar"/>')
        parts.append(f'<text x="{x + bw / 2:.1f}" y="{ch - bh - 4:.1f}" class="val" text-anchor="middle">{t["n"]}</text>')
        parts.append(f'<text x="{x + bw / 2:.1f}" y="{ch + 18}" class="axis" text-anchor="middle">{_e(t["label"])}</text>')
        if t["rate"] is not None:
            pts.append((x + bw / 2, ch - (ch - 20) * t["rate"] / 100))
    if len(pts) >= 2:
        parts.append('<polyline class="tline" points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')
    for x, y in pts:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" class="tdot"/>')
    parts.append("</svg>")
    return "".join(parts)


def svg_donut(pct: float | None, label: str, size: int = 150) -> str:
    r, c = 58, size / 2
    circ = 2 * math.pi * r
    p = max(0.0, min(100.0, pct or 0))
    txt = "—" if pct is None else f"{pct:.0f}%"
    arc = (f'<circle cx="{c}" cy="{c}" r="{r}" class="dfg" stroke-dasharray="{circ * p / 100:.1f} {circ:.1f}" '
           f'transform="rotate(-90 {c} {c})"/>') if p > 0 else ""  # 0%·기록 없음이면 둥근 끝 점도 그리지 않는다
    return (f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img">'
            f'<circle cx="{c}" cy="{c}" r="{r}" class="dbg"/>{arc}'
            f'<text x="{c}" y="{c + 4}" text-anchor="middle" class="dnum">{txt}</text>'
            f'<text x="{c}" y="{c + 26}" text-anchor="middle" class="dlab">{_e(label)}</text></svg>')


# ─────────────────────────── HTML ───────────────────────────
def _fmt_pct(v, sign: bool = True, dash: str = "—") -> str:
    if v is None:
        return dash
    return f"{v:+.1f}%" if sign else f"{v:.0f}%"


def _px(p) -> str:
    if not p:
        return "—"
    p = float(p)
    return f"${p:,.2f}" if p >= 1 else f"${p:.6f}".rstrip("0")


def _money(v) -> str:
    v = float(v or 0)
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= d:
            return f"${v / d:.1f}{s}"
    return f"${v:.0f}"


def _flag(market) -> str:
    return "<span class='flag us'>US</span>" if market == "US" else "<span class='flag kr'>KR</span>"


def _catalyst(e: dict, n: int) -> str:
    """보여줄 재료 한 줄: 안내문('찾지 못했습니다')은 빼고, 번역 안 된 영어보다 한국어를 먼저."""
    cats = [c for c in (e.get("catalyst") or []) if c and "찾지 못했" not in c]
    cats.sort(key=lambda c: not any("가" <= ch <= "힣" for ch in c))
    c = cats[0] if cats else ""
    return c if len(c) <= n else c[: n - 1] + "…"


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _cls(v) -> str:
    if v is None:
        return ""
    return "up" if v > 0 else ("down" if v < 0 else "")


CSS = """
@page { size: A4; margin: 0; }
* { box-sizing: border-box; }
body { margin: 0; font-family: 'Pretendard', 'Noto Sans CJK KR', 'Noto Sans KR', 'Malgun Gothic', sans-serif;
  color: #1B2230; background: #fff; font-size: 10.5pt; line-height: 1.55; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.page { width: 210mm; height: 297mm; overflow: hidden; padding: 14mm 15mm 14mm; position: relative; page-break-after: always; }
.page:last-child { page-break-after: auto; }
.cover { background: radial-gradient(120% 80% at 100% 0%, #3A1420 0%, #0C1222 55%, #070B16 100%); color: #EEF1F7; padding: 20mm 17mm; }
.brand { font-size: 10pt; letter-spacing: .18em; color: #FF7A59; font-weight: 700; }
.cover h1 { font-size: 34pt; line-height: 1.15; margin: 10mm 0 3mm; font-weight: 800; letter-spacing: -.02em; }
.cover h1 em { font-style: normal; color: #FF5A36; }
.period { font-size: 13pt; color: #AEB6C8; }
.kpis { display: grid; grid-template-columns: 1fr 1fr; gap: 5mm; margin-top: 14mm; }
.kpi { background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.1); border-radius: 14px; padding: 6mm; }
.kpi .n { font-size: 30pt; font-weight: 800; letter-spacing: -.02em; line-height: 1.1; }
.kpi .n small { font-size: 13pt; font-weight: 600; color: #AEB6C8; margin-left: 2px; }
.kpi .t { font-size: 10pt; color: #C9CFDC; margin-top: 2mm; }
.kpi .s { font-size: 8.5pt; color: #8B93A7; margin-top: 1mm; }
.kpi.hot .n { color: #FF6B4A; } .kpi.good .n { color: #3DDC97; }
.summary { margin-top: 10mm; background: rgba(255,255,255,.04); border-left: 3px solid #FF5A36; border-radius: 0 12px 12px 0; padding: 5mm 6mm; }
.summary h3 { margin: 0 0 2mm; font-size: 11pt; color: #FF9C84; letter-spacing: .05em; }
.summary li { margin: 1.6mm 0; color: #DCE1EB; }
.summary b { color: #fff; }
.cover .foot { position: absolute; left: 17mm; right: 17mm; bottom: 12mm; font-size: 8pt; color: #6E7790; display: flex; justify-content: space-between; }
h2 { font-size: 17pt; margin: 0 0 1mm; letter-spacing: -.01em; }
h2 .num { color: #FF5A36; margin-right: 6px; }
.lead { color: #5A6478; margin: 0 0 5mm; font-size: 9.5pt; }
h3 { font-size: 11.5pt; margin: 7mm 0 3mm; }
.card { border: 1px solid #E6E9F0; border-radius: 12px; padding: 4.5mm; margin-bottom: 4mm; break-inside: avoid; background: #fff; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 5mm; }
table { width: 100%; border-collapse: collapse; font-size: 9pt; }
th { text-align: left; font-weight: 600; color: #6B7488; border-bottom: 1.5px solid #1B2230; padding: 2mm 1.5mm; white-space: nowrap; }
td { border-bottom: 1px solid #EEF0F5; padding: 1.4mm 1.5mm; vertical-align: top; line-height: 1.35; }
td.r, th.r { text-align: right; white-space: nowrap; }
tr { break-inside: avoid; }
.up { color: #E0402A; font-weight: 700; } .down { color: #2463EB; font-weight: 700; }
.chip { display: inline-block; font-size: 7.5pt; padding: .4mm 2mm; border-radius: 99px; background: #F1F3F8; color: #475067; white-space: nowrap; }
.chip.unk { background: #FFF0EC; color: #C2412A; } .chip.bg { background: #EEF4FF; color: #2451B7; }
.muted { color: #8A93A6; font-size: 8.5pt; }
.lab { font-size: 11px; fill: #3A4356; } .val { font-size: 11px; fill: #1B2230; font-weight: 700; }
.note { fill: #8A93A6; font-weight: 400; } .axis { font-size: 10px; fill: #8A93A6; }
.bar0 { fill: #FF5A36; } .bar1 { fill: #FF8A6B; } .bar2 { fill: #FFB29E; }
.hbar { fill: #D5DAE5; } .hbar.hot { fill: #FF5A36; }
.tbar { fill: #FFD3C7; } .tline { fill: none; stroke: #16A36A; stroke-width: 2.5; } .tdot { fill: #16A36A; }
.dbg { fill: none; stroke: #EEF0F5; stroke-width: 16; } .dfg { fill: none; stroke: #16A36A; stroke-width: 16; stroke-linecap: round; }
.dnum { font-size: 26px; font-weight: 800; fill: #1B2230; } .dlab { font-size: 10px; fill: #8A93A6; }
.empty { color: #9AA2B3; font-size: 9.5pt; padding: 4mm 0; }
.legend { font-size: 8.5pt; color: #6B7488; margin-top: 2mm; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; vertical-align: -1px; margin: 0 4px 0 10px; }
.callout { background: #F6F8FC; border-radius: 12px; padding: 4mm 5mm; font-size: 9.5pt; color: #3A4356; }
.callout b { color: #1B2230; }
.pagefoot { position: absolute; left: 15mm; right: 15mm; bottom: 7mm; font-size: 7.5pt; color: #A0A7B6; display: flex; justify-content: space-between; }
.mk { display: flex; align-items: center; gap: 2mm; margin: 0 0 2mm; font-weight: 700; }
.flag { display: inline-block; font-size: 7.5pt; font-weight: 700; padding: .3mm 1.8mm; border-radius: 4px; color: #fff; vertical-align: 1px; }
.flag.us { background: #2451B7; } .flag.kr { background: #C2412A; }
.watch li { margin: 2mm 0; } .watch b { color: #1B2230; }
.glossary { display: grid; grid-template-columns: 1fr 1fr; gap: 1mm 6mm; margin: 0; }
.glossary div { break-inside: avoid; } .glossary dt { font-weight: 700; margin-top: 2mm; font-size: 9.5pt; }
.glossary dd { margin: .5mm 0 0; color: #4A5367; font-size: 8.8pt; line-height: 1.45; }
.disclaimer { position: absolute; left: 15mm; right: 15mm; bottom: 13mm; font-size: 8pt; color: #8A93A6; }
"""

FONT = '<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">'


def _foot(a: dict, n: int) -> str:
    return (f'<div class="pagefoot"><span>🚀 세력의 급등탐정 · {KIND_KO[a["period"]["kind"]]} 리포트 · '
            f'{_e(a["period"]["label"])}</span><span>{n}</span></div>')


def render_html(a: dict) -> str:
    p, c, s = a["period"], a["coin"], a["stock"]
    kind = KIND_KO[p["kind"]]
    gen = _kst(a["generated"])
    note_since = ""
    if a.get("since") and a["since"] > p["start"]:
        note_since = f"기록은 {_kst(a['since']):%m월 %d일}부터 쌓였습니다. 그 이전 기간은 집계에 없습니다."
    bullets = "".join(f"<li>{t}</li>" for t in insights(a)[:8])  # 표지 한 장에 들어가게

    kpi_success = _fmt_pct(c["success_rate"], sign=False)
    cover = f"""
<section class="page cover">
  <div class="brand">SEPOWER · PUMP DETECTIVE</div>
  <h1>세력의 급등탐정<br><em>{kind} 리포트</em></h1>
  <div class="period">{_e(p["label"])}</div>
  <div class="kpis">
    <div class="kpi hot"><div class="n">{c["ups"]}<small>건</small></div><div class="t">코인 급등 포착</div>
      <div class="s">코인 {c["coins"]}종 · 급락 알림 {c["downs"]}건</div></div>
    <div class="kpi"><div class="n">{_fmt_pct(c["known_rate"], sign=False)}</div><div class="t">오른 이유 확인률</div>
      <div class="s">상장·뉴스·선물 자금 등 근거를 찾은 비율</div></div>
    <div class="kpi good"><div class="n">{kpi_success}</div><div class="t">알림 뒤 24시간 +{SUCCESS_PCT:.0f}% 추가 상승</div>
      <div class="s">채점 {c["scored"]}건 · 평균 최고 {_fmt_pct(c["avg_max24"])}</div></div>
    <div class="kpi"><div class="n">{_fmt_pct(s["hit_rate"], sign=False)}</div><div class="t">주식 관찰 콜 적중률</div>
      <div class="s">판정 완료 {s["calls_done"]}건 중 적중 {s["calls_hit"]}건</div></div>
  </div>
  <div class="summary"><h3>핵심 요약</h3><ul>{bullets}</ul></div>
  <div class="foot"><span>{_e(note_since)}</span><span>{gen:%Y.%m.%d %H:%M} 발행</span></div>
</section>"""

    # ── 2. 이유별 성적
    grows = []
    for g in c["groups"]:
        chip = "chip unk" if g["name"] == UNKNOWN else "chip"
        grows.append(f"<tr><td><span class='{chip}'>{_e(g['name'])}</span></td><td class='r'>{g['n']}</td>"
                     f"<td class='r'>{g['scored']}</td><td class='r'>{_fmt_pct(g['success'], sign=False)}</td>"
                     f"<td class='r'>{_fmt_pct(g['fade'], sign=False)}</td>"
                     f"<td class='r {_cls(g['avg_r24'])}'>{_fmt_pct(g['avg_r24'])}</td></tr>")
    gtable = ("<table><tr><th>오른 이유</th><th class='r'>알림</th><th class='r'>채점</th><th class='r'>추가 상승</th>"
              "<th class='r'>되밀림</th><th class='r'>24시간 뒤 평균</th></tr>" + "".join(grows) + "</table>"
              if grows else '<div class="empty">기록이 없습니다</div>')
    gbars = svg_hbars([(g["name"], g["n"], "") for g in c["groups"]], unit="건", width=330)
    trend_block = ""
    if len(c["trend"]) >= 2:
        unit = {"week": "일별", "month": "주별"}.get(p["kind"], "월별")
        trend_block = f"""
  <div class="card"><h3 style="margin-top:0">{unit} 흐름</h3>{svg_trend(c["trend"])}
    <div class="legend"><i style="background:#FFD3C7"></i>급등 알림 수<i style="background:#16A36A"></i>알림 뒤 추가 상승 비율</div></div>"""
    wd = c["weekdays"]
    wd_txt = " · ".join(f"{WEEKDAY[i]} {int(wd.get(i, wd.get(str(i), 0)))}" for i in range(7))
    page2 = f"""
<section class="page">
  <h2><span class="num">01</span>코인 급등, 이유별 성적표</h2>
  <p class="lead">알림을 보낸 뒤 24시간 동안 실제로 더 올랐는지 1시간봉으로 채점했습니다.
  <b>추가 상승</b>은 알림가보다 +{SUCCESS_PCT:.0f}% 이상 더 오른 적이 있는 경우, <b>되밀림</b>은 24시간 뒤 {FADE_PCT:.0f}% 이하로 내려간 경우입니다.</p>
  <div class="card">{gtable}</div>
  <div class="grid2">
    <div class="card"><h3 style="margin-top:0">이유별 알림 수</h3>{gbars}</div>
    <div class="card"><h3 style="margin-top:0">시간대별 급등 (한국 시간)</h3>{svg_hours(c["hours"])}
      <div class="muted" style="margin-top:2mm">요일별: {wd_txt}</div>
      <div class="muted">바이낸스에 없는 비트겟 코인 비중 {_fmt_pct(c["bitget_share"], sign=False)}</div></div>
  </div>
  {trend_block}
  {_foot(a, 2)}
</section>"""

    # ── 3. 순위
    limit = 10 if p["kind"] == "week" else 12
    trows = []
    for i, e in enumerate(c["top"][:limit], 1):
        o = e.get("out") or {}
        d = _kst(e["ts"])
        ex = "<span class='chip bg'>비트겟</span>" if e.get("market") == "bitget" else ""
        grp = e.get("group") or reason_group(e.get("tag", ""))
        chip = "chip unk" if grp == UNKNOWN else "chip"
        trows.append(
            f"<tr><td class='r'>{i}</td><td><b>{_e(e['base'])}</b> {ex}<div class='muted'>{d:%m.%d} {d:%H:%M}</div></td>"
            f"<td class='r up'>{_fmt_pct(e.get('chg24h'))}</td><td><span class='{chip}'>{_e(grp)}</span>"
            f"<div class='muted'>{_e((e.get('news') or '')[:46])}</div></td>"
            f"<td class='r {_cls(o.get('max24'))}'>{_fmt_pct(o.get('max24'))}</td>"
            f"<td class='r {_cls(o.get('r24'))}'>{_fmt_pct(o.get('r24'))}</td></tr>")
    ttable = ("<table><tr><th class='r'>#</th><th>코인 · 포착 시각</th><th class='r'>포착 시 24시간</th><th>이유</th>"
              "<th class='r'>이후 최고</th><th class='r'>24시간 뒤</th></tr>" + "".join(trows) + "</table>"
              if trows else '<div class="empty">기록이 없습니다</div>')

    def mini(rows: list[dict], key: str) -> str:
        if not rows:
            return '<div class="empty">채점이 끝난 알림이 아직 없습니다</div>'
        return "<table>" + "".join(
            f"<tr><td><b>{_e(e['base'])}</b> <span class='muted'>{_kst(e['ts']):%m.%d %H시}</span></td>"
            f"<td class='r {_cls(e['out'][key])}'>{_fmt_pct(e['out'][key])}</td></tr>" for e in rows) + "</table>"

    rep = ", ".join(f"{b} {n}회" for b, n in c["repeat"] if n >= 2) or "없음"
    page3 = f"""
<section class="page">
  <h2><span class="num">02</span>기간 중 가장 크게 오른 코인</h2>
  <p class="lead">같은 코인이 여러 번 잡혔으면 가장 크게 올랐을 때 한 번만 넣었습니다. '이후 최고'와 '24시간 뒤'는 알림가 기준입니다.</p>
  <div class="card">{ttable}</div>
  <div class="grid2">
    <div class="card"><h3 style="margin-top:0">🟢 알림 뒤 더 크게 오른 코인</h3>{mini(c["follow"], "max24")}</div>
    <div class="card"><h3 style="margin-top:0">🔴 알림 뒤 크게 되밀린 코인</h3>{mini(c["faded"], "r24")}</div>
  </div>
  <div class="callout"><b>여러 번 잡힌 코인</b> · {_e(rep)}</div>
  {_foot(a, 3)}
</section>"""


    # ── 4. 첫 포착 이후 추적
    fu = a.get("followup") or {}

    def fu_table(part: dict) -> str:
        rows = []
        for st_ in part.get("stats") or []:
            if not st_.get("n"):
                continue
            rows.append(f"<tr><td><b>{_e(st_['label'])} 뒤</b></td><td class='r'>{st_['n']}</td>"
                        f"<td class='r {_cls(st_['avg'])}'>{_fmt_pct(st_['avg'])}</td>"
                        f"<td class='r {_cls(st_['med'])}'>{_fmt_pct(st_['med'])}</td>"
                        f"<td class='r'>{st_['win']}%</td>"
                        f"<td class='r up'>{_fmt_pct(st_['avg_hi'])}</td>"
                        f"<td class='r down'>{_fmt_pct(st_['avg_lo'])}</td></tr>")
        if not rows:
            return '<div class="empty">아직 구간이 끝난 포착이 없습니다</div>'
        return ("<table><tr><th>구간</th><th class='r'>채점</th><th class='r'>평균</th><th class='r'>중간값</th>"
                "<th class='r'>오른 비율</th><th class='r'>구간 최고 평균</th><th class='r'>구간 최저 평균</th></tr>"
                + "".join(rows) + "</table>")

    def fu_list(rows: list, key: str, head: str) -> str:
        if not rows:
            return '<div class="empty">기록이 없습니다</div>'
        out = []
        for r in rows[:6]:
            d = _kst(r.get("ts", 0))
            rep = f" · 재포착 {r['repeats']}회" if r.get("repeats", 1) >= 2 else ""
            out.append(f"<tr><td><b>{_e(r.get('name', ''))}</b><div class='muted'>{d:%m.%d} 첫 포착"
                       f"{_e(rep)}</div></td><td class='r {_cls(r.get(key))}'>{_fmt_pct(r.get(key))}</td>"
                       f"<td class='r muted'>{_fmt_pct(r.get('cur'))}</td></tr>")
        return (f"<table><tr><th>자산</th><th class='r'>{head}</th><th class='r'>최근</th></tr>"
                + "".join(out) + "</table>")

    coin_fu, stock_fu = fu.get("coin") or {}, fu.get("stock") or {}
    hs_txt = " · ".join(h["label"] for h in (fu.get("horizons") or []))
    stock_fu_block = (f'<div class="card"><h3 style="margin-top:0">주식(대장주 · 관찰 콜) 첫 포착 '
                      f'{stock_fu.get("n", 0)}건</h3>{fu_table(stock_fu)}</div>'
                      if stock_fu.get("scored") else "")
    page_fu = f"""
<section class="page">
  <h2><span class="num">03</span>첫 포착 이후, 그래서 얼마나 갔나</h2>
  <p class="lead">같은 자산이 여러 번 잡혀도 기준은 <b>맨 처음 알린 그 가격</b>입니다. 거기서 {_e(hs_txt)} 뒤 가격을 계속 따라갑니다.
  코인은 1시간봉, 주식은 일봉 종가로 쟀고, 이 기간에 <b>처음</b> 포착한 코인 {coin_fu.get("n", 0)}건 · 주식 {stock_fu.get("n", 0)}건이 대상입니다.</p>
  <div class="card"><h3 style="margin-top:0">코인 첫 포착 {coin_fu.get("n", 0)}건의 구간별 성적</h3>{fu_table(coin_fu)}</div>
  <div class="grid2">
    <div class="card"><h3 style="margin-top:0">🟢 첫 포착 뒤 가장 크게 간 것</h3>{fu_list(coin_fu.get("best") or [], "hi", "추적 중 최고")}</div>
    <div class="card"><h3 style="margin-top:0">🔴 첫 포착 뒤 가장 밀린 것</h3>{fu_list(coin_fu.get("worst") or [], "lo", "추적 중 최저")}</div>
  </div>
  {stock_fu_block}
  <div class="callout"><b>읽는 법</b> · '오른 비율'은 그 구간이 끝난 시점에 포착가보다 위에 있던 비율입니다.
  구간 최고 평균이 높은데 평균이 낮으면, 먹을 구간은 있었지만 들고 있으면 돌려주는 유형이라는 뜻입니다.</div>
  {_foot(a, 4)}
</section>"""

    # ── 5. 주식
    def market_block(mk: str, flag: str, name: str) -> str:
        m = s["markets"][mk]
        bars = svg_hbars([(_short(t, 9), n, "") for t, n in m["themes"][:5]], unit="회", width=330)
        rows = []
        for e in m["leaders"][:5]:
            ld = e.get("leader") or {}
            cat = _catalyst(e, 34)
            rows.append(f"<tr><td><b>{_e(ld.get('name', ''))}</b><div class='muted'>{_e(e['theme'])} · "
                        f"{_kst(e['ts']):%m.%d}</div><div class='muted'>{_e(cat)}</div></td>"
                        f"<td class='r up'>{_fmt_pct(ld.get('chg'))}</td></tr>")
        lt = "<table>" + "".join(rows) + "</table>" if rows else '<div class="empty">기록이 없습니다</div>'
        return (f'<div class="card"><div class="mk">{flag} {name} · 테마 알림 {m["n"]}건</div>'
                f'<div class="muted" style="margin-bottom:2mm">자주 불붙은 테마</div>{bars}'
                f'<div class="muted" style="margin:3mm 0 1mm">대표 대장주</div>{lt}</div>')

    crow = []
    status_ko = {"hit": "✅ 적중", "miss": "❌ 빗나감", "pending": "⏳ 진행 중"}
    for cl in sorted(s["calls"], key=lambda x: x["ts"])[-6:]:
        crow.append(f"<tr><td>{_kst(cl['ts']):%m.%d}</td><td>{_flag(cl.get('market'))} "
                    f"<b>{_e(cl.get('name') or cl.get('symbol'))}</b><div class='muted'>{_e(cl.get('theme') or '')} · "
                    f"대장 {_e(cl.get('leader') or '')}</div></td><td>{status_ko.get(cl.get('status'), '')}</td>"
                    f"<td class='r {_cls(cl.get('result_pct'))}'>{_fmt_pct(cl.get('result_pct'))}</td></tr>")
    ctable = ("<table><tr><th>콜</th><th>종목</th><th>결과</th><th class='r'>최고 수익</th></tr>" + "".join(crow)
              + "</table>") if crow else '<div class="empty">이 기간에 낸 관찰 콜이 없습니다</div>'
    page4 = f"""
<section class="page">
  <h2><span class="num">04</span>미국·한국 주식 테마</h2>
  <p class="lead">그날 돈이 몰린 테마의 대장주와, 뒤따라올 2·3등을 추적한 기록입니다.</p>
  <div class="grid2">{market_block("US", _flag("US"), "미국")}{market_block("KR", _flag("KR"), "한국")}</div>
  <div class="card"><div class="grid2" style="grid-template-columns: 170px 1fr; align-items:center">
    <div style="text-align:center">{svg_donut(s["hit_rate"], "관찰 콜 적중률")}
      <div class="muted">평균 최고 수익 {_fmt_pct(s["avg_result"])}</div></div>
    <div><h3 style="margin-top:0">관찰 콜 성적</h3>{ctable}
      <div class="muted" style="margin-top:2mm">적중: 콜 이후 1거래일 안에 기준가 대비 +2% 이상</div></div>
  </div></div>
  {_foot(a, 5)}
</section>"""

    watch = "".join(f"<li>{t}</li>" for t in watchlist(a))
    page5 = f"""
<section class="page">
  <h2><span class="num">05</span>다음 기간에 지켜볼 것</h2>
  <p class="lead">이번 기록에서 반복해서 나온 신호를 모았습니다. 다음 알림을 읽을 때 기준으로 쓰시면 좋아 보입니다.</p>
  <div class="card"><ul class="watch">{watch}</ul></div>
  <h2 style="margin-top:8mm"><span class="num">06</span>리포트 읽는 법</h2>
  <p class="lead">숫자는 모두 실제로 보낸 알림과 이후 시세로 계산했습니다. 표본이 적은 칸은 참고만 하시는 것이 좋아 보입니다.</p>
  <div class="card"><dl class="glossary">
    <div><dt>급등 포착</dt><dd>5분 +3% · 15분 +5% · 1시간 +8%, 거래량 3배, 또는 하루 +20% 이면서 최근 2시간에도 움직이는 코인을 잡습니다.</dd></div>
    <div><dt>오른 이유 확인률</dt><dd>상장 공지, 뉴스, 선물 자금 유입, 테마 순환매 등 근거를 하나라도 찾은 비율입니다. 못 찾으면 '원인 미확인'(큰손 주도 의심)으로 분류합니다.</dd></div>
    <div><dt>추가 상승 / 되밀림</dt><dd>알림가 대비 24시간 안에 +{SUCCESS_PCT:.0f}% 이상 더 오른 적이 있으면 추가 상승, 24시간 뒤 {FADE_PCT:.0f}% 이하면 되밀림입니다.</dd></div>
    <div><dt>대장주 · 2·3등</dt><dd>같은 테마에서 가장 먼저, 가장 크게 오른 종목이 대장주입니다. 2·3등이 아직 덜 올랐으면 따라오는지 지켜봅니다.</dd></div>
    <div><dt>첫 포착 이후 추적</dt><dd>같은 자산이 여러 번 잡혀도 맨 처음 알린 가격을 기준으로 1일·3일·7일·30일 뒤를 계속 따라갑니다. 재포착은 회차로만 기록합니다.</dd></div>
    <div><dt>관찰 콜</dt><dd>지금 사라는 신호가 아니라 앞으로 움직임을 지켜볼 종목 표시입니다. 1거래일 안에 +2%면 적중으로 기록하고, 틀린 콜도 모두 공개합니다.</dd></div>
  </dl></div>
  <div class="callout" style="margin-top:6mm"><b>활용 팁</b> · 이유가 확인된 급등과 원인 미확인 급등의 성적 차이를 먼저 보십시오.
  이미 크게 오른 코인을 쫓기보다 이유가 뚜렷하고 되밀림이 적은 유형을 고르는 데 이 리포트를 쓰시는 것이 좋아 보입니다.</div>
  <p class="disclaimer">⚠️ 이 리포트는 매수·매도 추천이 아니라 지난 알림의 기록과 통계입니다. 투자 판단과 책임은 본인에게 있습니다.
  과거 성적이 미래 수익을 보장하지 않습니다.</p>
  {_foot(a, 6)}
</section>"""

    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>급등탐정 {kind} 리포트</title>'
            f'{FONT}<style>{CSS}</style></head><body>{cover}{page2}{page3}{page_fu}{page4}{page5}</body></html>')


# ─────────────────────────── PDF · 전송 ───────────────────────────
def to_pdf(html_text: str, out: Path) -> Path:
    from playwright.sync_api import sync_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    src = out.with_suffix(".html")
    src.write_text(html_text, encoding="utf-8")
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        page = br.new_page()
        page.goto(src.resolve().as_uri(), wait_until="networkidle", timeout=60_000)
        try:
            page.evaluate("document.fonts.ready.then(() => true)")
        except Exception:  # noqa: BLE001
            pass
        page.pdf(path=str(out), format="A4", print_background=True, prefer_css_page_size=True)
        br.close()
    return out


def file_name(a: dict) -> str:
    p = a["period"]
    return f"급등탐정_{KIND_KO[p['kind']]}리포트_{p['key'][2:]}.pdf"


def _fu_caption(a: dict) -> str:
    """첫 포착 이후 추적 한 줄 — 가장 긴 구간까지 채점된 것 위주로."""
    st = [x for x in (((a.get("followup") or {}).get("coin") or {}).get("stats") or []) if x.get("n")]
    if not st:
        return ""
    part = " · ".join(f"{x['label']} {_fmt_pct(x['avg'])}(오른 비율 {x['win']}%)" for x in st[:3])
    return f"📊 첫 포착 이후 {part}"


def caption(a: dict) -> str:
    p, c, s = a["period"], a["coin"], a["stock"]
    lines = [f"<b>📑 세력의 급등탐정 · {KIND_KO[p['kind']]} 리포트</b>", html.escape(p["label"]), "",
             f"🚀 코인 급등 포착 <b>{c['ups']}건</b> · 이유 확인률 {_fmt_pct(c['known_rate'], sign=False)}"]
    if c["scored"]:
        lines.append(f"🟢 알림 뒤 24시간 +{SUCCESS_PCT:.0f}% 추가 상승 {_fmt_pct(c['success_rate'], sign=False)} "
                     f"(채점 {c['scored']}건)")
    fu_line = _fu_caption(a)
    if fu_line:
        lines.append(fu_line)
    if s["calls_done"]:
        lines.append(f"📣 주식 관찰 콜 적중률 {_fmt_pct(s['hit_rate'], sign=False)} ({s['calls_hit']}/{s['calls_done']})")
    lines += ["", "이유별 성적표, 가장 크게 오른 코인, 미국·한국 테마 흐름을 PDF에 정리했습니다.",
              "<i>매수·매도 추천이 아닌 지난 알림의 기록과 통계입니다.</i>"]
    return "\n".join(lines)[:1024]


def send_pdf(path: Path, cap: str, kind: str = "pump") -> dict:
    import requests

    from .notify import telegram

    token, chat = telegram.token_for(kind), telegram.chat_id(kind)
    if not (token and chat):
        return {"status": "skipped", "reason": "텔레그램 토큰/CHAT_ID 미설정"}
    data = {"chat_id": chat, "caption": cap, "parse_mode": "HTML"}
    t = telegram.topic_for(kind)
    if t is not None:
        data["message_thread_id"] = t
    last = ""
    for _ in range(3):
        try:
            with path.open("rb") as f:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument", data=data,
                                  files={"document": (path.name, f, "application/pdf")}, timeout=90)
            js = r.json()
            if js.get("ok"):
                return {"status": "ok", "message_id": js["result"]["message_id"]}
            last = f"{r.status_code}: {js.get('description')}"
            if r.status_code == 429:
                time.sleep(min(float((js.get("parameters") or {}).get("retry_after") or 3), 60))
                continue
            break
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e).replace(token, '***')}"
            time.sleep(3)
    return {"status": "error", "error": last}


def build(period: dict, now: float | None = None) -> tuple[dict, Path]:
    a = aggregate(period, now)
    out = OUT_DIR / file_name(a)
    to_pdf(render_html(a), out)
    return a, out


def run_due(send: bool = True, now: float | None = None) -> dict:
    res: dict = {}
    for p in due(now):
        try:
            a, path = build(p, now)
            info = {"file": path.name, "ups": a["coin"]["ups"], "scored": a["coin"]["scored"]}
            if send:
                sent = send_pdf(path, caption(a))
                info["telegram"] = sent
                if sent.get("status") == "ok":
                    history.mark_report_sent(p["key"], info)
            res[p["key"]] = info
        except Exception as e:  # noqa: BLE001 — 보고서 실패가 다음 보고서·알림을 막지 않게
            res[p["key"]] = {"error": f"{type(e).__name__}: {e}"}
    return res


def current_period(kind: str, now: float | None = None) -> dict:
    """아직 끝나지 않은 이번 기간(시작 ~ 지금). 미리보기용."""
    now = now or time.time()
    ahead = {"week": 7, "month": 31, "quarter": 92, "year": 366}[kind]
    p = last_period(kind, _kst(now) + timedelta(days=ahead))
    while p["start"] > now:  # 월 길이 차이로 한 칸 더 넘어간 경우
        ahead -= 1
        p = last_period(kind, _kst(now) + timedelta(days=ahead))
    p.update(end=now, key=p["key"] + "-partial", label=p["label"] + f" · {_kst(now):%m.%d %H시} 기준 중간 집계")
    return p


def run_one(kind: str, at: str = "", send: bool = False, partial: bool = False) -> dict:
    if partial:
        p = current_period(kind)
    else:
        ref = datetime.strptime(at, "%Y-%m-%d").replace(hour=12, tzinfo=KST) if at else _kst(time.time())
        p = last_period(kind, ref)
    a, path = build(p)
    info = {"key": p["key"], "file": str(path), "ups": a["coin"]["ups"], "scored": a["coin"]["scored"],
            "stock_events": sum(m["n"] for m in a["stock"]["markets"].values())}
    if send:
        info["telegram"] = send_pdf(path, caption(a))
        if info["telegram"].get("status") == "ok" and not partial:
            history.mark_report_sent(p["key"], {"file": path.name, "manual": True})
    return info


def dump_json(a: dict) -> str:
    return json.dumps(a, ensure_ascii=False, default=str)

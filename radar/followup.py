"""첫 포착 이후 추적 — 알림을 보낸 뒤 그 자산이 실제로 어떻게 됐는지 계속 따라간다.

- 기준은 언제나 **첫 포착**이다. 같은 자산이 dedupe_days 안에 다시 잡히면 새 기록을 만들지 않고
  첫 기록의 회차(repeats)만 올린다. 그래서 "처음 잡았을 때 샀으면 지금 얼마인가"가 그대로 남는다.
- 1일·3일·7일·30일 시점의 수익률과 그 구간의 최고/최저를 잰다. 시점이 지나 확정된 값은
  history/followups.json 에 굳혀 두고 다시 받지 않는다(보고서와 같은 캐시 전략).
- 하루 한 번(기본 09시 KST) 각 방에 '포착 이후 성적표'를 보낸다.

가격 출처(전부 키 불필요): 코인=바이낸스·비트겟 1시간봉, 미국주식=야후 일봉, 한국주식=네이버 일봉.
주식은 일봉이라 장 마감 종가 기준이고, 코인은 24시간 시장이라 정확히 N시간 뒤다.

    python run_once.py followup            # 추적 갱신 + (기한이면) 성적표 발송
    python run_once.py followup --dry      # 갱신만 하고 전송은 안 함(파일로 미리보기)
    python run_once.py followup --force    # 오늘 이미 보냈어도 지금 다시 보냄
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from . import history
from .config import CFG
from .history import KST, hist_dir
from .http import get_json, pmap
from .notify import deliver
from .notify.message import Msg
from .sources import binance, bitget

DEFAULT_HORIZONS = [24, 72, 168, 720]        # 1일 · 3일 · 7일 · 30일
KIND_TITLE = {"pump": "코인 급등 포착", "trend": "BTC 대비 우상향 포착", "stock": "주식 테마 포착"}
YH = "https://query1.finance.yahoo.com"
NV_CHART = "https://api.stock.naver.com/chart/domestic/item"


# ─────────────────────────── 설정 ───────────────────────────
def horizons() -> list[int]:
    hs = CFG.get("followup.horizons_h", DEFAULT_HORIZONS) or DEFAULT_HORIZONS
    return sorted({int(h) for h in hs if int(h) > 0})


def hlabel(h: int) -> str:
    return f"{h // 24}일" if h % 24 == 0 else f"{h}시간"


def hkey(h: int) -> str:
    return f"h{h}"


def enabled() -> bool:
    return bool(CFG.get("followup.enabled", True))


def digest_kinds() -> list[str]:
    ks = CFG.get("followup.digest.kinds", ["pump", "trend", "stock"]) or []
    return [k for k in ks if k in KIND_TITLE]


# ─────────────────────────── 저장소 ───────────────────────────
def _path(name: str):
    return hist_dir() / name


def load_cache() -> dict:
    try:
        return json.loads(_path("followups.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_cache(cache: dict) -> None:
    try:
        _path("followups.json").write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def state() -> dict:
    try:
        return json.loads(_path("followup_state.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(st: dict) -> None:
    try:
        _path("followup_state.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────── 포착 목록 ───────────────────────────
def _from_event(e: dict) -> dict | None:
    """history 이벤트 한 줄 → 추적 대상(없으면 None)."""
    t = e.get("t")
    if t == "pump":
        if e.get("direction", "up") != "up" or not e.get("price") or not e.get("base"):
            return None
        return {"asset": "coin", "kind": "pump", "market": e.get("market") or "binance",
                "symbol": e.get("symbol") or f"{e['base']}USDT", "name": e["base"],
                "ts": float(e["ts"]), "price": float(e["price"]),
                "note": e.get("tag") or "", "chg24h": e.get("chg24h")}
    if t == "trend":
        if not e.get("price") or not e.get("base"):
            return None
        return {"asset": "coin", "kind": "trend", "market": e.get("market") or "binance",
                "symbol": e.get("symbol") or f"{e['base']}USDT", "name": e["base"],
                "ts": float(e["ts"]), "price": float(e["price"]),
                "note": f"우상향 점수 {e.get('score')}" if e.get("score") else "", "chg24h": None}
    if t == "stock":
        ld = e.get("leader") or {}
        if not ld.get("price") or not ld.get("symbol"):
            return None   # 2026-09-20 이전 기록엔 대장주 가격이 없다 → 추적 불가
        return {"asset": "stock", "kind": "stock", "market": e.get("market") or "US",
                "symbol": ld["symbol"], "name": ld.get("name") or ld["symbol"],
                "ts": float(e["ts"]), "price": float(ld["price"]),
                "note": f"{e.get('theme', '')} 대장주", "chg24h": ld.get("chg")}
    return None


def _from_call(c: dict) -> dict | None:
    if not c.get("ref_price") or not c.get("symbol"):
        return None
    return {"asset": "stock", "kind": "stock", "market": c.get("market") or "US",
            "symbol": c["symbol"], "name": c.get("name") or c["symbol"],
            "ts": float(c["ts"]), "price": float(c["ref_price"]),
            "note": f"{c.get('theme', '')} 관찰 콜", "chg24h": None}


def candidates(now: float | None = None) -> list[dict]:
    """기록 전체에서 '첫 포착' 목록을 만든다. 재포착은 첫 기록에 합친다."""
    now = now or time.time()
    first = history.first_ts()
    raw: list[dict] = []
    if first is not None:
        for e in history.load(first - 1, now + 1, kinds=("pump", "trend", "stock")):
            c = _from_event(e)
            if c:
                raw.append(c)
    for c0 in history.load_calls(0, now + 1):
        c = _from_call(c0)
        if c:
            raw.append(c)
    raw.sort(key=lambda c: c["ts"])

    dedupe = float(CFG.get("followup.dedupe_days", 30)) * 86400
    cur: dict[str, dict] = {}
    out: list[dict] = []
    for c in raw:
        aid = f"{c['asset']}|{c['market']}|{c['symbol']}"
        prev = cur.get(aid)
        if prev and c["ts"] - prev["last_ts"] <= dedupe:
            prev["repeats"] += 1
            prev["last_ts"] = c["ts"]
            continue
        c.update(id=aid, key=f"{aid}|{int(c['ts'])}", repeats=1, last_ts=c["ts"])
        cur[aid] = c
        out.append(c)
    return out


# ─────────────────────────── 시세 ───────────────────────────
# 봉은 전부 [봉이 닫힌 시각(초), 고가, 저가, 종가] 로 맞춘다. 거래소는 '연 시각'을 주므로 한 칸을 더한다.
def _rows(raw, step: int) -> list[list]:
    out = []
    for r in raw:
        try:
            out.append([float(r[0]) / 1000 + step, float(r[2]), float(r[3]), float(r[4])])
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda b: b[0])
    return out


def coin_bars(market: str, symbol: str, t0: float, hours: int) -> list[list]:
    """[시각초, 고가, 저가, 종가]. 비트겟은 한 번에 200봉이라 30일 구간은 4시간봉으로 받는다."""
    if market == "bitget":
        gran, step = ("1h", 3600) if hours <= 190 else ("4h", 14_400)
        limit = min(200, hours * 3600 // step + 2)
        d = get_json(f"{bitget.BASE}/api/v2/spot/market/history-candles",
                     params={"symbol": symbol, "granularity": gran,
                             "endTime": str(int((t0 + hours * 3600) * 1000)), "limit": str(limit)}, timeout=20)
        rows = _rows(d.get("data") or [], step)
    else:
        gran, step = ("1h", 3600) if hours <= 900 else ("4h", 14_400)
        raw = binance._spot("/api/v3/klines", params={"symbol": symbol, "interval": gran,
                                                      "startTime": int(t0 * 1000),
                                                      "limit": min(1000, hours * 3600 // step + 2)}, timeout=20)
        rows = _rows(raw, step)
    return [b for b in rows if b[0] > t0]


def us_bars(symbol: str, t0: float, hours: int) -> list[list]:
    d = get_json(f"{YH}/v8/finance/chart/{symbol}", timeout=20,
                 params={"period1": str(int(t0 - 86400)), "period2": str(int(t0 + hours * 3600 + 86400)),
                         "interval": "1d", "includePrePost": "false"})
    res = ((d.get("chart") or {}).get("result") or [{}])[0]
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    ts, hi, lo, cl = res.get("timestamp") or [], q.get("high") or [], q.get("low") or [], q.get("close") or []
    out = []
    for i, t in enumerate(ts):
        try:
            if cl[i] is None:
                continue
            out.append([float(t) + 6.5 * 3600, float(hi[i] if hi[i] is not None else cl[i]),
                        float(lo[i] if lo[i] is not None else cl[i]), float(cl[i])])
        except (IndexError, TypeError, ValueError):
            continue
    out.sort(key=lambda b: b[0])
    return [b for b in out if b[0] > t0]


def kr_bars(code: str, t0: float, hours: int) -> list[list]:
    s = datetime.fromtimestamp(t0 - 86400, KST).strftime("%Y%m%d0000")
    e = datetime.fromtimestamp(t0 + hours * 3600 + 86400, KST).strftime("%Y%m%d2359")
    raw = get_json(f"{NV_CHART}/{code}/day", params={"startDateTime": s, "endDateTime": e}, timeout=20)
    out = []
    for r in raw if isinstance(raw, list) else []:
        try:
            d = datetime.strptime(str(r["localDate"]), "%Y%m%d").replace(hour=15, minute=30, tzinfo=KST)
            out.append([d.timestamp(), float(r["highPrice"]), float(r["lowPrice"]), float(r["closePrice"])])
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda b: b[0])
    return [b for b in out if b[0] > t0]


def bars_for(rec: dict, hours: int) -> list[list]:
    if rec["asset"] == "coin":
        return coin_bars(rec["market"], rec["symbol"], rec["ts"], hours)
    if rec["market"] == "KR":
        return kr_bars(rec["symbol"], rec["ts"], hours)
    return us_bars(rec["symbol"], rec["ts"], hours)


# ─────────────────────────── 채점 ───────────────────────────
def measure(rec: dict, now: float, prev: dict | None = None) -> dict | None:
    """확정된 구간만 채운다. 주식은 일봉이라 '그 시점 이전 마지막 종가'로 본다."""
    hs = horizons()
    span_h = int(min(now - rec["ts"], hs[-1] * 3600) // 3600) + 2
    bars = bars_for(rec, max(span_h, 2))
    if not bars:
        return None
    p0 = float(rec["price"])
    if p0 <= 0:
        return None
    res = dict((prev or {}).get("res") or {})
    daily = rec["asset"] == "stock"
    for h in hs:
        k = hkey(h)
        if k in res:
            continue
        end = rec["ts"] + h * 3600
        if now < end:
            continue
        win = [b for b in bars if b[0] <= end]
        if not win or win[-1][0] < end - (5 * 86400 if daily else 3 * 3600):
            continue  # 아직 그 시점 봉이 안 왔다(주말·휴장·상장폐지) → 다음 실행에서 다시
        res[k] = {"r": round((win[-1][3] / p0 - 1) * 100, 2),
                  "hi": round((max(b[1] for b in win) / p0 - 1) * 100, 2),
                  "lo": round((min(b[2] for b in win) / p0 - 1) * 100, 2)}
    last = bars[-1]
    return {"res": res, "cur": round((last[3] / p0 - 1) * 100, 2), "cur_ts": int(last[0]),
            "hi": round((max(b[1] for b in bars) / p0 - 1) * 100, 2),
            "lo": round((min(b[2] for b in bars) / p0 - 1) * 100, 2),
            "checked": int(now), "done": all(hkey(h) in res for h in hs)}


def _needs(rec: dict, prev: dict | None, now: float, live: bool) -> bool:
    hs = horizons()
    if prev and prev.get("done"):
        return False
    age = now - rec["ts"]
    if age > (hs[-1] + 24 * 7) * 3600:          # 마지막 구간 + 일주일이 지나도 못 채웠으면 포기
        return False
    if any(now >= rec["ts"] + h * 3600 and hkey(h) not in ((prev or {}).get("res") or {}) for h in hs):
        return True
    # 성적표를 보내는 날엔 추적 중인 것의 '지금 수익률'도 새로 받는다
    return live and age <= hs[-1] * 3600 and (now - float((prev or {}).get("checked") or 0)) > 12 * 3600


def refresh(now: float | None = None, live: bool = False, workers: int = 6) -> dict:
    """추적 갱신. live=True 면 아직 진행 중인 것의 현재 수익률도 갱신한다(성적표 보내는 날)."""
    now = now or time.time()
    cache = load_cache()
    recs = candidates(now)
    todo = [r for r in recs if _needs(r, cache.get(r["key"]), now, live)]
    out = pmap(lambda r: measure(r, now, cache.get(r["key"])), todo, workers=workers)
    filled = 0
    for r, m in zip(todo, out):
        if not m:
            continue
        filled += 1
        cache[r["key"]] = {**{k: r[k] for k in ("asset", "kind", "market", "symbol", "name", "ts", "price",
                                                "note", "repeats")}, **m}
    for r in recs:                              # 회차·이름 등 메타는 항상 최신으로
        if r["key"] in cache:
            cache[r["key"]].update(repeats=r["repeats"], name=r["name"], note=r["note"])
    hs = horizons()
    old = now - (hs[-1] + 24 * 30) * 3600       # 30일 구간 + 30일 지난 기록은 요약에서 제외(파일은 유지)
    save_cache(cache)
    return {"tracked": len(recs), "checked": len(todo), "filled": filled,
            "active": sum(1 for r in recs if r["ts"] > old)}


# ─────────────────────────── 집계 ───────────────────────────
def _num(v) -> bool:
    return isinstance(v, (int, float))


def stats(rows: list[dict]) -> list[dict]:
    """구간별 평균·승률."""
    out = []
    for h in horizons():
        k = hkey(h)
        vals = [r["res"][k] for r in rows if (r.get("res") or {}).get(k) and _num(r["res"][k].get("r"))]
        if not vals:
            out.append({"h": h, "label": hlabel(h), "n": 0})
            continue
        rs = [v["r"] for v in vals]
        out.append({"h": h, "label": hlabel(h), "n": len(rs),
                    "avg": round(sum(rs) / len(rs), 1),
                    "med": round(sorted(rs)[len(rs) // 2], 1),
                    "win": round(sum(v > 0 for v in rs) / len(rs) * 100),
                    "avg_hi": round(sum(v["hi"] for v in vals) / len(vals), 1),
                    "avg_lo": round(sum(v["lo"] for v in vals) / len(vals), 1)})
    return out


def rows_for(kind: str = "", start: float = 0, end: float = 0, cache: dict | None = None,
             asset: str = "") -> list[dict]:
    rows = list((cache if cache is not None else load_cache()).values())
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if asset:
        rows = [r for r in rows if r.get("asset") == asset]
    if start:
        rows = [r for r in rows if r.get("ts", 0) >= start]
    if end:
        rows = [r for r in rows if r.get("ts", 0) < end]
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


def summary(kind: str = "", start: float = 0, end: float = 0, cache: dict | None = None,
            asset: str = "") -> dict:
    """보고서용 — 그 기간에 '처음 포착된' 자산들의 이후 성적."""
    rows = rows_for(kind, start, end, cache, asset)
    st = stats(rows)
    scored = [r for r in rows if r.get("res")]
    best = sorted(scored, key=lambda r: -(r.get("hi") if _num(r.get("hi")) else -999))[:8]
    worst = sorted(scored, key=lambda r: (r.get("cur") if _num(r.get("cur")) else 999))[:5]
    return {"n": len(rows), "scored": len(scored), "stats": st, "best": best, "worst": worst,
            "repeat": sorted([r for r in rows if r.get("repeats", 1) >= 2],
                             key=lambda r: -r.get("repeats", 1))[:6]}


# ─────────────────────────── 성적표 메시지 ───────────────────────────
def _pct(v, dash: str = "—") -> str:
    return f"{v:+.1f}%" if _num(v) else dash


def _px(rec: dict) -> str:
    p = float(rec.get("price") or 0)
    if rec.get("asset") == "stock":
        return f"{p:,.0f}원" if rec.get("market") == "KR" else f"${p:,.2f}"
    return f"${p:,.6f}".rstrip("0").rstrip(".") if p < 1 else f"${p:,.4f}".rstrip("0").rstrip(".")


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%m/%d %H:%M")


def matured_since(rows: list[dict], since: float, now: float) -> list[tuple[dict, list[int]]]:
    """직전 성적표 이후에 새로 확정된 (기록, 구간들). 한 자산이 두 구간을 한꺼번에 넘겼으면 한 줄로 묶는다."""
    out = []
    for r in rows:
        hs = [h for h in horizons()
              if since < r.get("ts", 0) + h * 3600 <= now and (r.get("res") or {}).get(hkey(h))]
        if hs:
            out.append((r, hs))
    out.sort(key=lambda x: -abs(x[0]["res"][hkey(x[1][-1])]["r"]))
    return out


def digest_msg(kind: str, now: float, since: float, cache: dict | None = None) -> Msg | None:
    hs = horizons()
    rows = rows_for(kind, start=now - (hs[-1] + 24 * 30) * 3600, cache=cache)
    if not rows:
        return None
    limit = int(CFG.get("followup.digest.max_rows", 6))
    all_fresh = matured_since(rows, since, now)
    fresh = all_fresh[:limit]
    live = [r for r in rows if now - r.get("ts", 0) <= hs[-1] * 3600 and _num(r.get("cur"))]
    live.sort(key=lambda r: -(r.get("cur") or 0))
    st = stats(rows)
    scored = sum(s["n"] for s in st)
    if not fresh and not live:
        return None

    title = KIND_TITLE.get(kind, kind)
    intro = [f"*처음 포착한 뒤 실제로 어떻게 됐는지* 계속 따라가고 있습니다. ({title})",
             f"_추적 중인 첫 포착 {len(rows)}건 · 구간 성적 {scored}건입니다. 재포착은 첫 포착 기록에 합칩니다._"]
    units: list[list[str]] = [intro]

    if fresh:
        blk = [f"🆕 *지난 성적표 이후 성적이 확정된 {len(all_fresh)}건* (변동이 큰 순서)"]
        for r, hs in fresh:
            last = r["res"][hkey(hs[-1])]
            grade = " · ".join(f"{hlabel(h)} {_pct(r['res'][hkey(h)]['r'])}" for h in hs)
            blk.append(f"• *{r['name']}* — 포착가 {_px(r)} → *{grade}* "
                       f"(그사이 최고 {_pct(last['hi'])} · 최저 {_pct(last['lo'])})")
            blk.append(f"   ◦ {_when(r['ts'])} 포착" + (f" · {r['note']}" if r.get("note") else "")
                       + (f" · 재포착 {r['repeats']}회" if r.get("repeats", 1) >= 2 else ""))
        rest = all_fresh[limit:]
        if rest:
            vals = [r["res"][hkey(hs[-1])]["r"] for r, hs in rest]
            blk.append(f"_나머지 {len(rest)}건은 평균 {sum(vals) / len(vals):+.1f}%, "
                       f"그중 {sum(v > 0 for v in vals)}건이 플러스였습니다._")
        units.append(blk)

    if live:
        top = live[:limit]
        blk = ["📈 *지금 추적 중인 것 (포착가 대비 현재)*"]
        for r in top:
            days = max(0, int((now - r["ts"]) // 86400))
            blk.append(f"• *{r['name']}* {_pct(r.get('cur'))} · 포착 {days}일째 "
                       f"(최고 {_pct(r.get('hi'))} · 최저 {_pct(r.get('lo'))})")
        if len(live) > limit:
            blk.append(f"_… 외 {len(live) - limit}건을 더 보고 있습니다._")
        tail = [r for r in live[-3:] if r not in top and (r.get("cur") or 0) < 0]
        if tail:   # 잘 간 것만 보여주면 성적표가 아니다 — 가장 밀린 것도 같이 적는다
            blk.append("🔻 *반대로 가장 밀린 것*: "
                       + " · ".join(f"{r['name']} {_pct(r.get('cur'))}" for r in reversed(tail)))
        units.append(blk)

    rated = [s for s in st if s["n"]]
    if rated:
        blk = ["📊 *구간별 평균 성적*"]
        for s in rated:
            blk.append(f"• *{s['label']} 뒤* 평균 {_pct(s['avg'])} · 오른 비율 {s['win']}% "
                       f"(중간값 {_pct(s['med'])} · 구간 최고 평균 {_pct(s['avg_hi'])}) — {s['n']}건")
        units.append(blk)

    footer = ["_기준: 알림을 보낸 그 가격(포착가)에서 각 구간 뒤 가격까지의 변화입니다. "
              "코인은 1시간봉, 주식은 일봉 종가로 계산합니다._",
              "_매수·매도 추천이 아니라 지난 포착의 성적 기록입니다. 판단과 책임은 본인에게 있습니다._"]
    best = fresh[0][0]["name"] if fresh else (live[0]["name"] if live else "")
    return Msg(kind, "포착 이후 성적표", units, footer,
               summary=f"📊 포착 이후 성적표 · 추적 {len(rows)}건" + (f" · 선두 {best}" if best else ""))


# ─────────────────────────── 실행 ───────────────────────────
def digest_period(now: float) -> str:
    """성적표 한 번을 가리키는 키. 주 1회면 '2026-W38', 매일이면 날짜."""
    k = datetime.fromtimestamp(now, KST)
    if str(CFG.get("followup.digest.every", "week")).lower().startswith("w"):
        iso = k.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return f"{k:%Y-%m-%d}"


def digest_due(kind: str, now: float, st: dict | None = None) -> bool:
    """주 1회(기본 월요일 09시 KST) 또는 매일. 매일 보내면 틀린 날이 매일 드러나 방이 지친다."""
    if not CFG.get("followup.digest.enabled", True) or kind not in digest_kinds():
        return False
    k = datetime.fromtimestamp(now, KST)
    if k.hour < int(CFG.get("followup.digest.hour_kst", 9)):
        return False
    if str(CFG.get("followup.digest.every", "week")).lower().startswith("w")             and k.weekday() != int(CFG.get("followup.digest.weekday", 0)):
        return False
    return ((st if st is not None else state()).get("digest") or {}).get(kind) != digest_period(now)


def tick(now: float | None = None, send: bool = True, force: bool = False) -> dict:
    """추적 갱신 + 기한이 된 방에 성적표 발송. 클라우드 2시간 주기에서 그냥 호출하면 된다."""
    if not enabled():
        return {"status": "disabled"}
    now = now or time.time()
    st = state()
    kinds = [k for k in digest_kinds() if force or digest_due(k, now, st)]
    out: dict = {"refresh": refresh(now, live=bool(kinds)), "digest": {}}
    day = digest_period(now)
    cache = load_cache()
    for kind in kinds:
        since = float((st.get("last") or {}).get(kind) or (now - 86400))
        msg = digest_msg(kind, now, since, cache)
        if msg is None:
            out["digest"][kind] = {"status": "skipped", "reason": "보낼 성적이 없습니다"}
            st.setdefault("digest", {})[kind] = day     # 빈 날도 하루 한 번만 확인
            continue
        if not send:
            out["digest"][kind] = {"status": "dry", "chunks": len(msg.telegram_chunks())}
            continue
        res = deliver(msg)
        out["digest"][kind] = {"status": "ok" if res["delivered"] else "error",
                               "telegram": res.get("telegram"), "slack": res.get("slack")}
        if res["delivered"]:
            st.setdefault("digest", {})[kind] = day
            st.setdefault("last", {})[kind] = int(now)
    if send:
        save_state(st)
    return out

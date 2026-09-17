"""테마 추적 + 관찰 콜 판정 — 순수 로직(네트워크 없음, 테스트 대상).

행(row) 공통 필드
  symbol, name, price, chg(%), trade_value(한국=억 원, 미국=달러), mcap(한국=억 원, 미국=달러, 모르면 0),
  vol_x(평소 대비 거래량 배수, 모르면 0), open, high, series(장중 등락률 % 목록, 없으면 [])

흐름: pick_leader → rank_followers(2·3등) → verdict(따라감/아직/과열) → momentum(테마 힘)
      → should_call(관찰 콜 여부) → leader_invalid / invalidation_text(무효 조건) → evaluate_call(결과 추적)
"""
from __future__ import annotations

import math

DEFAULTS: dict = {
    "max_themes": 2,                  # 시장당 테마 블록 수
    "kr_theme_scan": 20,              # 오늘 등락률 상위 몇 개 테마를 훑을지
    "leader_min_chg": 8.0,            # 대장주 최소 상승률
    "leader_min_value_kr_eok": 50.0,  # 대장주 최소 거래대금(억 원)
    "leader_min_value_us": 5_000_000.0,   # 대장주 최소 거래대금(달러)
    "peer_min_value_kr_eok": 5.0,     # 2·3등 후보 최소 거래대금
    "peer_min_value_us": 1_000_000.0,
    "follow_min_chg": 2.0,            # '따라가는 중' 최소 상승률
    "follow_min_vol_x": 1.5,          # '따라가는 중' 최소 거래량 배수
    "overheat_ratio": 0.8,            # 대장 상승률의 80% 이상이면 '이미 따라감·과열'
    "weak_breadth": 0.3,              # 테마 종목 중 오른 비율이 이보다 낮으면 힘이 약함
    "call_max_peer_chg_ratio": 0.5,   # '따라가는 중'이라도 대장의 절반 이하만 올랐으면 콜 대상
    "call_min_peer_chg": -1.0,        # 테마가 오르는데 이보다 더 빠진 종목은 흐름 이탈로 보고 콜하지 않음
    "call_min_vol_x": 1.0,            # 동조율을 못 구할 때의 콜 근거: 거래량이 최소한 평소 수준은 돼야 한다
    "call_min_co_move": 0.4,          # 동조율을 구했으면 장중 흐름이 대장주와 반드시 이만큼은 같이 움직여야 한다
    "call_max_mcap_ratio": 20.0,      # 대장주보다 덩치가 20배 넘게 큰 종목은 같은 재료로 움직이기 어려워 제외
    "invalid_drop_pct": 5.0,          # 대장주가 고점 대비 이만큼 밀리면 콜 무효
    "hit_pct": 2.0,                   # 적중 기준: 기준가 대비 +2%
    "hit_window_trading_days": 1,     # 적중 판정 기간(거래일)
    "us_call_sessions": ["정규장"],   # 미국 새 콜을 내는 세션(프리·애프터는 시세 기준이 달라 기본 제외)
}

VERDICT_LABEL = {
    "following": "🟢 따라가는 중",
    "lagging": "🟡 아직 안 움직임",
    "overheated": "⚪ 이미 따라감·과열",
}


def settings(theme_cfg: dict | None) -> dict:
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (theme_cfg or {}).items() if not k.startswith("_")})
    return s


def _value(r: dict) -> float:
    return float(r.get("trade_value") or 0)


def _pct_rank(vals: list[float], v: float) -> float:
    if len(vals) <= 1:
        return 1.0 if v > 0 else 0.0
    return sum(1 for x in vals if x < v) / (len(vals) - 1)


def co_move(a: list[float], b: list[float]) -> float | None:
    """두 장중 등락률 시계열의 변화 상관계수(0~1로 자름). 점이 8개 미만이면 None."""
    n = min(len(a), len(b))
    if n < 8:
        return None
    ra = [a[-n:][i] - a[-n:][i - 1] for i in range(1, n)]
    rb = [b[-n:][i] - b[-n:][i - 1] for i in range(1, n)]
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    if va < 1e-9 or vb < 1e-9:  # 거의 평평한 시계열(거래정지 등)은 반올림 잡음뿐이라 계산하지 않는다
        return None
    return max(0.0, min(1.0, cov / (va * vb)))


def pick_leader(members: list[dict], s: dict, market: str) -> dict | None:
    """거래대금이 받쳐준 급등 종목 중 상승률(동률이면 거래대금) 1위."""
    min_v = s["leader_min_value_kr_eok"] if market == "KR" else s["leader_min_value_us"]
    cands = [m for m in members if (m.get("chg") or 0) >= s["leader_min_chg"] and _value(m) >= min_v]
    return max(cands, key=lambda m: (round(m["chg"], 1), _value(m))) if cands else None


def rank_followers(leader: dict, members: list[dict], s: dict, market: str, n: int = 2) -> list[dict]:
    """대장주를 뺀 테마 종목을 시가총액·거래대금·대장과 같이 움직인 정도로 줄 세워 2·3등을 고른다."""
    min_v = s["peer_min_value_kr_eok"] if market == "KR" else s["peer_min_value_us"]
    pool = [m for m in members if m.get("symbol") != leader.get("symbol") and (m.get("price") or 0) > 0]
    liquid = [m for m in pool if _value(m) >= min_v]
    if len(liquid) >= n:
        pool = liquid
    if not pool:
        return []
    vals = [_value(m) for m in pool]
    mcaps = [float(m.get("mcap") or 0) for m in pool]
    use_mcap = any(mcaps)
    lchg = float(leader.get("chg") or 0)
    out = []
    for m in pool:
        co = co_move(leader.get("series") or [], m.get("series") or [])
        # 장중 시계열이 없으면 '대장 대비 상승률 비율'로 동조 정도를 대신한다(순위용일 뿐, 상관도로 표시하지 않음)
        fit = co if co is not None else (max(0.0, min(1.0, float(m.get("chg") or 0) / lchg)) if lchg > 0 else 0.0)
        parts = [(0.4, _pct_rank(vals, _value(m))), (0.2, fit)]
        if use_mcap:
            parts.append((0.4, _pct_rank(mcaps, float(m.get("mcap") or 0))))
        score = sum(w * x for w, x in parts) / sum(w for w, _ in parts)
        out.append({**m, "follow_score": round(score, 3), "co_move": None if co is None else round(co, 2)})
    out.sort(key=lambda m: m["follow_score"], reverse=True)
    return out[:n]


def verdict(leader: dict, peer: dict, s: dict) -> str:
    lchg, pchg = float(leader.get("chg") or 0), float(peer.get("chg") or 0)
    if lchg > 0 and pchg >= lchg * s["overheat_ratio"]:
        return "overheated"
    if pchg >= s["follow_min_chg"] and float(peer.get("vol_x") or 0) >= s["follow_min_vol_x"]:
        return "following"
    return "lagging"


def momentum(leader: dict, members: list[dict], s: dict, breadth: float | None = None) -> dict:
    """테마 힘. 대장주만 오르고 나머지가 제자리/하락이면 weak."""
    peers = [m for m in members if m.get("symbol") != leader.get("symbol")]
    moved = [m for m in peers if float(m.get("chg") or 0) >= 0.5]
    if breadth is None:
        breadth = (sum(1 for m in peers if float(m.get("chg") or 0) > 0) / len(peers)) if peers else 0.0
    weak = (not moved) or breadth < s["weak_breadth"]
    return {"weak": weak, "moved": len(moved), "peers": len(peers), "breadth": round(breadth, 2)}


def leader_invalid(leader: dict, s: dict) -> str:
    """지금 이미 무효 조건에 걸렸으면 사유 문자열, 아니면 빈 문자열."""
    price = float(leader.get("price") or 0)
    high = float(leader.get("high") or 0)
    opn = float(leader.get("open") or 0)
    if price and high and price <= high * (1 - s["invalid_drop_pct"] / 100):
        return f"고점 대비 {(1 - price / high) * 100:.1f}% 밀린"
    if price and opn and price < opn:
        return "시가 아래로 내려간"
    return ""


def invalidation_text(leader: dict, s: dict, fmt_price) -> str:
    high, opn = leader.get("high") or 0, leader.get("open") or 0
    hi = f"고점({fmt_price(high)})" if high else "장중 고점"
    op = f"시가({fmt_price(opn)})" if opn else "시가"
    return (f"대장주 {leader.get('name') or leader.get('symbol')}의 주가가 {hi} 대비 "
            f"{s['invalid_drop_pct']:.0f}% 넘게 밀리거나 {op} 아래로 내려가면 이 콜은 무효입니다.")


def call_blocker(leader: dict, peer: dict, v: str, mom: dict, s: dict) -> str:
    """관찰 콜을 막는 사유. 빈 문자열이면 콜 대상.
    콜은 '테마가 살아 있고, 대장주가 무너지지 않았고, 이 종목이 아직 덜 올랐으며,
    같은 재료에 반응할 근거(거래량 또는 장중 동조)가 있고, 덩치 차이가 지나치지 않을 때'만 낸다."""
    if mom.get("weak"):
        return "테마 힘 약함"
    if leader_invalid(leader, s):
        return "대장주 무효 조건"
    lchg, pchg = float(leader.get("chg") or 0), float(peer.get("chg") or 0)
    if v == "overheated":
        return "이미 과열"
    if v == "following" and not (lchg > 0 and pchg <= lchg * s["call_max_peer_chg_ratio"]):
        return "이미 충분히 따라감"
    if pchg < s["call_min_peer_chg"]:
        return "흐름 이탈(하락)"
    lm, pm = float(leader.get("mcap") or 0), float(peer.get("mcap") or 0)
    if lm and pm and pm / lm > s["call_max_mcap_ratio"]:
        return "대장주보다 덩치가 너무 큼"
    co = peer.get("co_move")
    # 장중 동조율을 계산할 수 있으면 반드시 기준을 넘어야 한다 — 메시지의 '연결고리'가 이 수치에 기대기 때문.
    # (예전엔 거래량만 넘으면 동조율 0.10 종목에도 콜이 나갔다)
    if co is not None and co < s["call_min_co_move"]:
        return "대장주와 장중 흐름이 따로 놂"
    if co is None and float(peer.get("vol_x") or 0) < s["call_min_vol_x"]:
        return "거래량·장중 동조 근거 부족"
    return ""


def should_call(leader: dict, peer: dict, v: str, mom: dict, s: dict) -> bool:
    return not call_blocker(leader, peer, v, mom, s)


def evaluate_call(call: dict, price: float | None, day_high: float | None, now: float,
                  use_high: bool = False) -> tuple[str, float | None, float]:
    """(status, 결과%, 최고가). 기준가 대비 목표% 이상 찍으면 hit, 기한 지나면 miss, 아니면 pending.
    use_high: 콜한 날이 아닌 날의 고가만 반영(콜 이전 고가를 적중으로 치지 않기 위해)."""
    ref = float(call["ref_price"])
    best = max(float(call.get("best_price") or ref), float(price or 0),
               float(day_high or 0) if use_high else 0.0)
    best_pct = (best / ref - 1) * 100
    last_pct = (float(price) / ref - 1) * 100 if price else None
    if best_pct >= float(call["target_pct"]) - 1e-9:
        return "hit", round(best_pct, 2), best
    if now >= float(call["deadline"]):
        return "miss", round(last_pct if last_pct is not None else best_pct, 2), best
    return "pending", (round(last_pct, 2) if last_pct is not None else None), best

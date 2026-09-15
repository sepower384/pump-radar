"""장 시간 판정 — 한국(KST) / 미국(ET, 서머타임 자동). tzdata 없이 동작한다(윈도우 파이썬 대비).

한국: 평일 09:00~15:30 KST
미국: 평일 ET 04:00~09:30 프리마켓 · 09:30~16:00 정규장 · 16:00~20:00 애프터마켓
      (KST 로는 서머타임 기간 정규장 22:30~05:00, 겨울 23:30~06:00)
공휴일은 반영하지 않는다(한계).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
CLOSED = "휴장"


def _us_dst(d: date) -> bool:
    """미국 서머타임: 3월 둘째 일요일 ~ 11월 첫째 일요일."""
    mar1 = date(d.year, 3, 1)
    start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1 = date(d.year, 11, 1)
    end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return start <= d < end


def _utc(ts: float | datetime | None) -> datetime:
    if ts is None:
        return datetime.now(timezone.utc)
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=KST).astimezone(timezone.utc)
    return datetime.fromtimestamp(ts, timezone.utc)


def to_et(ts: float | datetime | None = None) -> datetime:
    u = _utc(ts)
    off = -4 if _us_dst((u - timedelta(hours=4)).date()) else -5
    return (u + timedelta(hours=off)).replace(tzinfo=timezone(timedelta(hours=off)))


def to_local(market: str, ts: float | datetime | None = None) -> datetime:
    return to_et(ts) if market == "US" else _utc(ts).astimezone(KST)


def local_date(market: str, ts: float | datetime | None = None) -> date:
    return to_local(market, ts).date()


def kr_session(ts: float | datetime | None = None) -> str:
    k = _utc(ts).astimezone(KST)
    m = k.hour * 60 + k.minute
    return "정규장" if k.weekday() < 5 and 9 * 60 <= m < 15 * 60 + 30 else CLOSED


def us_session(ts: float | datetime | None = None) -> str:
    e = to_et(ts)
    if e.weekday() >= 5:
        return CLOSED
    m = e.hour * 60 + e.minute
    if 4 * 60 <= m < 9 * 60 + 30:
        return "프리마켓"
    if 9 * 60 + 30 <= m < 16 * 60:
        return "정규장"
    if 16 * 60 <= m < 20 * 60:
        return "애프터마켓"
    return CLOSED


def session(market: str, ts: float | datetime | None = None) -> str:
    return us_session(ts) if market == "US" else kr_session(ts)


def _close_ts(market: str, d: date) -> float:
    if market == "US":
        off = -4 if _us_dst(d) else -5
        dt = datetime(d.year, d.month, d.day, 16, 0, tzinfo=timezone(timedelta(hours=off)))
    else:
        dt = datetime(d.year, d.month, d.day, 15, 30, tzinfo=KST)
    return dt.timestamp()


def hit_deadline(market: str, call_ts: float, trading_days: int = 1) -> float:
    """콜 이후 N거래일 마감 시각(유닉스초). 콜한 날(현지)의 다음 거래일부터 센다.
    예) 한국 화 10:00 콜 → 수 15:30 / 금 콜 → 월 15:30."""
    d = local_date(market, call_ts)
    left = max(1, int(trading_days))
    while left:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return _close_ts(market, d)


def fmt_kst(ts: float) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%m/%d %H:%M")


def now_ts() -> float:
    return time.time()

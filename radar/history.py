"""보낸 알림의 영구 기록 — 주간·월간·분기·연간 보고서의 원재료.

sqlite(data/)는 Actions 캐시라 언제든 비워질 수 있고 알림 기록도 30일이면 지운다.
그래서 보낸 알림은 history/YYYY-MM.jsonl 에 한 줄씩 쌓는다. 클라우드에선 워크플로가
이 폴더를 저장소의 history 브랜치로 커밋해 영구 보관한다(HISTORY_DIR 로 위치 지정).

이벤트 공통 필드: t(종류: pump/trend/stock/call), ts(유닉스초)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ROOT

KST = timezone(timedelta(hours=9))


def hist_dir() -> Path:
    d = Path(os.environ.get("HISTORY_DIR") or ROOT / "history")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _month_file(ts: float) -> Path:
    return hist_dir() / f"{datetime.fromtimestamp(ts, KST):%Y-%m}.jsonl"


def log(t: str, rows: list[dict], ts: float | None = None) -> int:
    """이벤트를 월별 파일에 덧붙인다. 기록 실패가 알림 전송을 막으면 안 되므로 예외는 삼킨다."""
    if not rows:
        return 0
    ts = float(ts or time.time())
    try:
        with _month_file(ts).open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({"t": t, "ts": int(ts), **r}, ensure_ascii=False) + "\n")
        return len(rows)
    except Exception:  # noqa: BLE001
        return 0


def load(start: float, end: float, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """[start, end) 구간 이벤트. 깨진 줄은 건너뛴다."""
    out: list[dict] = []
    d = datetime.fromtimestamp(start, KST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = datetime.fromtimestamp(end, KST)
    while d <= last:
        p = hist_dir() / f"{d:%Y-%m}.jsonl"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if start <= e.get("ts", 0) < end and (not kinds or e.get("t") in kinds):
                    out.append(e)
        d = (d + timedelta(days=32)).replace(day=1)
    out.sort(key=lambda e: e["ts"])
    return out


def first_ts() -> float | None:
    """가장 이른 기록 시각. 과거분(backfill)은 나중에 덧붙으므로 첫 파일 전체에서 최솟값을 본다."""
    for p in sorted(hist_dir().glob("*.jsonl")):
        best = None
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                ts = float(json.loads(line)["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            best = ts if best is None else min(best, ts)
        if best is not None:
            return best
    return None


# ── 관찰 콜 스냅샷: sqlite 의 calls 표를 통째로 보관(판정 결과가 나중에 바뀌므로 덮어쓴다) ──
def dump_calls(con) -> int:
    from . import store
    try:
        rows = store._dicts(con.execute("SELECT * FROM calls")) if con else []
    except Exception:  # noqa: BLE001
        return 0
    if not rows:
        return 0
    p = hist_dir() / "calls.json"
    old: dict = {}
    try:
        old = {f"{c['market']}|{c['symbol']}|{c['ts']}": c for c in json.loads(p.read_text(encoding="utf-8"))}
    except Exception:  # noqa: BLE001
        pass
    for r in rows:  # 캐시가 비워져 sqlite 가 새로 시작해도 옛 콜은 남긴다
        old[f"{r['market']}|{r['symbol']}|{r['ts']}"] = r
    p.write_text(json.dumps(sorted(old.values(), key=lambda c: c["ts"]), ensure_ascii=False, indent=0),
                 encoding="utf-8")
    return len(rows)


def load_calls(start: float, end: float) -> list[dict]:
    try:
        rows = json.loads((hist_dir() / "calls.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [c for c in rows if start <= c.get("ts", 0) < end]


# ── 사후 성과 캐시: 알림 뒤 가격 흐름은 한 번 확정되면 다시 받지 않는다 ──
def outcome_cache() -> dict:
    try:
        return json.loads((hist_dir() / "outcomes.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_outcomes(cache: dict) -> None:
    try:
        (hist_dir() / "outcomes.json").write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ── 보고서 발송 기록 ──
def sent_reports() -> dict:
    try:
        return json.loads((hist_dir() / "reports_sent.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def mark_report_sent(key: str, info: dict) -> None:
    d = sent_reports()
    d[key] = {"at": int(time.time()), **info}
    (hist_dir() / "reports_sent.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

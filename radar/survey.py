"""매달 1일 의견 받기 — 투표 없이, 봇 1:1 메시지로만.

강회장 지시(2026-09-20): 투표는 번거롭고, 결과가 방에 걸리는 것도 원치 않는다.
그래서 방에는 **"하고 싶은 말 있으면 봇으로 한 줄 보내주세요"** 안내 한 통만 올린다.

- 매달 1일 KST 10시 이후 첫 실행에서 `survey.kinds` 의 각 방에 안내 한 통.
- 답장(봇 1:1 메시지)은 **매 실행마다** 받아 운영자(`TELEGRAM_ADMIN_CHAT_ID`)에게 그대로 넘긴다.
  조사 기간이 아니어도 언제 온 의견이든 다 받는다.
- 내용은 **공개 저장소에 남기지 않는다**. 기록에는 건수만 남고, 원문은 `history/private/`(git 제외).

    python run_once.py survey             # 기한이면 안내 발송 + 의견 수집
    python run_once.py survey --force     # 지금 바로 안내 올리기
    python run_once.py survey-collect     # 의견만 수집
    python run_once.py survey-results     # 지금까지 받은 의견 요약(운영자용, 방에는 안 나감)
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import requests

from .config import CFG, env
from .history import KST, hist_dir
from .notify import deliver, telegram
from .notify.message import Msg

SEND_HOUR = 10          # KST. 이 시각 이후 첫 실행에서 보낸다
ASK = {
    "pump": "어떤 코인을 더 보고 싶은지, 알림이 많은지 적은지",
    "trend": "어떤 코인을 더 보고 싶은지, 점수 기준이 헐거운지 빡빡한지",
    "stock": "어떤 테마·종목을 더 보고 싶은지, 관찰 콜이 도움이 되는지",
}


# ─────────────────────────── 설정·저장소 ───────────────────────────
def enabled() -> bool:
    return bool(CFG.get("survey.enabled", True))


def kinds() -> list[str]:
    ks = CFG.get("survey.kinds", ["pump"]) or []
    return [k for k in ks if k in ("pump", "trend", "stock")]


def _hour() -> int:
    return int(CFG.get("survey.hour_kst", SEND_HOUR))


def _day() -> int:
    return int(CFG.get("survey.day", 1))


def state() -> dict:
    try:
        return json.loads((hist_dir() / "survey.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(st: dict) -> None:
    try:
        (hist_dir() / "survey.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _private_dir():
    """공개 저장소(history 브랜치)에 올라가면 안 되는 것만 여기 둔다."""
    d = hist_dir() / "private"
    d.mkdir(parents=True, exist_ok=True)
    gi = hist_dir() / ".gitignore"
    try:
        cur = gi.read_text(encoding="utf-8") if gi.exists() else ""
        if "private/" not in cur.split():
            gi.write_text((cur if not cur or cur.endswith("\n") else cur + "\n") + "private/\n",
                          encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return d


def month_key(now: float) -> str:
    """안내가 묻는 대상 = 지난달."""
    d = datetime.fromtimestamp(now, KST).replace(day=1)
    return f"{d - timedelta(days=1):%Y-%m}"


# ─────────────────────────── 안내 발송 ───────────────────────────
def due(now: float | None = None, st: dict | None = None) -> bool:
    if not enabled() or not kinds():
        return False
    now = now or time.time()
    k = datetime.fromtimestamp(now, KST)
    if k.day != _day() or k.hour < _hour():
        return False
    return month_key(now) not in ((st if st is not None else state()).get("sent") or {})


def bot_link(kind: str, st: dict | None = None) -> str:
    """의견을 받을 봇 1:1 링크. 못 가져오면 빈 문자열(안내 문구에서 빠진다)."""
    st = state() if st is None else st
    cached = (st.get("bot") or {}).get(kind)
    if cached:
        return cached
    try:
        me = telegram._call(telegram.token_for(kind), "getMe", {})
        user = (me.get("result") or {}).get("username")
    except Exception:  # noqa: BLE001
        return ""
    if not user:
        return ""
    st.setdefault("bot", {})[kind] = f"https://t.me/{user}"
    save_state(st)
    return st["bot"][kind]


def ask_msg(kind: str, now: float, st: dict) -> Msg:
    """방에 올라가는 안내 한 통. 투표도, 점수 공개도 없다."""
    mk = month_key(now)
    target = f"{int(mk.split('-')[1])}월" if "-" in mk else "지난달"
    link = bot_link(kind, st)
    where = f"<{link}|이 봇에게 1:1 메시지>" if link else "이 봇에게 1:1 메시지"
    units = [
        [f"*{target} 한 달, 이 방 어떠셨습니까?*",
         f"고칠 점이 있으면 {where}로 한 줄만 보내주시면 됩니다. 투표도 양식도 없습니다."],
        [f"_예를 들면 — {ASK.get(kind, '무엇이 도움이 됐고 무엇이 불편했는지')} 같은 것입니다._",
         "_보내주신 내용은 운영자만 보고, 방에는 공개하지 않습니다._"],
    ]
    return Msg(kind, "한 달에 한 번, 의견 받는 날", units,
               ["_받은 의견은 다음 달 알림 기준과 보고서에 반영합니다._"],
               summary="📮 한 달에 한 번, 의견 받는 날")


def send(now: float | None = None, kind: str = "") -> dict:
    now = now or time.time()
    st = state()
    out: dict = {}
    mk = month_key(now)
    done = []
    for k in ([kind] if kind else kinds()):
        if not telegram.available(k):
            out[k] = {"status": "skipped", "reason": "텔레그램 토큰/CHAT_ID 미설정"}
            continue
        res = deliver(ask_msg(k, now, st))
        out[k] = {"status": "ok" if res.get("delivered") else "error",
                  "month": mk, "telegram": res.get("telegram")}
        if res.get("delivered"):
            done.append(k)
    if done:
        st.setdefault("sent", {})[mk] = {"at": int(now), "kinds": done}
    save_state(st)
    return out


# ─────────────────────────── 의견 수집 ───────────────────────────
def _updates(kind: str, offset: int | None) -> list[dict]:
    token = telegram.token_for(kind)
    if not token:
        return []
    api = f"https://api.telegram.org/bot{token}/"
    try:
        requests.get(api + "deleteWebhook", timeout=15)   # 웹훅이 걸려 있으면 getUpdates 가 막힌다
        params = {"timeout": 0, "allowed_updates": '["message"]'}
        if offset:
            params["offset"] = offset
        return (requests.get(api + "getUpdates", params=params, timeout=25).json().get("result") or [])
    except Exception:  # noqa: BLE001
        return []


def _forward(kind: str, text: str, who: str, now: float) -> bool:
    admin = env("TELEGRAM_ADMIN_CHAT_ID", "")
    if not admin:
        return False
    when = f"{datetime.fromtimestamp(now, KST):%m/%d %H:%M}"
    body = "\n".join([f"<b>📩 의견이 왔습니다 ({telegram.to_html(kind)})</b>",
                      f"{telegram.to_html(who)} · {when}", "", telegram.to_html(text[:3000])])
    try:
        telegram._call(telegram.token_for(kind), "sendMessage",
                       {"chat_id": admin, "text": body, "parse_mode": "HTML",
                        "disable_web_page_preview": True})
        return True
    except Exception:  # noqa: BLE001
        return False


def _save_feedback(kind: str, who: str, text: str, now: float) -> None:
    """원문은 private 폴더에만(공개 저장소로 안 나간다)."""
    try:
        with (_private_dir() / "feedback.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(now), "kind": kind, "who": who, "text": text},
                               ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def collect(now: float | None = None) -> dict:
    """봇에게 온 1:1 메시지를 가져와 운영자에게 넘긴다. 텔레그램은 24시간만 보관하므로 매 실행마다."""
    now = now or time.time()
    st = state()
    out: dict = {}
    seen: set[str] = set()
    for kind in kinds():
        got = {"feedback": 0, "forwarded": 0}
        token = telegram.token_for(kind)
        if token and token in seen:   # 두 방이 같은 봇을 쓰면(주식=급등탐정) 한 번만 읽는다
            out[kind] = {"shared_bot": True, **got}
            continue
        seen.add(token)
        for u in _updates(kind, (st.get("offset") or {}).get(kind)):
            st.setdefault("offset", {})[kind] = u["update_id"] + 1
            m = u.get("message") or {}
            text = (m.get("text") or m.get("caption") or "").strip()
            if not text or (m.get("chat") or {}).get("type") != "private" or text.startswith("/"):
                continue
            frm = m.get("from") or {}
            who = f"@{frm['username']}" if frm.get("username") else (frm.get("first_name") or "익명")
            got["feedback"] += 1
            if _forward(kind, text, who, now):
                got["forwarded"] += 1
            _save_feedback(kind, who, text, now)
            st["feedback_n"] = int(st.get("feedback_n") or 0) + 1
            mk = f"{datetime.fromtimestamp(now, KST):%Y-%m}"
            by = st.setdefault("feedback_by_month", {})
            by[mk] = int(by.get(mk) or 0) + 1
        out[kind] = got
    save_state(st)
    return out


# ─────────────────────────── 조회(운영자용) ───────────────────────────
def recent(n: int = 10) -> list[dict]:
    """최근 받은 의견 원문 — private 폴더에 있을 때만. 방에는 절대 안 나간다."""
    try:
        lines = (_private_dir() / "feedback.jsonl").read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def results(st: dict | None = None) -> dict:
    st = st if st is not None else state()
    return {"보낸 안내": st.get("sent") or {}, "받은 의견": int(st.get("feedback_n") or 0),
            "월별 건수": st.get("feedback_by_month") or {}, "최근 의견": recent(10)}


# ─────────────────────────── 실행 ───────────────────────────
def tick(now: float | None = None, send_now: bool = True, force: bool = False) -> dict:
    if not enabled():
        return {"status": "disabled"}
    now = now or time.time()
    out: dict = {"collected": collect(now)}
    if force or due(now):
        out["sent"] = send(now) if send_now else {"status": "dry"}
    return out

"""매달 1일 만족도 조사 + 피드백 수렴.

- 매달 1일 KST 10시(설정 가능) 이후 첫 실행에서 방에 텔레그램 투표 2개를 올린다.
    1) 만족도 5점  2) 다음 달에 보고 싶은 것(복수 선택)
  투표는 익명이다. **결과는 방에 발표하지 않는다**(2026-09-20 강회장 지시) — 집계는 운영자에게만 간다.
  텔레그램 투표는 누른 사람에게 현재 집계가 보이므로, remove_after_days(기본 3일) 뒤 투표를 닫고 지운다.
- 자유 서술 피드백은 봇에게 1:1 메시지로 받는다. **내용은 공개 저장소에 남기지 않는다** —
  운영자 채팅(TELEGRAM_ADMIN_CHAT_ID)으로 그때그때 넘기고, 기록에는 건수만 남긴다.
  로컬 실행이면 원문이 history/private/ 아래에만 남는다(git 에서 제외).
- 집계는 history/survey.json 에 쌓이고, 새 집계가 들어오면 운영자 채팅으로만 요약을 보낸다.
  방에 나가는 메시지(조사 안내·PDF 보고서)에는 점수도 표도 넣지 않는다.

    python run_once.py survey             # 기한이면 조사 발송 + 응답 수집
    python run_once.py survey --force     # 지금 바로 조사 발송(수동)
    python run_once.py survey-collect     # 응답만 수집
    python run_once.py survey-results     # 지금까지 모인 결과 출력
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
SATISFACTION = {
    "q": "지난 한 달, 이 방의 포착과 성적표가 투자 판단에 도움이 됐나요?",
    "opts": ["아주 도움이 됐습니다", "도움이 된 편입니다", "보통입니다",
             "기대에 못 미칩니다", "거의 도움이 안 됐습니다"],
    "multi": False,
    "score": [5, 4, 3, 2, 1],
}
WISH = {
    "q": "다음 달에 가장 보고 싶은 것을 골라주세요 (여러 개 선택 가능)",
    "opts": ["포착 이후 성적표를 더 자세히", "급등 이유를 더 깊게", "알림을 줄이고 확실한 것만",
             "알림을 더 자주", "주식·테마 비중 늘리기", "코인 비중 늘리기",
             "용어·기초 설명 추가", "지금 이대로가 좋습니다"],
    "multi": True,
}
POLLS = [("satisfaction", SATISFACTION), ("wish", WISH)]


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
    """조사가 묻는 대상 = 지난달."""
    d = datetime.fromtimestamp(now, KST).replace(day=1)
    prev = d - timedelta(days=1)
    return f"{prev:%Y-%m}"


# ─────────────────────────── 발송 ───────────────────────────
def due(now: float | None = None, st: dict | None = None) -> bool:
    if not enabled() or not kinds():
        return False
    now = now or time.time()
    k = datetime.fromtimestamp(now, KST)
    if k.day != _day() or k.hour < _hour():
        return False
    return month_key(now) not in ((st if st is not None else state()).get("sent") or {})


def bot_link(kind: str, st: dict | None = None) -> str:
    """자유 피드백을 받을 봇 1:1 링크. 실패하면 빈 문자열(안내 문구에서 빠진다)."""
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


def intro_msg(kind: str, now: float, st: dict) -> Msg:
    """조사 결과(점수·집계)는 방에 발표하지 않는다 — 운영자만 본다(2026-09-20 강회장 지시)."""
    mk = month_key(now)
    link = bot_link(kind, st)
    target = f"{int(mk.split('-')[1])}월" if "-" in mk else "지난달"
    units = [[f"*{target} 한 달, 이 방 어떠셨습니까?* 한 달에 한 번, 1일에만 여쭤봅니다.",
              "_바로 아래 투표 두 개만 눌러주시면 됩니다. 익명이고 30초면 끝납니다._"]]
    tail = ["✍️ *하고 싶은 말은 자유롭게*",
            "투표에 없는 의견·불만·아이디어는 " +
            (f"<{link}|봇에게 1:1 메시지>로 보내주세요." if link else "봇에게 1:1 메시지로 보내주세요.") +
            " 운영자에게 그대로 전달되고, 방에는 공개되지 않습니다."]
    units.append(tail)
    return Msg(kind, "한 달에 한 번, 만족도 조사", units,
               ["_받은 의견은 다음 달 알림 기준과 보고서에 반영합니다._"],
               summary="📮 이 방 만족도 조사 (매달 1일)")


def _send_poll(kind: str, spec: dict) -> dict:
    payload = {**telegram._base(kind), "question": spec["q"][:300],
               "options": json.dumps([{"text": o[:100]} for o in spec["opts"]], ensure_ascii=False),
               "is_anonymous": True, "allows_multiple_answers": bool(spec.get("multi"))}
    try:
        return telegram._call(telegram.token_for(kind), "sendPoll", payload)
    except telegram.TelegramError:
        # 구버전 Bot API 는 옵션이 문자열 배열이다
        payload["options"] = json.dumps([o[:100] for o in spec["opts"]], ensure_ascii=False)
        return telegram._call(telegram.token_for(kind), "sendPoll", payload)


def send(now: float | None = None, kind: str = "") -> dict:
    now = now or time.time()
    st = state()
    out: dict = {}
    for k in ([kind] if kind else kinds()):
        if not telegram.available(k):
            out[k] = {"status": "skipped", "reason": "텔레그램 토큰/CHAT_ID 미설정"}
            continue
        res = deliver(intro_msg(k, now, st))
        polls = []
        for name, spec in POLLS:
            try:
                r = _send_poll(k, spec)
                p = (r.get("result") or {}).get("poll") or {}
                polls.append({"name": name, "poll_id": p.get("id"),
                              "message_id": (r.get("result") or {}).get("message_id"),
                              "opts": spec["opts"]})
                time.sleep(telegram.GAP_SEC)
            except Exception as e:  # noqa: BLE001
                polls.append({"name": name, "error": str(e)})
        mk = month_key(now)
        st.setdefault("sent", {})[mk] = {"at": int(now), "kind": k, "polls": polls,
                                         "intro": res.get("delivered")}
        out[k] = {"status": "ok" if any(p.get("poll_id") for p in polls) else "error",
                  "month": mk, "polls": [p.get("name") for p in polls if p.get("poll_id")],
                  "errors": [p["error"] for p in polls if p.get("error")] or None}
    save_state(st)
    return out


# ─────────────────────────── 응답 수집 ───────────────────────────
def _updates(kind: str, offset: int | None) -> list[dict]:
    token = telegram.token_for(kind)
    if not token:
        return []
    api = f"https://api.telegram.org/bot{token}/"
    try:
        requests.get(api + "deleteWebhook", timeout=15)   # 웹훅이 걸려 있으면 getUpdates 가 막힌다
        params = {"timeout": 0, "allowed_updates": '["poll","message"]'}
        if offset:
            params["offset"] = offset
        return (requests.get(api + "getUpdates", params=params, timeout=25).json().get("result") or [])
    except Exception:  # noqa: BLE001
        return []


def _poll_month(st: dict, poll_id: str) -> str:
    for mk, info in (st.get("sent") or {}).items():
        if any(p.get("poll_id") == poll_id for p in info.get("polls") or []):
            return mk
    return ""


def _poll_name(st: dict, poll_id: str) -> str:
    for info in (st.get("sent") or {}).values():
        for p in info.get("polls") or []:
            if p.get("poll_id") == poll_id:
                return p.get("name", "")
    return ""


def _forward(kind: str, text: str, who: str, now: float) -> bool:
    admin = env("TELEGRAM_ADMIN_CHAT_ID", "")
    if not admin:
        return False
    body = (f"<b>📩 스터디방 피드백</b>\n{telegram.to_html(who)} · "
            f"{datetime.fromtimestamp(now, KST):%m/%d %H:%M}\n\n{telegram.to_html(text[:3000])}")
    try:
        telegram._call(telegram.token_for(kind), "sendMessage",
                       {"chat_id": admin, "text": body, "parse_mode": "HTML",
                        "disable_web_page_preview": True})
        return True
    except Exception:  # noqa: BLE001
        return False


def sweep(now: float | None = None) -> dict:
    """투표 메시지를 며칠 뒤 방에서 치운다.

    텔레그램 투표는 누른 사람에게 현재 집계가 그대로 보인다(막을 수 있는 설정이 없다).
    결과를 방에 남겨두지 않기 위해, 며칠 지나면 투표를 닫고 메시지를 지운다.
    집계는 이미 history/survey.json 에 들어와 있고 운영자에게만 간다.
    survey.remove_after_days = 0 이면 그대로 둔다.
    """
    days = float(CFG.get("survey.remove_after_days", 3))
    now = now or time.time()
    st = state()
    removed = 0
    if days <= 0:
        return {"removed": 0, "reason": "remove_after_days=0"}
    for mk, info in (st.get("sent") or {}).items():
        if info.get("removed") or now - float(info.get("at") or 0) < days * 86400:
            continue
        kind = info.get("kind") or (kinds()[0] if kinds() else "pump")
        ok = True
        for p in info.get("polls") or []:
            mid = p.get("message_id")
            if not mid:
                continue
            for method in ("stopPoll", "deleteMessage"):   # 닫고 → 지운다
                try:
                    telegram._call(telegram.token_for(kind), method,
                                   {"chat_id": telegram.chat_id(kind), "message_id": mid})
                except Exception:  # noqa: BLE001 — 이미 지웠거나 권한이 없으면 넘어간다
                    ok = ok and method != "deleteMessage"
            removed += 1
        info["removed"] = int(now) if ok else info.get("removed")
    save_state(st)
    return {"removed": removed}


def collect(now: float | None = None) -> dict:
    """투표 집계와 1:1 피드백을 가져온다. 텔레그램은 업데이트를 24시간만 보관하므로 매 실행마다 부른다."""
    now = now or time.time()
    st = state()
    out: dict = {}
    for kind in kinds():
        got = {"polls": 0, "feedback": 0, "forwarded": 0}
        changed: set[str] = set()
        offs = (st.get("offset") or {}).get(kind)
        for u in _updates(kind, offs):
            st.setdefault("offset", {})[kind] = u["update_id"] + 1
            poll = u.get("poll")
            if poll and poll.get("id"):
                mk, name = _poll_month(st, poll["id"]), _poll_name(st, poll["id"])
                if mk:
                    st.setdefault("results", {}).setdefault(mk, {})[name or poll["id"]] = {
                        "q": poll.get("question", ""), "total": poll.get("total_voter_count", 0),
                        "opts": [{"text": o.get("text", ""), "votes": o.get("voter_count", 0)}
                                 for o in poll.get("options") or []], "at": int(now)}
                    got["polls"] += 1
            if poll and poll.get("id") and _poll_month(st, poll["id"]):
                changed.add(_poll_month(st, poll["id"]))
            m = u.get("message") or {}
            text = (m.get("text") or m.get("caption") or "").strip()
            if text and (m.get("chat") or {}).get("type") == "private" and not text.startswith("/"):
                frm = m.get("from") or {}
                who = f"@{frm['username']}" if frm.get("username") else (frm.get("first_name") or "익명")
                got["feedback"] += 1
                if _forward(kind, text, who, now):
                    got["forwarded"] += 1
                _save_feedback(kind, who, text, now)
                st["feedback_n"] = int(st.get("feedback_n") or 0) + 1
        for mk in sorted(changed):   # 결과는 방이 아니라 운영자에게만
            got["reported"] = _report_admin(kind, mk, st, now) or got.get("reported", False)
        out[kind] = got
    save_state(st)
    return out


def _report_admin(kind: str, month: str, st: dict, now: float) -> bool:
    """만족도 집계를 운영자 채팅으로만 보낸다(방에는 발표하지 않는다)."""
    admin = env("TELEGRAM_ADMIN_CHAT_ID", "")
    lines = summary_lines(month, st)
    if not admin or not lines:
        return False
    head = f"<b>📊 {month} 만족도 조사 집계</b> (방에는 나가지 않습니다)"
    body = "\n".join([head] + [telegram.to_html(l) for l in lines])
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


# ─────────────────────────── 결과 ───────────────────────────
def score(res: dict) -> float | None:
    """만족도 5점 평균."""
    opts = res.get("opts") or []
    pts = SATISFACTION["score"]
    tot = sum(o["votes"] for o in opts)
    if not tot or len(opts) != len(pts):
        return None
    return round(sum(o["votes"] * p for o, p in zip(opts, pts)) / tot, 2)


def results(month: str = "", st: dict | None = None) -> dict:
    st = st if st is not None else state()
    all_res = st.get("results") or {}
    if month:
        return all_res.get(month) or {}
    return all_res


def summary_lines(month: str, st: dict | None = None) -> list[str]:
    r = results(month, st)
    if not r:
        return []
    out = []
    sat = r.get("satisfaction")
    if sat:
        s = score(sat)
        top = max(sat["opts"], key=lambda o: o["votes"]) if sat["opts"] else None
        out.append(f"• 만족도 *{s if s is not None else '—'}점* / 5점 (응답 {sat['total']}명"
                   + (f" · 가장 많은 답 '{top['text']}'" if top and top["votes"] else "") + ")")
    wish = r.get("wish")
    if wish and wish["opts"]:
        top3 = sorted(wish["opts"], key=lambda o: -o["votes"])[:3]
        picks = " · ".join(f"{o['text']} {o['votes']}표" for o in top3 if o["votes"])
        if picks:
            out.append(f"• 가장 원하는 것: {picks}")
    return out


def last_results(st: dict, before: str = "") -> list[str]:
    """운영자 보고·수동 조회용. 방에 나가는 메시지에는 쓰지 않는다."""
    months = sorted(m for m in (st.get("results") or {}) if not before or m < before)
    if not months:
        return []
    lines = summary_lines(months[-1], st)
    return ([f"• 대상 기간: {months[-1]}"] + lines) if lines else []


# ─────────────────────────── 실행 ───────────────────────────
def tick(now: float | None = None, send_now: bool = True, force: bool = False) -> dict:
    if not enabled():
        return {"status": "disabled"}
    now = now or time.time()
    out: dict = {"collected": collect(now), "sweep": sweep(now)}
    if force or due(now):
        out["sent"] = send(now) if send_now else {"status": "dry"}
    return out

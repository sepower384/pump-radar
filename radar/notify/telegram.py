"""텔레그램 봇 전송 — 슈퍼그룹 토픽(kind별 스레드)으로 보낸다. requests 만 사용.

환경변수
  TELEGRAM_BOT_TOKEN_PUMP / _TREND / _STOCK  kind별 봇 토큰 (없으면 아래 공용 토큰)
  TELEGRAM_BOT_TOKEN                          공용 봇 토큰
  TELEGRAM_CHAT_ID                            슈퍼그룹 id (-100…)
  TELEGRAM_CHAT_ID_PUMP / _TREND / _STOCK     kind별 전용 방(채널·그룹) id — 있으면 공용 CHAT_ID 대신
  TELEGRAM_TOPIC_PUMP / _TREND / _STOCK       토픽 스레드 id (없으면 일반 채팅으로)
토큰이나 CHAT_ID 가 없으면 조용히 건너뛴다(status=skipped).
"""
from __future__ import annotations

import html
import re
import time

import requests

from ..config import env

LIMIT = 4096          # sendMessage 본문 제한
CAPTION_LIMIT = 1024  # sendPhoto 캡션 제한
GAP_SEC = 1.0         # 여러 메시지 사이 간격

_TOKEN_ENV = {"pump": "TELEGRAM_BOT_TOKEN_PUMP", "trend": "TELEGRAM_BOT_TOKEN_TREND",
              "stock": "TELEGRAM_BOT_TOKEN_STOCK"}
_CHAT_ENV = {"pump": "TELEGRAM_CHAT_ID_PUMP", "trend": "TELEGRAM_CHAT_ID_TREND",
             "stock": "TELEGRAM_CHAT_ID_STOCK"}
_TOPIC_ENV = {"pump": "TELEGRAM_TOPIC_PUMP", "trend": "TELEGRAM_TOPIC_TREND",
              "stock": "TELEGRAM_TOPIC_STOCK"}

# 슬랙 이모지 코드 → 유니코드. 텔레그램은 :rocket: 을 글자 그대로 보여주므로 반드시 변환.
# (현재 코드베이스는 유니코드 이모지를 직접 쓰지만, 슬랙 문구 재활용 대비 흔한 코드는 전부 둔다.
#  tests/test_all.py 가 코드베이스의 :코드: 가 여기 다 있는지 검사한다.)
EMOJI = {
    "rocket": "🚀", "point_right": "👉", "dollar": "💵", "moneybag": "💰", "thinking_face": "🤔",
    "newspaper": "📰", "mag_right": "🔎", "mag": "🔍", "link": "🔗", "new": "🆕",
    "chart_with_upwards_trend": "📈", "chart_with_downwards_trend": "📉", "bar_chart": "📊",
    "rotating_light": "🚨", "warning": "⚠️", "fire": "🔥", "small_red_triangle_down": "🔻",
    "small_red_triangle": "🔺", "classical_building": "🏛", "jigsaw": "🧩", "ocean": "🌊",
    "roller_coaster": "🎢", "crescent_moon": "🌙", "lock": "🔒", "speech_balloon": "💬",
    "question": "❓", "white_check_mark": "✅", "x": "❌", "flag-kr": "🇰🇷", "flag-us": "🇺🇸",
    "kr": "🇰🇷", "us": "🇺🇸", "satellite": "🛰", "chart": "💹", "eyes": "👀", "bulb": "💡",
    "memo": "📝", "pushpin": "📌", "calendar": "📅", "clock3": "🕒", "star": "⭐",
    "tada": "🎉", "arrow_up": "⬆️", "arrow_down": "⬇️", "heavy_plus_sign": "➕",
    "large_green_circle": "🟢", "red_circle": "🔴", "large_yellow_circle": "🟡",
    "information_source": "ℹ️", "loudspeaker": "📢", "zap": "⚡", "gem": "💎",
    "whale": "🐳", "chart_increasing": "📈", "chart_decreasing": "📉",
}

_EMOJI_RE = re.compile(r":([a-z0-9_+\-]+):")
_LINK = re.compile(r"<(https?://[^|<>\s]+)\|([^<>]*)>")
_BARE = re.compile(r"<(https?://[^|<>\s]+)>")
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"(?<![A-Za-z0-9*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![A-Za-z0-9*])")
_ITAL = re.compile(r"(?<![A-Za-z0-9_])_(?=\S)([^_\n]+?)(?<=\S)_(?![A-Za-z0-9_])")
_TAG = re.compile(r"<[^>]+>")


class TelegramError(RuntimeError):
    pass


# ─────────────────────────── 변환 ───────────────────────────
def emojify(text: str) -> str:
    return _EMOJI_RE.sub(lambda m: EMOJI.get(m.group(1), m.group(0)), text)


def to_html(text: str) -> str:
    """슬랙 mrkdwn → 텔레그램 HTML. 줄 단위로 변환해 태그가 줄을 넘지 않게 한다."""
    return "\n".join(_line_to_html(ln) for ln in text.split("\n"))


def _line_to_html(line: str) -> str:
    line = emojify(line)
    keep: list[str] = []

    def stash(s: str) -> str:
        keep.append(s)
        return f"\x00{len(keep) - 1}\x00"

    line = _LINK.sub(lambda m: stash(
        f'<a href="{html.escape(m.group(1), quote=True)}">{html.escape(m.group(2).strip() or m.group(1))}</a>'), line)
    line = _BARE.sub(lambda m: stash(
        f'<a href="{html.escape(m.group(1), quote=True)}">{html.escape(m.group(1))}</a>'), line)
    line = _CODE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), line)
    line = html.escape(line, quote=False)
    line = _BOLD.sub(r"<b>\1</b>", line)
    line = _ITAL.sub(r"<i>\1</i>", line)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], line)


def strip_tags(h: str) -> str:
    return html.unescape(_TAG.sub("", h))


# ─────────────────────────── 분할 ───────────────────────────
def _hard_cut(line: str, limit: int) -> list[str]:
    """한 줄이 제한보다 길면 태그를 벗기고 평문으로 자른다(엔티티 중간 절단 방지)."""
    plain = html.escape(strip_tags(line), quote=False)
    out = []
    while len(plain) > limit:
        cut = limit
        amp = plain.rfind("&", max(0, cut - 8), cut)
        if amp >= 0 and ";" not in plain[amp:cut]:
            cut = amp
        out.append(plain[:cut])
        plain = plain[cut:]
    if plain:
        out.append(plain)
    return out


def _split_unit(unit: str, limit: int) -> list[str]:
    """하나의 단위(코인 한 개 분량)가 너무 길면 줄 경계로 나눈다."""
    parts: list[str] = []
    cur = ""
    for line in unit.split("\n"):
        pieces = [line] if len(line) <= limit else _hard_cut(line, limit)
        for p in pieces:
            cand = f"{cur}\n{p}" if cur else p
            if len(cand) <= limit:
                cur = cand
            else:
                if cur:
                    parts.append(cur)
                cur = p
    if cur:
        parts.append(cur)
    return parts


def split_messages(units: list[str], limit: int = LIMIT) -> list[str]:
    """HTML 단위들(헤더, 코인1, 코인2, …, 꼬리말)을 빈 줄로 이어 붙이되 limit 을 넘으면 새 메시지로.
    단위 경계 → 줄 경계 순으로만 자르므로 태그가 중간에 끊기지 않는다."""
    reserve = 40  # "(이어서 2/3)" 머리말 자리
    room = limit - reserve
    pieces: list[str] = []
    for u in units:
        u = u.strip("\n")
        if not u:
            continue
        pieces += [u] if len(u) <= room else _split_unit(u, room)
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        cand = f"{cur}\n\n{p}" if cur else p
        if len(cand) <= room:
            cur = cand
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    n = len(chunks)
    if n > 1:
        chunks = [c if i == 0 else f"<i>(이어서 {i + 1}/{n})</i>\n{c}" for i, c in enumerate(chunks)]
    return chunks


# ─────────────────────────── 설정 ───────────────────────────
def token_for(kind: str) -> str:
    """kind 전용 봇 토큰 우선, 없으면 공용 TELEGRAM_BOT_TOKEN."""
    return env(_TOKEN_ENV.get(kind, ""), "") or env("TELEGRAM_BOT_TOKEN", "")


def chat_id(kind: str = "") -> str:
    """kind 전용 방 우선, 없으면 공용 TELEGRAM_CHAT_ID."""
    return env(_CHAT_ENV.get(kind, ""), "") or env("TELEGRAM_CHAT_ID", "")


def _own_chat(kind: str) -> bool:
    return bool(env(_CHAT_ENV.get(kind, ""), ""))


def topic_for(kind: str) -> int | None:
    if _own_chat(kind):   # 전용 방(채널 등)으로 보내면 공용 그룹의 토픽 id 는 의미가 없다
        return None
    v = env(_TOPIC_ENV.get(kind, ""), "")
    try:
        return int(v) if v else None
    except ValueError:
        return None


def available(kind: str = "") -> bool:
    return bool(token_for(kind) and chat_id(kind))


def list_chats(get=requests.get) -> dict:
    """kind별 봇이 초대된 방(채널·그룹) id 찾기 — 봇을 방에 넣은 뒤 실행. 토큰은 출력하지 않는다."""
    out: dict = {}
    for kind in ("pump", "trend", "stock"):
        token = token_for(kind)
        if not token:
            out[kind] = "토큰 없음"
            continue
        api = f"https://api.telegram.org/bot{token}/"
        try:
            get(api + "deleteWebhook", timeout=20)  # 웹훅이 있으면 getUpdates 가 막힌다
            data = get(api + "getUpdates", timeout=20, params={
                "allowed_updates": '["message","channel_post","my_chat_member"]'}).json()
        except Exception as e:  # noqa: BLE001
            out[kind] = _redact(f"조회 실패: {type(e).__name__}: {e}", token)
            continue
        seen: dict = {}
        for u in data.get("result", []):
            for k in ("my_chat_member", "channel_post", "message"):
                if k in u:
                    c = u[k]["chat"]
                    st = (u[k].get("new_chat_member") or {}).get("status")
                    prev = seen.get(str(c["id"]), {})
                    seen[str(c["id"])] = {"type": c.get("type"),
                                          "title": c.get("title") or c.get("username") or c.get("first_name"),
                                          "status": st or prev.get("status", "")}
        out[kind] = seen or "받은 기록 없음 — 방에서 봇을 뺐다가 다시 넣거나 방에 글 하나 올린 뒤 재실행"
    return out


# ─────────────────────────── 전송 ───────────────────────────
def _redact(s: str, token: str) -> str:
    return s.replace(token, "***") if token else s


def _call(token: str, method: str, payload: dict, timeout: int = 20) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    for attempt in (0, 1):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — 예외 문자열에 URL(토큰)이 섞이므로 가린다
            raise TelegramError(_redact(f"{method} 네트워크 오류: {type(e).__name__}: {e}", token)) from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 429 and attempt == 0:
            wait = (data.get("parameters") or {}).get("retry_after") or 1
            time.sleep(min(float(wait), 60.0))
            continue
        if r.status_code != 200 or not data.get("ok"):
            desc = data.get("description") or r.text[:200]
            raise TelegramError(_redact(f"{method} {r.status_code}: {desc}", token))
        return data
    raise TelegramError(f"{method} 429 재시도 후에도 실패")


def _base(kind: str) -> dict:
    p: dict = {"chat_id": chat_id(kind)}
    t = topic_for(kind)
    if t is not None:
        p["message_thread_id"] = t
    return p


def send_message(kind: str, text_html: str) -> dict:
    token = token_for(kind)
    payload = {**_base(kind), "text": text_html, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    try:
        return _call(token, "sendMessage", payload)
    except TelegramError as e:
        if "can't parse entities" not in str(e):
            raise
        # HTML 파싱이 깨졌을 때만: 태그를 벗긴 평문으로 한 번 더 (텍스트는 반드시 나가야 한다)
        payload.pop("parse_mode")
        payload["text"] = strip_tags(text_html)[:LIMIT]
        return _call(token, "sendMessage", payload)


def send_photo(kind: str, photo_url: str, caption_html: str = "") -> dict:
    payload = {**_base(kind), "photo": photo_url}
    if caption_html:
        cap = caption_html if len(caption_html) <= CAPTION_LIMIT else \
            html.escape(strip_tags(caption_html), quote=False)[:CAPTION_LIMIT]
        payload.update(caption=cap, parse_mode="HTML")
    return _call(token_for(kind), "sendPhoto", payload, timeout=25)


def send(kind: str, chunks: list[str], photo: str = "", caption: str = "") -> dict:
    """사진(있으면, 실패 무시) → 본문 조각들. 결과 dict 는 run_once JSON 에 그대로 실린다."""
    if not available(kind):
        return {"status": "skipped", "reason": "텔레그램 토큰/CHAT_ID 미설정"}
    res: dict = {"status": "error", "sent": 0, "total": len(chunks), "photo": None, "errors": []}
    if photo:
        try:
            send_photo(kind, photo, caption)
            res["photo"] = "ok"
            time.sleep(GAP_SEC)
        except Exception as e:  # noqa: BLE001 — 사진은 실패해도 본문은 보낸다
            res["photo"] = "failed"
            res["errors"].append(f"photo: {e}")
    for i, c in enumerate(chunks):
        if i:
            time.sleep(GAP_SEC)
        try:
            send_message(kind, c)
            res["sent"] += 1
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"message {i + 1}: {e}")
    res["status"] = ("ok" if res["sent"] == res["total"] and res["total"] else
                     "partial" if res["sent"] else "error")
    if not res["errors"]:
        res.pop("errors")
    return res

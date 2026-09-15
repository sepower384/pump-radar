"""백엔드 선택 + 실패 시 폴백. 어떤 경우에도 알림을 '잃지는' 않는다(파일로 남김).

슬랙과 텔레그램은 서로 독립이다 — 한쪽 예외가 다른 쪽 전송을 막지 않는다.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..config import CFG, DATA_DIR, env
from . import slack_cdp, slack_playwright, slack_webhook, telegram

OUTBOX: Path = DATA_DIR / "outbox"
OUTBOX.mkdir(exist_ok=True)


def _archive(kind: str, text: str, note: str) -> None:
    stamp = time.strftime("%Y%m%d")
    with (OUTBOX / f"{stamp}.md").open("a", encoding="utf-8") as f:
        f.write(f"\n\n---\n### [{time.strftime('%H:%M:%S')}] {kind} ({note})\n{text}\n")


def _send_slack(kind: str, text: str, blocks: list | None) -> tuple[str, list[str]]:
    # 클라우드(Actions)는 로그인 크롬이 없으니 SLACK_BACKEND=webhook 으로 config 를 덮어쓴다
    backend = (env("SLACK_BACKEND") or CFG.get("slack.backend", "auto") or "auto").lower()
    used = "none"
    errors: list[str] = []

    if backend in ("auto", "webhook") and slack_webhook.available(kind):
        try:
            slack_webhook.send(kind, text, blocks)
            used = "webhook"
        except Exception as e:  # noqa: BLE001
            errors.append(f"webhook: {e}")

    if used == "none" and backend in ("auto", "playwright"):
        pw = CFG.section("slack").get("playwright", {})
        try:
            if pw.get("channel_url"):
                slack_cdp.post(pw["channel_url"], text,
                               cdp=pw.get("cdp_url") or f"http://127.0.0.1:{int(pw.get('cdp_port', 9222))}")
            else:
                slack_playwright.send(
                    text,
                    cdp_port=int(pw.get("cdp_port", 9222)),
                    workspace_url=pw.get("workspace_url", "https://app.slack.com/client"),
                    channel=pw.get("channel", ""),
                )
            used = "playwright"
        except Exception as e:  # noqa: BLE001
            errors.append(f"playwright: {e}")
    return used, errors


def send(kind: str, text: str, blocks: list | None = None) -> str:
    """슬랙만 보내는 구 경로(연결 테스트용). 반환값: 실제로 사용한 백엔드 이름."""
    used, errors = _send_slack(kind, text, blocks)
    _archive(kind, text, used if used != "none" else " / ".join(errors) or "미전송")
    return used


def deliver(msg) -> dict:
    """Msg 하나를 슬랙 + 텔레그램으로 동시 발송.

    반환 {"slack": 백엔드, "telegram": {...}, "delivered": 둘 중 하나라도 성공}
    호출부는 delivered 가 True 일 때만 쿨다운을 기록한다.
    """
    kind = msg.kind
    text = msg.slack_text()
    try:
        slack_used, slack_errors = _send_slack(kind, text, msg.slack_blocks())
    except Exception as e:  # noqa: BLE001 — 설정 오류 등 예상 밖 예외도 텔레그램을 막지 않게
        slack_used, slack_errors = "none", [f"slack: {type(e).__name__}: {e}"]

    try:
        chunks = msg.telegram_chunks()
        tg = telegram.send(kind, chunks, msg.photo, msg.caption)
    except Exception as e:  # noqa: BLE001
        chunks = []
        tg = {"status": "error", "errors": [f"{type(e).__name__}: {e}"]}

    delivered = slack_used != "none" or tg.get("status") in ("ok", "partial")
    out: dict = {"slack": slack_used, "telegram": tg, "delivered": delivered}
    if slack_errors:
        out["slack_errors"] = slack_errors

    slack_note = slack_used if slack_used != "none" else (" / ".join(slack_errors) or "미전송")
    _archive(kind, text, f"slack={slack_note} · telegram={tg.get('status')}")
    if chunks and tg.get("status") != "skipped":
        _archive(kind + " telegram", "\n\n".join(chunks), f"{len(chunks)}개 · photo={tg.get('photo')}")
    return out

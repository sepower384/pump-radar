"""백엔드 선택 + 실패 시 폴백. 어떤 경우에도 알림을 '잃지는' 않는다(파일로 남김)."""
from __future__ import annotations

import time
from pathlib import Path

from ..config import CFG, DATA_DIR
from . import slack_cdp, slack_playwright, slack_webhook

OUTBOX: Path = DATA_DIR / "outbox"
OUTBOX.mkdir(exist_ok=True)


def _archive(kind: str, text: str, note: str) -> None:
    stamp = time.strftime("%Y%m%d")
    with (OUTBOX / f"{stamp}.md").open("a", encoding="utf-8") as f:
        f.write(f"\n\n---\n### [{time.strftime('%H:%M:%S')}] {kind} ({note})\n{text}\n")


def send(kind: str, text: str, blocks: list | None = None) -> str:
    """반환값: 실제로 사용한 백엔드 이름."""
    backend = (CFG.get("slack.backend", "auto") or "auto").lower()
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
                               cdp=f"http://127.0.0.1:{int(pw.get('cdp_port', 9222))}")
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

    _archive(kind, text, used if used != "none" else " / ".join(errors) or "미전송")
    return used

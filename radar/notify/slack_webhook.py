"""Slack Incoming Webhook 전송 (권장 경로 — PC가 꺼져 있어도 서버에서 그대로 동작)."""
from __future__ import annotations

import json

import requests

from ..config import env

_KIND_ENV = {
    "trend": "SLACK_WEBHOOK_TREND",
    "pump": "SLACK_WEBHOOK_PUMP",
    "stock": "SLACK_WEBHOOK_STOCK",
}


def webhook_for(kind: str) -> str:
    return env(_KIND_ENV.get(kind, ""), "") or env("SLACK_WEBHOOK_URL", "")


def available(kind: str = "") -> bool:
    return bool(webhook_for(kind))


def send(kind: str, text: str, blocks: list | None = None) -> bool:
    url = webhook_for(kind)
    if not url:
        return False
    payload: dict = {"text": text[:2900]}
    if blocks:
        payload["blocks"] = blocks[:48]
    r = requests.post(url, data=json.dumps(payload).encode("utf-8"), timeout=15,
                      headers={"Content-Type": "application/json"})
    if r.status_code != 200 or r.text.strip() != "ok":
        raise RuntimeError(f"Slack webhook 실패 {r.status_code}: {r.text[:200]}")
    return True

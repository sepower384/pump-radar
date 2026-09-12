"""이미 로그인해 둔 크롬으로 슬랙에 직접 쳐 넣는 백엔드.

웹훅을 못 만들 때의 대안. 단점이 분명하니 알고 쓸 것:
  - 그 PC가 켜져 있고 크롬이 떠 있어야 한다(원격 서버/PC 종료 시 불가).
  - 슬랙 DOM이 바뀌면 셀렉터가 깨질 수 있다.
크롬을 원격 디버깅 포트로 띄워두면 새 창이 안 뜬다:
  chrome.exe --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\\ChromeRadar"
"""
from __future__ import annotations

import time

CHANNEL_INPUT = 'div[data-qa="message_input"] div[contenteditable="true"], ' \
                'div[contenteditable="true"][role="textbox"]'


def _find_slack_page(browser):
    for ctx in browser.contexts:
        for page in ctx.pages:
            if "slack.com" in (page.url or ""):
                return page
    return None


def send(text: str, cdp_port: int = 9222, workspace_url: str = "https://app.slack.com/client",
         channel: str = "") -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"CDP {cdp_port} 연결 실패 — 크롬을 --remote-debugging-port={cdp_port} 로 "
                f"띄워야 함 ({e})") from e

        page = _find_slack_page(browser)
        if page is None:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            page.goto(workspace_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)

        if channel:
            try:
                page.keyboard.press("Control+K")
                page.wait_for_timeout(900)
                page.keyboard.type(channel.lstrip("#"), delay=35)
                page.wait_for_timeout(1400)
                page.keyboard.press("Enter")
                page.wait_for_timeout(2200)
            except Exception:  # noqa: BLE001
                pass

        box = page.wait_for_selector(CHANNEL_INPUT, timeout=20000)
        box.click()
        page.wait_for_timeout(300)

        # 슬랙 입력창은 Enter가 전송이므로 줄바꿈은 Shift+Enter로 넣는다.
        for i, line in enumerate(text.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")
            if line:
                page.keyboard.type(line, delay=4)
        page.wait_for_timeout(400)
        page.keyboard.press("Enter")
        time.sleep(1.2)
        return True

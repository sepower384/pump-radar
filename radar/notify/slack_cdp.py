# -*- coding: utf-8 -*-
"""로그인된 크롬으로 슬랙 채널에 직접 입력 — 뉴스레이더/펌프레이더/마켓브리핑 공용 사본.

- 기본 경로는 Playwriter 확장 릴레이(ws://127.0.0.1:19988/cdp?extensionId=...):
  평소 쓰는 크롬(이미 슬랙 로그인됨)에 붙는다. 릴레이는 C:\dev\slack-chrome\start_relay.vbs 가 띄운다.
  http://127.0.0.1:9222 같은 일반 CDP 주소도 그대로 받는다.

- 채널마다 새 탭을 열고 보내고 닫는다. 기존 슬랙 탭(사람이 보고 있는 화면)은 건드리지 않는다.
- 세 봇이 같은 크롬을 같이 쓰므로 PC 전역 파일락으로 한 번에 하나씩만 보낸다.
  (락 없이 동시에 치면 키 입력이 섞여 엉뚱한 채널로 들어갈 수 있다)
- 웹 입력창은 <url|라벨> 문법을 링크로 안 바꿔주므로 "라벨 url" 로 풀어서 친다.
"""
import os
import random
import re
import time

LOCK_PATH = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                         "ChromeRadar", "slack_send.lock")
INPUT_SELECTORS = ('div[data-qa="message_input"] div[contenteditable="true"]',
                   'div[role="textbox"][contenteditable="true"]',
                   '.ql-editor[contenteditable="true"]')
_LINK = re.compile(r"<(https?://[^|>\s]+)\|([^>]*)>")
_BARE = re.compile(r"<(https?://[^|>\s]+)>")


def desk_text(text):
    """슬랙 mrkdwn 링크를 웹 입력창용 평문으로."""
    def _sub(m):
        url, label = m.group(1), m.group(2).strip()
        if not label or label in ("↗", "링크", "link"):
            return url
        return "%s %s" % (label, url)
    return _BARE.sub(r"\1", _LINK.sub(_sub, text))


class _Lock:
    def __init__(self, path=LOCK_PATH, timeout=180):
        self.path, self.timeout, self.fh = path, timeout, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+")
        deadline = time.time() + self.timeout
        while True:
            try:
                try:
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                except ImportError:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > deadline:
                    self.fh.close()
                    raise RuntimeError("슬랙 전송 락 대기 초과(%ds) — 다른 봇이 전송 중" % self.timeout)
                time.sleep(0.5)

    def __exit__(self, *exc):
        try:
            try:
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                pass
        except OSError:
            pass
        self.fh.close()


def resolve_endpoint(cdp):
    """Playwriter 릴레이는 연결마다 고유 clientId 가 필요하다: /cdp -> /cdp/<id>."""
    m = re.match(r"^(wss?://[^/]+/cdp)/?(\?.*)?$", cdp or "")
    if not m:
        return cdp
    return "%s/bot%d_%d%s" % (m.group(1), random.randint(1, 10 ** 9), int(time.time() * 1000), m.group(2) or "")


def post(channel_url, text, cdp="http://127.0.0.1:9222"):
    """channel_url 예) https://app.slack.com/client/T0XXXX/C0XXXX"""
    if not (channel_url or "").strip():
        raise RuntimeError("슬랙 채널 URL이 비어 있음")
    from playwright.sync_api import sync_playwright

    text = desk_text(text)
    with _Lock(), sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(resolve_endpoint(cdp), timeout=15000)
        except Exception as e:
            raise RuntimeError("브라우저 연결 실패(%s) — 크롬이 꺼졌거나 Playwriter 릴레이/확장이 연결 안 됨: %s"
                               % (cdp.split("?")[0], e))
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        try:
            page.goto(channel_url, wait_until="domcontentloaded", timeout=90000)
            if "signin" in page.url or "workspace-signin" in page.url:
                raise RuntimeError("슬랙 로그아웃 상태 — 크롬에서 슬랙 다시 로그인 필요")
            try:
                box = page.wait_for_selector(", ".join(INPUT_SELECTORS), state="visible", timeout=45000)
            except Exception:
                box = None
            if box is None:
                if "signin" in page.url:
                    raise RuntimeError("슬랙 로그아웃 상태 — 크롬에서 슬랙 다시 로그인 필요")
                raise RuntimeError("슬랙 입력창을 못 찾음 (채널 URL/권한 확인): %s" % page.url)
            page.wait_for_timeout(800)
            box.click()
            # Enter=전송이라 줄바꿈은 Shift+Enter
            for i, line in enumerate(text.split("\n")):
                if i:
                    page.keyboard.press("Shift+Enter")
                if line:
                    page.keyboard.insert_text(line)
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
        finally:
            try:
                page.close()
            except Exception:
                pass
    return True

"""텔레그램 미리보기 — 실제 데이터로 3종 메시지를 만들어 파일로만 저장한다.

- 전송하지 않는다(슬랙·텔레그램 모두).
- 알림기록/쿨다운/관찰 콜 DB 를 오염시키지 않는다: radar.db 를 임시 폴더에 복사해 그 사본으로 스캔하고,
  mark_alerted·add_call 은 아예 호출하지 않으며, 끝나면 사본을 지운다.
- 결과: data/outbox/preview_telegram_<kind>.html (텔레그램 HTML 원문 + 분할 결과 + 사진 URL + 슬랙 텍스트)
        data/outbox/preview_slack_<kind>.md      (슬랙 텍스트 미리보기)
  stock 은 미국·한국이 각각 별도 메시지라 한 파일 안에 두 묶음으로 보여준다.
"""
from __future__ import annotations

import html
import shutil
import tempfile
import time
import traceback
from pathlib import Path

from . import runner, store
from .config import CFG
from .engine import reason
from .notify import telegram
from .notify.message import BOT_NAME, Msg
from .notify.sender import OUTBOX

_CSS = """
body{font-family:-apple-system,'Segoe UI','Malgun Gothic',sans-serif;max-width:780px;margin:0 auto;
     padding:24px 16px;background:#dfe6ee;color:#111}
h1{font-size:20px;margin:0 0 4px} h2{font-size:17px;margin:32px 0 8px} h3{font-size:15px;margin:22px 0 8px}
.meta{color:#445;font-size:13px;line-height:1.6}
.bubble{background:#fff;border-radius:14px;padding:10px 14px;margin:10px 0;white-space:pre-wrap;
        line-height:1.5;font-size:15px;box-shadow:0 1px 2px rgba(0,0,0,.12)}
.bubble a{color:#1a73c8} .bubble code{background:#f1f3f5;padding:0 3px;border-radius:3px}
.len{font-size:12px;color:#667;margin-top:6px}
pre{white-space:pre-wrap;word-break:break-all;background:#1f2329;color:#dfe3e8;padding:10px;
    border-radius:8px;font-size:12px}
img{max-width:100%;border-radius:10px;display:block}
.warn{background:#fff4d6;border-radius:8px;padding:8px 12px;font-size:13px}
"""

_TOPIC_ENV = {"pump": "TELEGRAM_TOPIC_PUMP", "trend": "TELEGRAM_TOPIC_TREND", "stock": "TELEGRAM_TOPIC_STOCK"}
_TOKEN_ENV = {"pump": "TELEGRAM_BOT_TOKEN_PUMP", "trend": "TELEGRAM_BOT_TOKEN_TREND",
              "stock": "TELEGRAM_BOT_TOKEN_STOCK"}


def _msg_section(msg: Msg, label: str) -> list[str]:
    esc = html.escape
    chunks = msg.telegram_chunks()
    out = [f"<h2>{esc(label)}</h2>", "<h3>1) sendPhoto (메시지당 최대 1장, 실패해도 본문은 전송)</h3>"]
    if msg.photo:
        out.append(f"<div class='meta'>사진 URL ({len(msg.photo)}자):<br><a href='{esc(msg.photo, quote=True)}'>"
                   f"{esc(msg.photo)}</a></div>")
        out.append(f"<div class='bubble'><img src='{esc(msg.photo, quote=True)}' alt='chart'>"
                   f"{msg.caption}</div><div class='len'>캡션 {len(msg.caption)}자 / 1024</div>")
    else:
        out.append("<p class='meta'>사진 없음 (텍스트만 보냅니다)</p>")
    out.append(f"<h3>2) sendMessage × {len(chunks)} (parse_mode=HTML, 링크 미리보기 끔, 사이 1초)</h3>")
    for i, c in enumerate(chunks, 1):
        out.append(f"<div class='bubble'>{c}</div><div class='len'>메시지 {i}/{len(chunks)} · {len(c)}자 / 4096</div>")
    out.append("<h3>3) 텔레그램 HTML 원문</h3>")
    for i, c in enumerate(chunks, 1):
        out.append(f"<div class='meta'>메시지 {i}</div><pre>{esc(c)}</pre>")
    if msg.caption:
        out.append(f"<div class='meta'>사진 캡션</div><pre>{esc(msg.caption)}</pre>")
    out.append("<h3>4) 슬랙 텍스트 (같은 원본)</h3>")
    out.append(f"<pre>{esc(msg.slack_text())}</pre>")
    return out


def _page(kind: str, msgs: list[tuple[str, Msg]], info: dict) -> str:
    esc = html.escape
    out = [f"<title>텔레그램 미리보기 · {kind}</title><style>{_CSS}</style>",
           f"<h1>텔레그램 미리보기 · {kind}</h1>",
           f"<div class='meta'>봇: {BOT_NAME} · 생성: {time.strftime('%Y-%m-%d %H:%M:%S')} · 실제 전송 없음<br>"
           f"토픽 스레드: <code>{_TOPIC_ENV[kind]}</code> · 봇 토큰: <code>{_TOKEN_ENV[kind]}</code> → "
           f"없으면 <code>TELEGRAM_BOT_TOKEN</code><br>스캔 결과: <code>{esc(str(info))}</code></div>"]
    if not msgs:
        out.append("<p class='warn'>이번 스캔에서는 보낼 메시지가 없습니다.</p>")
    if info.get("sample"):
        out.append(f"<p class='warn'>미리보기용 대체 데이터입니다: {esc(str(info['sample']))}</p>")
    for label, m in msgs:
        out += _msg_section(m, label)
    return "\n".join(out)


def run(only: str = "all") -> dict:
    orig = store.DB_PATH
    tmpdir = Path(tempfile.mkdtemp(prefix="pump_radar_preview_"))
    tmp_db = tmpdir / "radar.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(orig) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(tmp_db) + suffix))
    store.DB_PATH = tmp_db
    results: dict = {"db": "임시 사본 사용(원본 알림기록·관찰 콜 기록 무변경)", "files": {}}
    try:
        con = store.connect()
        ctx = None
        if only in ("all", "pump"):
            try:
                ctx = reason.MarketContext.build(int(CFG.get("reason.news_lookback_hours", 24)))
            except Exception:  # noqa: BLE001
                ctx = reason.MarketContext()

        def single(fn):
            msg, _marks, info = fn()   # marks 는 버린다 — 알림기록 안 함
            return ([("메시지", msg)] if msg is not None else []), info

        def stock():
            jobs, info = runner.compose_stock(con, respect_cooldown=False, preview=True)
            return [(f"{j['market']} 메시지 (새 관찰 콜 {len(j['calls'])}건 — 미리보기라 기록 안 함)", j["msg"])
                    for j in jobs], info

        jobs = (
            ("pump", lambda: single(lambda: runner.compose_pump(con, ctx, respect_cooldown=False, preview=True))),
            ("trend", lambda: single(lambda: runner.compose_trend(con, respect_cooldown=False, preview=True))),
            ("stock", stock),
        )
        for kind, fn in jobs:
            if only not in ("all", kind):
                continue
            try:
                msgs, info = fn()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                msgs, info = [], {"error": f"{type(e).__name__}: {e}"}
            page = OUTBOX / f"preview_telegram_{kind}.html"
            page.write_text(_page(kind, msgs, info), encoding="utf-8")
            entry: dict = {"html": str(page), "info": info}
            if msgs:
                slack = OUTBOX / f"preview_slack_{kind}.md"
                slack.write_text("\n\n==========\n\n".join(m.slack_text() for _, m in msgs) + "\n",
                                 encoding="utf-8")
                entry["slack"] = str(slack)
                entry["messages"] = [{"label": label, "telegram_messages": len(m.telegram_chunks()),
                                      "lengths": [len(c) for c in m.telegram_chunks()],
                                      "photo": bool(m.photo), "photo_url_len": len(m.photo)}
                                     for label, m in msgs]
            results["files"][kind] = entry
        con.close()
    finally:
        store.DB_PATH = orig
        shutil.rmtree(tmpdir, ignore_errors=True)
    results["telegram_configured"] = {k: telegram.available(k) for k in ("pump", "trend", "stock")}
    return results

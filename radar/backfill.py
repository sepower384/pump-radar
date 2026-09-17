"""보고서용 과거 기록 채우기 — history 가 생기기 전(2026-09-13~) 보낸 알림을 outbox 원문에서 읽어온다.

outbox(data/outbox/YYYYMMDD.md)는 클라우드 캐시에 남아 있는 슬랙 형식 원문이다.
형식이 조금씩 바뀌어 왔으므로 줄 단위로 느슨하게 읽고, 못 읽는 조각은 건너뛴다.
한 번 채운 파일은 history/backfill.json 에 적어 두 번 넣지 않는다.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import history
from .notify.sender import OUTBOX

_SECTION = re.compile(r"^### \[(\d\d):(\d\d):(\d\d)\] (\S+)(?: \((.*)\))?\s*$")
_COIN = re.compile(r"^(🚀|🔻) \*([A-Za-z0-9]+)\* — (.+)$")
_CHG = re.compile(r"(5분 만에|15분 만에|1시간 동안|2시간 동안|하루 동안) `([+-]?[\d.]+)%`")
_CHG_KEY = {"5분 만에": "chg5m", "15분 만에": "chg15m", "1시간 동안": "chg1h", "2시간 동안": "chg2h",
            "하루 동안": "chg24h"}
_PRICE = re.compile(r"현재가 \$([\d.,]+) \((바이낸스|비트겟)\)")
_QVOL = re.compile(r"거래대금[^$]*\$([\d.]+)([KMB]?)")
_VOLX = re.compile(r"평소의 \*([\d.]+)배\*")
_CONF = re.compile(r"확신도: ([^)]+)\)")
_EVID = re.compile(r"^\s*◦ \*([^*]+)\*")
_MARKET = re.compile(r"^💹 (🇰🇷|🇺🇸)")
_THEME = re.compile(r"^🧩 \*(.+?)\* 테마")
_LEADER = re.compile(r"^👑 대장주.*?\*(.+?)\*(?: \(([A-Z0-9.\-]+)\))? `([+-]?[\d.]+)%`")
_PEER = re.compile(r"^(?:🥈|🥉) \d등 \*(.+?)\*(?: \(([A-Z0-9.\-]+)\))? `([+-]?[\d.]+)%` — (.+)$")
_CALL = re.compile(r"^📣 \*관찰 콜\* — (.+)$")
_CATALYST = re.compile(r"^\s*◦ 재료: (.+?)(?: <https?://|$)")
_VERDICT = {"따라가는 중": "following", "아직 안 움직임": "lagging", "과열": "overheated"}
_MULT = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_pump(text: str) -> list[dict]:
    out: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = _COIN.match(line.strip())
        if m:
            cur = {"market": "binance", "base": m.group(2), "symbol": m.group(2) + "USDT",
                   "direction": "up" if m.group(1) == "🚀" else "down",
                   "headline": m.group(3).strip(), "tags": [], "src": "outbox"}
            out.append(cur)
            continue
        if cur is None:
            continue
        for label, v in _CHG.findall(line):
            cur[_CHG_KEY[label]] = float(v)
        if (m := _PRICE.search(line)):
            cur["price"] = _num(m.group(1))
            cur["market"] = "bitget" if m.group(2) == "비트겟" else "binance"
        if (m := _QVOL.search(line)) and "qvol" not in cur:
            cur["qvol"] = float(m.group(1)) * _MULT[m.group(2)]
        if (m := _VOLX.search(line)):
            cur["vol_x"] = float(m.group(1))
        if (m := _CONF.search(line)):
            cur["conf"] = m.group(1).strip()
        if (m := _EVID.match(line)):
            cur["tags"].append(m.group(1).strip())
    for c in out:
        c["tag"] = c["tags"][0] if c["tags"] else ""
    return [c for c in out if c.get("price")]


def parse_stock(text: str) -> list[dict]:
    lines = text.splitlines()
    market = ""
    for line in lines[:3]:
        if (m := _MARKET.match(line.strip())):
            market = "KR" if m.group(1) == "🇰🇷" else "US"
    out: list[dict] = []
    cur: dict | None = None
    for line in lines:
        s = line.strip()
        if (m := _THEME.match(s)):
            cur = {"market": market, "theme": m.group(1), "leader": {}, "catalyst": [], "peers": [],
                   "calls": [], "src": "outbox"}
            out.append(cur)
            continue
        if cur is None:
            continue
        if (m := _LEADER.match(s)):
            cur["leader"] = {"name": m.group(1), "symbol": m.group(2) or "", "chg": float(m.group(3))}
        elif (m := _PEER.match(s)):
            verdict = next((v for k, v in _VERDICT.items() if k in m.group(4)), "")
            cur["peers"].append({"name": m.group(1), "symbol": m.group(2) or "", "chg": float(m.group(3)),
                                 "verdict": verdict})
        elif (m := _CALL.match(s)):
            cur["calls"].append(m.group(1).strip())
        elif (m := _CATALYST.match(line)):
            cur["catalyst"].append(m.group(1).strip())
    return [c for c in out if c["leader"]]


def _delivered(note: str) -> bool:
    n = note or ""
    if "미전송" in n or n.strip() in ("none", ""):
        return False
    return "telegram=ok" in n or "webhook" in n or "playwright" in n or "cdp" in n


def run(outbox: Path | None = None) -> dict:
    box = outbox or OUTBOX
    marker = history.hist_dir() / "backfill.json"
    try:
        done = set(json.loads(marker.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        done = set()
    existing = history.load(0, time.time() + 86400)
    # 실시간 기록이 시작된 뒤의 알림은 이미 history 에 있으므로 그 이전 것만 채운다
    first = min((e["ts"] for e in existing if e.get("src") != "outbox"), default=None)
    have = {(e["t"], e["ts"], e.get("base") or e.get("theme")) for e in existing}
    stats = {"files": 0, "pump": 0, "stock": 0, "skipped_files": 0}
    for p in sorted(box.glob("[0-9]" * 8 + ".md")):
        if p.name in done:
            stats["skipped_files"] += 1
            continue
        day = datetime.strptime(p.stem, "%Y%m%d")
        chunks = p.read_text(encoding="utf-8").split("\n---\n")
        for ch in chunks:
            body = ch.strip().splitlines()
            if not body:
                continue
            m = _SECTION.match(body[0])
            if not m:
                continue
            hh, mm, ss, kind, note = m.groups()
            if kind not in ("pump", "stock") or not _delivered(note or ""):
                continue
            # 클라우드 러너 시계는 UTC
            ts = day.replace(hour=int(hh), minute=int(mm), second=int(ss), tzinfo=timezone.utc).timestamp()
            if first and ts >= first:
                continue
            text = "\n".join(body[1:])
            rows = parse_pump(text) if kind == "pump" else parse_stock(text)
            rows = [r for r in rows if (kind, int(ts), r.get("base") or r.get("theme")) not in have]
            stats[kind] += history.log(kind, rows, ts=ts)
        done.add(p.name)
        stats["files"] += 1
    # 오늘 파일은 아직 쌓이는 중이라 완료 표시하지 않는다
    today = datetime.now(timezone.utc).strftime("%Y%m%d") + ".md"
    done.discard(today)
    marker.write_text(json.dumps(sorted(done)), encoding="utf-8")
    return stats

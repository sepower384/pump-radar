"""상시 감시 루프. pythonw.exe 로 띄우면 콘솔 창 없이 백그라운드로 돈다.

    pythonw.exe watch.py

- 급등 스캔: config.json 의 scan.interval_sec 마다
- BTC 우상향 스캔: 4시간마다 (봉 마감 주기)
- 주식 스캔: 15분마다 (장중에만 의미 있음)
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")

from radar import runner, store  # noqa: E402
from radar.config import CFG, DATA_DIR  # noqa: E402

KST = timezone(timedelta(hours=9))
LOG = DATA_DIR / "watch.log"


def log(msg: str) -> None:
    line = f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass  # pythonw 에는 stdout 이 없다


def in_quiet_hours() -> bool:
    hours = CFG.get("scan.quiet_hours_kst", []) or []
    return datetime.now(KST).hour in hours


def main() -> None:
    interval = int(CFG.get("scan.interval_sec", 300))
    log(f"PUMP RADAR 시작 (주기 {interval}s, pid={os.getpid()})")

    last_trend = 0.0
    last_stock = 0.0
    while True:
        try:
            if in_quiet_hours():
                time.sleep(interval)
                continue

            now = time.time()
            res = runner.cycle("pump", verbose=False)
            p = res.get("pump", {})
            if p.get("sent"):
                log(f"급등 알림 {p.get('alerted')}건 전송 ({p.get('backend')})")

            if now - last_trend > 4 * 3600:
                r = runner.cycle("trend", verbose=False).get("trend", {})
                last_trend = now
                log(f"BTC우상향 {r.get('count', 0)}종목 (신규 {r.get('new', 0)}) sent={r.get('sent')}")

            if now - last_stock > 15 * 60:
                r = runner.cycle("stock", verbose=False).get("stock", {})
                last_stock = now
                if r.get("sent"):
                    log(f"주식 알림 {r.get('alerted')}건 전송")

        except KeyboardInterrupt:
            log("사용자 중지")
            return
        except Exception:  # noqa: BLE001
            log("사이클 예외:\n" + traceback.format_exc())

        time.sleep(interval)


if __name__ == "__main__":
    main()

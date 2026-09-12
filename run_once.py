"""한 번만 스캔하고 종료. 사용법:
    python run_once.py            # 전부
    python run_once.py pump       # 코인 급등만
    python run_once.py trend      # BTC 우상향만
    python run_once.py stock      # 주식 급등만
    python run_once.py test       # 슬랙 연결 테스트 메시지만 발사
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")

from radar import runner  # noqa: E402
from radar.notify import send  # noqa: E402


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()

    if arg == "test":
        backend = send("pump", "✅ *PUMP RADAR 연결 테스트* — 이 메시지가 보이면 알림 경로 정상입니다.",
                       [{"type": "section", "text": {"type": "mrkdwn",
                         "text": "✅ *PUMP RADAR 연결 테스트*\n이 메시지가 보이면 알림 경로 정상입니다."}}])
        print(f"전송 백엔드: {backend}")
        return 0 if backend != "none" else 1

    res = runner.cycle(arg if arg in ("pump", "trend", "stock") else "all")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

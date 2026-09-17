"""한 번만 스캔하고 종료. 사용법:
    python run_once.py                    # 전부
    python run_once.py pump               # 코인 급등만
    python run_once.py trend              # BTC 우상향만
    python run_once.py stock              # 주식 급등만
    python run_once.py test               # 슬랙+텔레그램 연결 테스트 메시지만 발사
    python run_once.py chats              # 텔레그램 봇들이 초대된 방(채널·그룹) id 조회
    python run_once.py preview-telegram   # 실제 데이터로 3종 메시지를 만들어 파일로만 저장(전송·알림기록 없음)
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")

from radar import runner  # noqa: E402
from radar.notify import deliver  # noqa: E402
from radar.notify.message import Msg  # noqa: E402


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()

    if arg == "test":
        msg = Msg("pump", "연결 테스트",
                  [["✅ *PUMP RADAR 연결 테스트*입니다. 이 메시지가 보이면 알림 경로가 정상입니다."]],
                  summary="✅ PUMP RADAR 연결 테스트")
        res = deliver(msg)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["delivered"] else 1

    if arg == "chats":
        from radar.notify import telegram
        print(json.dumps(telegram.list_chats(), ensure_ascii=False, indent=2))
        return 0

    if arg in ("preview-telegram", "preview"):
        from radar import preview
        only = sys.argv[2].lower() if len(sys.argv) > 2 else "all"
        res = preview.run(only)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    res = runner.cycle(arg if arg in ("pump", "trend", "stock") else "all")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

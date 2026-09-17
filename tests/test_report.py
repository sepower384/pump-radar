"""보고서·기록 테스트 (네트워크 없음). python tests/test_report.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HISTORY_DIR"] = tempfile.mkdtemp()

from radar import backfill, history, report  # noqa: E402
from radar.history import KST  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def kst(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def test_periods() -> None:
    print("\n[기간 계산]")
    w = report.last_period("week", kst("2026-09-21 10:17"))
    check("주간 = 지난주 월~일", w["key"] == "W-2026-38" and "09.14(월) ~ 09.20(일)" in w["label"], w["label"])
    check("주간 경계 = 월요일 0시", datetime.fromtimestamp(w["end"], KST) == kst("2026-09-21 00:00"))
    m = report.last_period("month", kst("2026-10-01 10:00"))
    check("월간 = 지난달", m["key"] == "M-2026-09" and m["label"] == "2026년 9월")
    m1 = report.last_period("month", kst("2027-01-01 10:00"))
    check("1월엔 작년 12월", m1["key"] == "M-2026-12")
    q = report.last_period("quarter", kst("2026-10-01 10:00"))
    check("분기 = 3분기(7~9월)", q["key"] == "Q-2026-3" and "7~9월" in q["label"], q["label"])
    q1 = report.last_period("quarter", kst("2027-01-02 10:00"))
    check("1월엔 작년 4분기", q1["key"] == "Q-2026-4")
    y = report.last_period("year", kst("2027-01-01 10:00"))
    check("연간 = 작년", y["key"] == "Y-2026")
    cp = report.current_period("month", kst("2026-09-17 19:00").timestamp())
    check("중간 집계 = 이번 달 1일부터 지금까지",
          datetime.fromtimestamp(cp["start"], KST) == kst("2026-09-01 00:00") and cp["key"].endswith("-partial"))


def test_history_and_due() -> None:
    print("\n[기록 · 발송 시점]")
    t0 = kst("2026-09-15 12:00").timestamp()
    history.log("pump", [{"market": "binance", "symbol": "AAAUSDT", "base": "AAA", "price": 1.0,
                          "chg24h": 40.0, "direction": "up", "tag": "거래소 상장"}], ts=t0)
    history.log("pump", [{"market": "bitget", "symbol": "BBBUSDT", "base": "BBB", "price": 2.0,
                          "chg24h": 25.0, "direction": "up", "tag": "원인 미확인"}], ts=t0 + 3600)
    history.log("stock", [{"market": "KR", "theme": "우주항공", "leader": {"name": "비츠로테크", "chg": 30.0},
                           "peers": [], "calls": []}], ts=t0 + 7200)
    ev = history.load(kst("2026-09-14 00:00").timestamp(), kst("2026-09-21 00:00").timestamp())
    check("월별 파일에 기록·조회", [e["t"] for e in ev] == ["pump", "pump", "stock"])
    check("구간 밖은 제외", not history.load(0, t0 - 1))
    (Path(os.environ["HISTORY_DIR"]) / "2026-09.jsonl").open("a", encoding="utf-8").write("{깨진 줄\n")
    check("깨진 줄은 건너뜀", len(history.load(0, t0 + 86400)) == 3)

    early = kst("2026-09-21 08:30").timestamp()
    check("월요일 9시 전엔 주간 보고서 대기", not any(p["kind"] == "week" for p in report.due(early)))
    ready = [p["key"] for p in report.due(kst("2026-09-21 10:17").timestamp())]
    check("월요일 10시 실행에서 주간 보고서", "W-2026-38" in ready, str(ready))
    check("기록 시작 전 기간은 안 보냄", "M-2026-08" not in ready and "Y-2025" not in ready)
    history.mark_report_sent("W-2026-38", {"file": "x.pdf"})
    check("보낸 보고서는 다시 안 보냄", "W-2026-38" not in [p["key"] for p in report.due(kst("2026-09-21 12:17").timestamp())])
    check("10월 1일엔 월간·분기 보고서",
          {"M-2026-09", "Q-2026-3"} <= {p["key"] for p in report.due(kst("2026-10-01 10:17").timestamp())})

    # 관찰 콜 스냅샷
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE calls (id INTEGER, ts INTEGER, market TEXT, symbol TEXT, status TEXT, result_pct REAL)")
    con.execute("INSERT INTO calls VALUES (1, ?, 'KR', '000001', 'hit', 3.1)", (int(t0),))
    history.dump_calls(con)
    con2 = sqlite3.connect(":memory:")
    con2.execute("CREATE TABLE calls (id INTEGER, ts INTEGER, market TEXT, symbol TEXT, status TEXT, result_pct REAL)")
    con2.execute("INSERT INTO calls VALUES (1, ?, 'US', 'NVDA', 'miss', 0.5)", (int(t0) + 10,))
    history.dump_calls(con2)
    calls = history.load_calls(0, t0 + 100)
    check("캐시가 비워져도 옛 관찰 콜 유지", {c["symbol"] for c in calls} == {"000001", "NVDA"}, str(calls))


def test_aggregate_render() -> None:
    print("\n[집계 · 렌더]")
    p = report.last_period("week", kst("2026-09-21 10:17"))
    a = report.aggregate(p, score=False)
    c = a["coin"]
    check("급등 건수", c["ups"] == 2 and c["coins"] == 2)
    check("이유 확인률 50%", c["known_rate"] == 50.0, str(c["known_rate"]))
    check("이유 묶음", {g["name"] for g in c["groups"]} == {"상장·공지", "원인 미확인"})
    check("주식 테마 집계", a["stock"]["markets"]["KR"]["themes"] == [("우주항공", 1)])
    check("비트겟 비중", c["bitget_share"] == 50.0)
    html_text = report.render_html(a)
    check("5쪽 구성", html_text.count('<section class="page') == 5)
    check("표지 제목", "주간 리포트" in html_text and "09.14(월) ~ 09.20(일)" in html_text)
    check("채점 전이면 '—' 표시", "—" in html_text)
    check("면책 문구", "매수·매도 추천이 아니라" in html_text)
    check("국기 이모지 대신 배지", "🇺🇸" not in html_text and "flag us" in html_text)
    cap = report.caption(a)
    check("캡션 1024자 이하 + 요약", len(cap) <= 1024 and "급등 포착 <b>2건</b>" in cap, cap)

    empty = report.aggregate(report.last_period("week", kst("2026-12-07 10:00")), score=False)
    eh = report.render_html(empty)
    check("기록 없는 기간도 렌더", "조건을 넘는 코인 급등이 없었습니다" in eh and "기록이 없습니다" in eh)

    # 채점 결과가 있을 때 인사이트
    ev = [{"t": "pump", "ts": 0, "base": f"C{i}", "market": "binance", "direction": "up", "tag": tag,
           "chg24h": 30, "out": {"max24": mx, "r24": r24, "min24": -20}}
          for i, (tag, mx, r24) in enumerate([("거래소 상장", 12, 4), ("거래소 상장", 8, 1), ("거래소 상장", 6, 2),
                                              ("원인 미확인", 1, -15), ("원인 미확인", 2, -12), ("원인 미확인", 0, -11)])]
    orig = history.load
    history.load = lambda s, e, kinds=None: [dict(x) for x in ev]
    try:
        a2 = report.aggregate(p, score=False)
    finally:
        history.load = orig
    c2 = a2["coin"]
    check("추가 상승 비율 50%", c2["success_rate"] == 50.0, str(c2["success_rate"]))
    check("되밀림 비율 50%", c2["fade_rate"] == 50.0)
    text = " ".join(report.insights(a2))
    check("인사이트: 좋은 유형·나쁜 유형", "'상장·공지'" in text and "'원인 미확인'" in text, text)
    check("인사이트: 이유 없는 급등 경고", "추격하지 않는 것이 좋아 보입니다" in text)
    watch = " ".join(report.watchlist(a2))
    check("지켜볼 것: 조심할 유형", "조심할 급등 유형" in watch and "원인 미확인" in watch, watch)


def test_backfill() -> None:
    print("\n[과거 기록 채우기]")
    sys.path.insert(0, str(ROOT / "tests"))
    import test_all as T
    msgs = T._fake_messages()
    pumps = backfill.parse_pump(msgs[0].slack_text())
    check("코인 3개 읽음", [p["base"] for p in pumps] == ["AAA", "BBB", "ZZZ"])
    check("변동률·가격·거래소", pumps[0]["chg24h"] == 98.5 and pumps[0]["price"] == 1.0
          and pumps[1]["market"] == "bitget" and pumps[1]["direction"] == "down")
    check("첫 근거 태그", pumps[0]["tag"] == "테마 순환매" and pumps[2]["tag"] == "원인 미확인")
    check("거래대금 단위", pumps[0]["qvol"] == 30e6 and pumps[2]["qvol"] == 900e3)
    st = backfill.parse_stock(msgs[2].slack_text())
    check("주식 테마 읽음", len(st) == 1 and st[0]["market"] == "KR" and st[0]["leader"]["name"] == "대장전자")
    check("2·3등 판정·콜", [p["verdict"] for p in st[0]["peers"]] == ["overheated", "lagging"]
          and st[0]["calls"] == ["둘째소재"])
    check("결과만 있는 글은 테마 없음", backfill.parse_stock(msgs[3].slack_text()) == [])

    box = Path(tempfile.mkdtemp())
    (box / "20260914.md").write_text(
        "\n\n---\n### [01:17:30] pump (slack=webhook · telegram=ok)\n" + msgs[0].slack_text()
        + "\n\n---\n### [01:17:31] pump telegram (1개 · photo=ok)\n무시\n"
        + "\n\n---\n### [03:17:30] pump (미전송)\n" + msgs[0].slack_text()
        + "\n\n---\n### [05:17:30] stock (webhook)\n" + msgs[2].slack_text() + "\n", encoding="utf-8")
    hd = Path(tempfile.mkdtemp())
    old = os.environ["HISTORY_DIR"]
    os.environ["HISTORY_DIR"] = str(hd)
    try:
        r1 = backfill.run(box)
        r2 = backfill.run(box)
        ev = history.load(0, 2e9)
    finally:
        os.environ["HISTORY_DIR"] = old
    check("전송된 것만 채움(미전송·텔레그램 사본 제외)", r1["pump"] == 3 and r1["stock"] == 1, str(r1))
    check("두 번 돌려도 중복 없음", r2["pump"] == 0 and len(ev) == 4, f"{r2} {len(ev)}")
    check("UTC → 시각 보존", ev[0]["ts"] == int(datetime(2026, 9, 14, 1, 17, 30, tzinfo=__import__('datetime').timezone.utc).timestamp()))
    check("채운 기록 표시", all(e.get("src") == "outbox" for e in ev))


def main() -> int:
    test_periods()
    test_history_and_due()
    test_aggregate_render()
    test_backfill()
    print(f"\n결과: {PASS} PASS / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

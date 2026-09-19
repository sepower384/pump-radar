"""첫 포착 이후 추적 · 만족도 조사 테스트 (네트워크 없음). python tests/test_followup.py"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HISTORY_DIR"] = tempfile.mkdtemp()

from radar import followup, history, survey  # noqa: E402
from radar.history import KST  # noqa: E402

PASS = FAIL = 0
HOUR = 3600


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


def ts(s: str) -> float:
    return kst(s).timestamp()


def bars(t0: float, pattern: list[tuple[float, float, float]], step: int = HOUR) -> list[list]:
    """[시각, 고가, 저가, 종가] 봉을 손으로 만든다."""
    return [[t0 + (i + 1) * step, hi, lo, cl] for i, (hi, lo, cl) in enumerate(pattern)]


# ─────────────────────────── 첫 포착 목록 ───────────────────────────
def test_candidates() -> None:
    print("\n[첫 포착 목록]")
    t0 = ts("2026-09-01 10:00")
    history.log("pump", [{"market": "binance", "symbol": "AAAUSDT", "base": "AAA", "price": 100.0,
                          "direction": "up", "tag": "뉴스", "chg24h": 30.0}], ts=t0)
    history.log("pump", [{"market": "binance", "symbol": "AAAUSDT", "base": "AAA", "price": 130.0,
                          "direction": "up", "tag": "거래량 폭증"}], ts=t0 + 30 * HOUR)
    history.log("pump", [{"market": "bitget", "symbol": "BBBUSDT", "base": "BBB", "price": 2.0,
                          "direction": "down", "tag": "급락"}], ts=t0 + HOUR)
    history.log("trend", [{"symbol": "CCCUSDT", "base": "CCC", "price": 5.0, "score": 88.0}], ts=t0 + 2 * HOUR)
    history.log("stock", [{"market": "KR", "theme": "광통신",
                           "leader": {"symbol": "043260", "name": "성호전자", "chg": 15.0, "price": 19400.0},
                           "peers": [], "calls": []}], ts=t0 + 3 * HOUR)
    history.log("stock", [{"market": "US", "theme": "AI 반도체",
                           "leader": {"symbol": "MSTR", "name": "Strategy", "chg": 11.0},
                           "peers": [], "calls": []}], ts=t0 + 4 * HOUR)

    recs = followup.candidates(now=t0 + 40 * HOUR)
    by = {r["name"]: r for r in recs}
    check("급등·우상향·주식 대장주가 추적 대상", set(by) == {"AAA", "CCC", "성호전자"}, str(sorted(by)))
    check("같은 코인 재포착은 첫 포착에 합쳐 회차만 올림",
          by["AAA"]["repeats"] == 2 and by["AAA"]["price"] == 100.0 and by["AAA"]["ts"] == t0)
    check("급락 알림은 추적하지 않음", "BBB" not in by)
    check("가격 없는 옛 대장주 기록은 건너뜀", "Strategy" not in by)
    check("키는 자산+첫포착시각", by["CCC"]["key"] == f"coin|binance|CCCUSDT|{int(t0 + 2 * HOUR)}")

    # 30일(dedupe_days) 넘겨 다시 잡히면 새 포착으로 친다
    history.log("pump", [{"market": "binance", "symbol": "AAAUSDT", "base": "AAA", "price": 300.0,
                          "direction": "up", "tag": "뉴스"}], ts=t0 + 40 * 24 * HOUR)
    later = [r for r in followup.candidates(now=t0 + 41 * 24 * HOUR) if r["name"] == "AAA"]
    check("한 달 넘게 잠잠하다 다시 잡히면 새 첫 포착", len(later) == 2, str(len(later)))


# ─────────────────────────── 채점 ───────────────────────────
def test_measure() -> None:
    print("\n[구간 채점]")
    t0 = ts("2026-09-01 10:00")
    rec = {"asset": "coin", "kind": "pump", "market": "binance", "symbol": "AAAUSDT", "name": "AAA",
           "ts": t0, "price": 100.0, "note": "뉴스", "repeats": 1, "key": "k"}
    # 1일 뒤 +10%, 그 뒤 하락해 3일 뒤 -5%, 구간 최고 +50%
    pat = [(110, 95, 105)] * 23 + [(150, 100, 110)] + [(120, 80, 95)] * 48
    followup.bars_for = lambda r, hours: bars(t0, pat)[:hours]

    m = followup.measure(rec, now=t0 + 25 * HOUR)
    check("1일 구간만 확정", set(m["res"]) == {"h24"}, str(sorted(m["res"])))
    check("1일 수익률 = 포착가 대비 종가", m["res"]["h24"]["r"] == 10.0, str(m["res"]["h24"]))
    check("구간 최고·최저도 기록", m["res"]["h24"]["hi"] == 50.0 and m["res"]["h24"]["lo"] == -5.0)
    check("아직 안 끝난 구간은 비워둠", "h72" not in m["res"] and not m["done"])

    m2 = followup.measure(rec, now=t0 + 80 * HOUR, prev=m)
    check("확정된 구간은 그대로 두고 새 구간만 추가", m2["res"]["h24"]["r"] == 10.0 and "h72" in m2["res"])
    check("3일 뒤 되밀린 결과", m2["res"]["h72"]["r"] == -5.0, str(m2["res"]["h72"]))

    # 봉이 아직 안 온 경우(주말·휴장)는 다음 실행으로 미룬다
    followup.bars_for = lambda r, hours: bars(t0, [(110, 95, 105)] * 2)
    m3 = followup.measure(rec, now=t0 + 30 * HOUR)
    check("구간 시점 봉이 없으면 채점 보류", "h24" not in (m3["res"] if m3 else {}))

    followup.bars_for = lambda r, hours: []
    check("시세를 못 받으면 None", followup.measure(rec, now=t0 + 30 * HOUR) is None)


def test_needs_and_stats() -> None:
    print("\n[갱신 대상 · 집계]")
    t0 = ts("2026-09-01 10:00")
    rec = {"ts": t0, "key": "k"}
    check("구간이 지났는데 값이 없으면 갱신", followup._needs(rec, None, t0 + 25 * HOUR, live=False))
    done = {"res": {"h24": {"r": 1}, "h72": {"r": 1}}, "checked": t0 + 80 * HOUR}
    check("확정된 구간만 있으면 평소엔 건너뜀", not followup._needs(rec, done, t0 + 80 * HOUR, live=False))
    check("성적표 보내는 날엔 진행 중인 것도 새로 받음", followup._needs(rec, done, t0 + 200 * HOUR, live=True))
    check("마지막 구간+1주 지나면 포기",
          not followup._needs(rec, {"res": {}}, t0 + (720 + 24 * 8) * HOUR, live=True))

    rows = [{"res": {"h24": {"r": 10.0, "hi": 20.0, "lo": -5.0}}},
            {"res": {"h24": {"r": -4.0, "hi": 2.0, "lo": -9.0}}},
            {"res": {"h24": {"r": 6.0, "hi": 8.0, "lo": -1.0}}}]
    s24 = followup.stats(rows)[0]
    check("구간 평균·승률", s24["n"] == 3 and s24["avg"] == 4.0 and s24["win"] == 67, str(s24))
    check("구간 최고·최저 평균", s24["avg_hi"] == 10.0 and s24["avg_lo"] == -5.0, str(s24))
    check("채점 없는 구간은 n=0", followup.stats(rows)[1]["n"] == 0)


def test_digest() -> None:
    print("\n[성적표 메시지]")
    t0 = ts("2026-09-10 09:00")
    now = ts("2026-09-12 09:30")
    cache = {
        "coin|binance|AAAUSDT|1": {"asset": "coin", "kind": "pump", "market": "binance", "symbol": "AAAUSDT",
                                   "name": "AAA", "ts": t0, "price": 100.0, "note": "뉴스", "repeats": 2,
                                   "res": {"h24": {"r": 12.0, "hi": 30.0, "lo": -2.0}},
                                   "cur": 25.0, "hi": 30.0, "lo": -2.0},
        "coin|bitget|BBBUSDT|2": {"asset": "coin", "kind": "pump", "market": "bitget", "symbol": "BBBUSDT",
                                  "name": "BBB", "ts": t0 - 5 * HOUR, "price": 2.0, "note": "상장/공지",
                                  "repeats": 1, "res": {}, "cur": -8.0, "hi": 4.0, "lo": -12.0},
        "coin|binance|CCCUSDT|3": {"asset": "coin", "kind": "trend", "market": "binance", "symbol": "CCCUSDT",
                                   "name": "CCC", "ts": t0, "price": 5.0, "note": "", "repeats": 1,
                                   "res": {"h24": {"r": 3.0, "hi": 5.0, "lo": 0.0}}, "cur": 3.0},
    }
    msg = followup.digest_msg("pump", now, since=now - 30 * HOUR, cache=cache)
    text = msg.slack_text()
    check("급등 방 성적표엔 급등 포착만", "AAA" in text and "BBB" in text and "CCC" not in text)
    check("새로 확정된 구간을 앞에 알림", "1일 +12.0%" in text and "성적이 확정된" in text, text[:400])
    check("포착가·최고·최저를 같이 보여줌", "최고 +30.0%" in text and "포착가" in text)
    check("재포착 회차 표시", "재포착 2회" in text)
    check("진행 중인 것의 현재 수익률", "+25.0%" in text and "포착 2일째" in text)
    check("구간별 평균 표", "구간별 평균 성적" in text and "오른 비율" in text)
    check("텔레그램 변환도 깨지지 않음", all(len(c) <= 4096 for c in msg.telegram_chunks()))

    trend = followup.digest_msg("trend", now, since=now - 30 * HOUR, cache=cache)
    check("우상향 방은 우상향 포착만", "CCC" in trend.slack_text() and "AAA" not in trend.slack_text())
    check("보낼 게 없으면 메시지를 만들지 않음",
          followup.digest_msg("stock", now, since=now - 30 * HOUR, cache=cache) is None)

    fresh = followup.matured_since(list(cache.values()), since=now - 30 * HOUR, now=now)
    check("직전 성적표 이후 확정분만 '새 성적'", [r[0]["name"] for r in fresh] == ["AAA", "CCC"], str(fresh))
    check("한 자산이 두 구간을 넘겼으면 한 줄로 묶음", all(isinstance(hs, list) for _, hs in fresh))
    check("이미 알린 구간은 다시 안 넣음", not followup.matured_since(list(cache.values()), t0 + 25 * HOUR, now))


def test_digest_schedule() -> None:
    print("\n[성적표 발송 시점 — 주 1회 월요일]")
    st: dict = {}
    check("월요일 9시 전엔 대기", not followup.digest_due("pump", ts("2026-09-21 08:30"), st))
    check("월요일 9시 지나면 발송", followup.digest_due("pump", ts("2026-09-21 09:10"), st))
    check("월요일이 아니면 안 보냄", not followup.digest_due("pump", ts("2026-09-23 09:10"), st))
    week = followup.digest_period(ts("2026-09-21 09:10"))
    check("주 단위 키", week == "2026-W39", week)
    st = {"digest": {"pump": week}}
    check("그 주엔 한 번만", not followup.digest_due("pump", ts("2026-09-21 21:00"), st))
    check("다음 주 월요일에 다시", followup.digest_due("pump", ts("2026-09-28 09:10"), st))


# ─────────────────────────── 만족도 조사 ───────────────────────────
def test_survey_schedule() -> None:
    print("\n[만족도 조사 시점]")
    check("매달 1일 10시 이후", survey.due(ts("2026-10-01 10:05"), {}))
    check("1일 10시 전엔 대기", not survey.due(ts("2026-10-01 09:00"), {}))
    check("2일엔 안 보냄", not survey.due(ts("2026-10-02 11:00"), {}))
    check("조사 대상은 지난달", survey.month_key(ts("2026-10-01 10:05")) == "2026-09")
    check("1월 1일엔 작년 12월", survey.month_key(ts("2027-01-01 10:05")) == "2026-12")
    check("이미 보낸 달은 다시 안 보냄",
          not survey.due(ts("2026-10-01 15:00"), {"sent": {"2026-09": {"at": 1}}}))


def test_survey_results() -> None:
    print("\n[의견 받기 — 투표 없음]")
    survey.save_state({})
    updates = [
        {"update_id": 13, "message": {"chat": {"type": "private"}, "from": {"username": "kang"},
                                      "text": "알림이 밤에 너무 많아요"}},
        {"update_id": 14, "message": {"chat": {"type": "supergroup"}, "from": {"first_name": "누구"},
                                      "text": "방에 올린 글은 수집 대상 아님"}},
        {"update_id": 15, "message": {"chat": {"type": "private"}, "from": {"first_name": "손님"},
                                      "text": "/start"}},
    ]
    # 방마다 봇이 다르다 — 급등탐정 봇에만 의견이 왔다고 본다
    survey._updates = lambda kind, offset: (updates if kind == "pump" and not offset else [])
    forwarded: list = []
    survey._forward = lambda kind, text, who, now: (forwarded.append((kind, who, text)), True)[1]
    got = survey.collect(now=ts("2026-10-03 12:00"))
    check("1:1 메시지만 의견으로 수집", got["pump"]["feedback"] == 1, str(got))
    check("운영자에게 그대로 전달", forwarded == [("pump", "@kang", "알림이 밤에 너무 많아요")], str(forwarded))
    check("다른 방 봇에 온 게 없으면 0건", got["trend"]["feedback"] == 0 and got["stock"]["feedback"] == 0)
    check("명령어(/start)는 의견이 아님", got["pump"]["feedback"] == 1)

    st2 = survey.state()
    check("offset 을 저장해 같은 의견을 두 번 안 읽음", st2["offset"]["pump"] == 16, str(st2.get("offset")))
    check("의견 원문은 공개 기록이 아니라 private 폴더에",
          (Path(os.environ["HISTORY_DIR"]) / "private" / "feedback.jsonl").exists()
          and "알림이 밤에" not in (Path(os.environ["HISTORY_DIR"]) / "survey.json").read_text(encoding="utf-8"))
    check("private 폴더는 git 에서 제외",
          "private/" in (Path(os.environ["HISTORY_DIR"]) / ".gitignore").read_text(encoding="utf-8"))
    check("건수·월별 집계만 기록", st2.get("feedback_n") == 1 and st2["feedback_by_month"]["2026-10"] == 1)
    check("운영자 조회에는 원문이 보임",
          any("알림이 밤에" in (f.get("text") or "") for f in survey.results()["최근 의견"]))

    msg = survey.ask_msg("pump", ts("2026-11-01 10:00"), st2)
    text = msg.slack_text()
    check("방에는 안내 한 통만 — 투표 없음", "투표" in text and "투표도 양식도 없습니다" in text, text[:300])
    check("점수·집계는 방에 안 나감", "점" not in text.replace("한 줄", "") or "4.3" not in text)
    check("1:1로 보내라는 안내", "1:1" in text and "공개하지 않습니다" in text)
    check("대상 달을 물어봄", "10월 한 달" in text, text[:120])
    check("텔레그램 분할 4096 이하", all(len(c) <= 4096 for c in msg.telegram_chunks()))
    check("투표 기능은 코드에서 사라짐",
          not hasattr(survey, "POLLS") and not hasattr(survey, "sweep") and not hasattr(survey, "_send_poll"))


def test_report_hook() -> None:
    print("\n[보고서 연결]")
    from radar import report
    cache = {"k1": {"asset": "coin", "kind": "pump", "name": "AAA", "ts": ts("2026-09-10 09:00"),
                    "price": 100.0, "repeats": 2, "cur": 25.0, "hi": 30.0, "lo": -2.0,
                    "res": {"h24": {"r": 12.0, "hi": 30.0, "lo": -2.0},
                            "h168": {"r": -3.0, "hi": 30.0, "lo": -18.0}}},
             "k2": {"asset": "stock", "kind": "stock", "name": "성호전자", "ts": ts("2026-09-11 09:00"),
                    "price": 19400.0, "repeats": 1, "cur": 5.0, "hi": 9.0, "lo": -3.0,
                    "res": {"h24": {"r": 4.0, "hi": 9.0, "lo": -3.0}}}}
    s = followup.summary(start=ts("2026-09-01 00:00"), end=ts("2026-10-01 00:00"), cache=cache, asset="coin")
    check("기간·자산으로 걸러 집계", s["n"] == 1 and s["stats"][0]["n"] == 1, str(s["n"]))
    a = {"period": {"kind": "month", "label": "2026년 9월", "start": ts("2026-09-01 00:00"),
                    "end": ts("2026-10-01 00:00")}, "generated": ts("2026-10-01 09:00"), "since": None,
         "coin": {"alerts": 0, "ups": 0, "downs": 0, "coins": 0, "known_rate": None, "scored": 0,
                  "success": 0, "fade": 0, "success_rate": None, "fade_rate": None, "avg_max24": None,
                  "groups": [], "hours": {}, "weekdays": {}, "top": [], "repeat": [], "follow": [],
                  "faded": [], "trend": [], "bitget_share": None},
         "stock": {"markets": {"US": {"n": 0, "themes": [], "leaders": []},
                               "KR": {"n": 0, "themes": [], "leaders": []}},
                   "calls": [], "calls_done": 0, "calls_hit": 0, "hit_rate": None, "avg_result": None},
         "followup": {"coin": s,
                      "stock": followup.summary(start=ts("2026-09-01 00:00"), end=ts("2026-10-01 00:00"),
                                                cache=cache, asset="stock"),
                      "horizons": [{"h": h, "label": followup.hlabel(h)} for h in followup.horizons()]},
         "survey": {"months": [{"month": "2026-09", "lines": ["• 만족도 *4.3점* / 5점 (응답 10명)"],
                                "score": 4.3}], "feedback_n": 1}}   # 보고서엔 실리지 않아야 한다
    html_text = report.render_html(a)
    check("보고서에 추적 페이지", "첫 포착 이후, 그래서 얼마나 갔나" in html_text)
    check("구간 표가 들어감", "1일 뒤" in html_text and "7일 뒤" in html_text)
    check("주식 추적 블록", "관찰 콜) 첫 포착" in html_text)
    check("만족도 결과는 보고서에 안 실림", "만족도 조사" not in html_text and "4.3점" not in html_text)
    check("페이지 번호 6쪽까지", ">6</span>" in html_text)
    cap = report.caption(a)
    check("캡션에 추적 한 줄", "첫 포착 이후" in cap, cap[:200])


def main() -> int:
    test_candidates()
    test_measure()
    test_needs_and_stats()
    test_digest()
    test_digest_schedule()
    test_survey_schedule()
    test_survey_results()
    test_report_hook()
    print(f"\n{PASS} PASS · {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

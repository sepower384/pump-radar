"""자체 검증. 네트워크 없이 도는 로직 테스트 + (옵션) 실계정 없는 라이브 스모크.

    python tests/test_all.py          # 로직만 (빠름, 오프라인)
    python tests/test_all.py --live   # 실제 API까지 호출
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PYTHONUTF8", "1")

from radar import store  # noqa: E402
from radar.engine import btc_trend, indicators as ind, reason  # noqa: E402
from radar.sources import binance, news, stocks  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


# ─────────────────────────── 지표 ───────────────────────────
def test_indicators() -> None:
    print("\n[지표]")
    check("ema 길이 유지", len(ind.ema([1, 2, 3, 4, 5], 3)) == 5)
    check("ema 상승 추종", ind.ema([1, 2, 3, 4, 5], 3)[-1] > ind.ema([1, 2, 3, 4, 5], 3)[0])
    check("직선의 기울기", abs(ind.linreg_slope([1, 2, 3, 4, 5]) - 1.0) < 1e-9)
    check("하락은 음의 기울기", ind.linreg_slope([5, 4, 3, 2, 1]) < 0)
    check("완전한 직선 R2=1", abs(ind.r_squared([1, 2, 3, 4, 5]) - 1.0) < 1e-9)
    check("들쭉날쭉하면 R2 낮음", ind.r_squared([1, 5, 2, 6, 3]) < 0.6)
    check("MDD 계산", abs(ind.max_drawdown([100, 120, 60, 90]) - (-0.5)) < 1e-9)
    check("scale 하한 클램프", ind.scale(-5, 0, 10) == 0.0)
    check("scale 상한 클램프", ind.scale(50, 0, 10) == 1.0)
    check("zscore 0분산 안전", ind.zscore(5, [3, 3, 3]) == 0.0)
    check("데이터 부족시 0", ind.linreg_slope([1]) == 0.0)


# ─────────────────────────── BTC 우상향 점수 ───────────────────────────
def test_btc_trend() -> None:
    print("\n[BTC 우상향 점수]")
    n = 200
    up = [100 * (1.01 ** i) for i in range(n)]          # 깔끔한 우상향
    down = [100 * (0.99 ** i) for i in range(n)]        # 하락
    flat = [100 + math.sin(i / 3) for i in range(n)]    # 횡보

    s_up = btc_trend.score_series(up, "4h")
    s_down = btc_trend.score_series(down, "4h")
    s_flat = btc_trend.score_series(flat, "4h")

    check("우상향 점수 산출됨", s_up is not None)
    check("우상향 > 횡보", s_up["score"] > s_flat["score"], f"{s_up['score']} vs {s_flat['score']}")
    check("횡보 > 하락", s_flat["score"] >= s_down["score"])
    check("하락은 저점수", s_down["score"] < 25, str(s_down["score"]))
    check("우상향은 고점수", s_up["score"] > 70, str(s_up["score"]))
    check("점수 범위 0~100", all(0 <= s["score"] <= 100 for s in (s_up, s_down, s_flat)))
    check("정배열 감지", s_up["ema_stacked"] is True)
    check("고점근접 ~1.0", s_up["near_high"] > 0.99)
    check("데이터 부족시 None", btc_trend.score_series([1, 2, 3], "4h") is None)

    ratio = btc_trend.ratio_series([10, 20, 30], [1, 2, 3])
    check("비율 시계열", ratio == [10.0, 10.0, 10.0])
    check("길이 다르면 뒤에서 맞춤", btc_trend.ratio_series([1, 2, 3, 4], [1, 1]) == [3.0, 4.0])
    check("빈 입력 안전", btc_trend.ratio_series([], [1]) == [])


# ─────────────────────────── 저장소 ───────────────────────────
def test_store() -> None:
    print("\n[상태 저장소]")
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    orig = store.DB_PATH
    store.DB_PATH = tmp
    try:
        con = store.connect()
        now = int(time.time())
        store.save_snapshot(con, "binance", [("BTCUSDT", 100.0, 1.0)], ts=now - 15 * 60)
        store.save_snapshot(con, "binance", [("BTCUSDT", 110.0, 1.0)], ts=now)

        got = store.price_at(con, "binance", 15, tolerance_min=10)
        check("15분 전 스냅샷 조회", got.get("BTCUSDT", (0,))[0] == 100.0, str(got))
        check("허용오차 밖은 빈값", store.price_at(con, "binance", 300, tolerance_min=5) == {})

        check("첫 알림은 통과", not store.recently_alerted(con, "pump", "X", 60))
        store.mark_alerted(con, "pump", "X")
        check("쿨다운 내 재알림 차단", store.recently_alerted(con, "pump", "X", 60))
        check("쿨다운 0이면 통과", not store.recently_alerted(con, "pump", "X", 0))
        check("종류가 다르면 별개", not store.recently_alerted(con, "trend", "X", 60))

        store.prune(con, keep_hours=0)
        check("prune 후 스냅샷 정리", store.price_at(con, "binance", 15) == {})
        con.close()
    finally:
        store.DB_PATH = orig


# ─────────────────────────── 필터/파서 ───────────────────────────
def test_filters() -> None:
    print("\n[유니버스 필터]")
    fake = [
        {"symbol": "BTCUSDT", "quoteVolume": "9e9", "lastPrice": "60000", "priceChangePercent": "1"},
        {"symbol": "USDCUSDT", "quoteVolume": "9e9", "lastPrice": "1", "priceChangePercent": "0"},
        {"symbol": "BTCUPUSDT", "quoteVolume": "9e9", "lastPrice": "5", "priceChangePercent": "9"},
        {"symbol": "ETHBTC", "quoteVolume": "9e9", "lastPrice": "0.03", "priceChangePercent": "1"},
        {"symbol": "TINYUSDT", "quoteVolume": "100", "lastPrice": "0.1", "priceChangePercent": "5"},
        {"symbol": "PEPEUSDT", "quoteVolume": "5e6", "lastPrice": "0.00001",
         "priceChangePercent": "12"},
    ]
    uni = binance.usdt_universe(fake, 1_000_000)
    syms = {u["symbol"] for u in uni}
    check("스테이블 제외", "USDCUSDT" not in syms)
    check("레버리지토큰 제외", "BTCUPUSDT" not in syms)
    check("비USDT 마켓 제외", "ETHBTC" not in syms)
    check("저유동성 제외", "TINYUSDT" not in syms)
    check("정상 종목 통과", {"BTCUSDT", "PEPEUSDT"} <= syms)
    check("파생 필드 부착", uni[0]["_base"] and uni[0]["_qvol"] > 0)

    print("\n[뉴스 매칭]")
    hl = [
        {"title": "Solana ETF approved by SEC", "source": "X", "url": "", "ts": 0},
        {"title": "Bitcoin hits new high", "source": "X", "url": "", "ts": 0},
        {"title": "$SC token surges", "source": "X", "url": "", "ts": 0},
    ]
    check("이름으로 매칭", len(news.match_symbol(hl, "SOL", "Solana")) == 1)
    check("짧은 티커는 $필요", len(news.match_symbol(hl, "SC", "Siacoin")) == 1)
    check("무관 코인은 0건", len(news.match_symbol(hl, "DOGE", "Dogecoin")) == 0)

    print("\n[네이버 숫자 파싱]")
    check("콤마 제거", stocks._f("1,234") == 1234.0)
    check("퍼센트 제거", stocks._f("+21.24%") == 21.24)
    check("태그 제거", stocks._f('<span class="tah">7,960</span>') == 7960.0)
    check("빈값 0", stocks._f("") == 0.0)


# ─────────────────────────── 이유 엔진 ───────────────────────────
def test_reason() -> None:
    print("\n[이유 추론]")
    ctx = reason.MarketContext()
    ctx.offline = True   # 종목별 네트워크 조회 차단 (테스트 재현성)
    ctx.listings = {"ABC": {"kind": "업비트 신규상장", "title": "에이비씨(ABC) 신규 거래지원", "url": "u"}}
    ctx.themes = {"MEME1": ["밈코인"], "MEME2": ["밈코인"]}
    ctx.meta = {"ABC": {"name": "ABC Coin"}}

    c = {"symbol": "ABCUSDT", "base": "ABC", "price": 1.0, "market": "bitget",
         "direction": "up", "chg15m": 20, "chg24h": 30, "vol_x": 5}
    r = reason.explain_crypto(c, ctx)
    check("상장 재료 최우선", r["evidence"][0]["tag"] == "상장/공지")
    check("신뢰도 높음", r["confidence"] == "높음")
    check("한 줄 요약 생성", "상장" in r["headline"])

    ctx.note_pump("MEME1")
    ctx.note_pump("MEME2")
    check("테마 카운트 누적", ctx.pumping_themes.get("밈코인") == 2)

    c2 = {"symbol": "MEME1USDT", "base": "MEME1", "price": 1.0, "market": "bitget",
          "direction": "up", "chg15m": 9, "chg24h": 9, "vol_x": 0}
    r2 = reason.explain_crypto(c2, ctx)
    check("테마 순환매 감지", any(e["tag"] == "테마 순환매" for e in r2["evidence"]))

    c3 = {"symbol": "ZZZUSDT", "base": "ZZZ", "price": 1.0, "market": "bitget",
          "direction": "up", "chg15m": 9, "chg24h": 9, "vol_x": 0}
    r3 = reason.explain_crypto(c3, ctx)
    check("근거 없으면 원인미확인", r3["evidence"][0]["tag"] == "원인 미확인")
    check("근거 없으면 신뢰도 낮음", r3["confidence"] == "낮음")

    ctx.upbit_krw = {"USDT": {"price": 1400.0}, "KIM": {"price": 1540.0}}
    ctx.usdkrw = 1400.0
    kp = ctx.kimchi_premium("KIM", 1.0)
    check("김프 계산", kp is not None and abs(kp - 10.0) < 0.01, str(kp))
    check("없는 코인은 None", ctx.kimchi_premium("NONE", 1.0) is None)

    s = {"market": "KR", "name": "테스트", "code": "000000", "chg": 30, "trade_value_eok": 900}
    rs = reason.explain_stock(dict(s, name=""), offline=True)
    check("주식 근거 항상 존재", len(rs["evidence"]) >= 1)


# ─────────────────────────── 라이브 스모크 ───────────────────────────
def test_live() -> None:
    print("\n[라이브 — 실제 API]")
    t = binance.ticker_24hr()
    check("바이낸스 24h 티커", len(t) > 100)
    uni = binance.usdt_universe(t, 3_000_000)
    check("유니버스 구성", len(uni) > 50, str(len(uni)))
    kl = binance.klines("BTCUSDT", "4h", 60)
    check("캔들 수신", len(kl) == 60)
    check("종가 파싱", binance.closes(kl)[-1] > 0)

    from radar.sources import bitget, coingecko, upbit
    check("비트겟 티커", len(bitget.normalized(500_000)) > 20)
    check("업비트 원화마켓", len(upbit.krw_markets()) > 100)
    check("코인게코 트렌딩", len(coingecko.trending_symbols()) >= 0)
    check("뉴스 RSS", len(news.crypto_headlines(48)) > 5)
    check("미국 급등주", len(stocks.us_movers(["day_gainers"], 20)) > 3)
    check("한국 급등주", len(stocks.kr_movers(1)) > 10)


# ─────────────────────────── 텔레그램 ───────────────────────────
import re  # noqa: E402

_TG_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN_PUMP", "TELEGRAM_BOT_TOKEN_TREND",
            "TELEGRAM_BOT_TOKEN_STOCK", "TELEGRAM_CHAT_ID", "TELEGRAM_TOPIC_PUMP",
            "TELEGRAM_TOPIC_TREND", "TELEGRAM_TOPIC_STOCK")


class _EnvGuard:
    """텔레그램 환경변수를 비운 상태로 테스트하고 원래대로 돌려놓는다(.env 값 보호)."""

    def __enter__(self):
        self.saved = {k: os.environ.pop(k) for k in _TG_KEYS if k in os.environ}
        return self

    def __exit__(self, *exc):
        for k in _TG_KEYS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)


def test_telegram_convert() -> None:
    from radar.notify import telegram as tg
    print("\n[텔레그램 변환]")
    check("굵게", tg.to_html("*BTC* 급등") == "<b>BTC</b> 급등", tg.to_html("*BTC* 급등"))
    check("기울임", tg.to_html("_관찰 목록입니다._") == "<i>관찰 목록입니다.</i>")
    check("코드", tg.to_html("`+12.3%`") == "<code>+12.3%</code>")
    link = tg.to_html("<https://a.com/x_y?a=1&b=2|차트 보기>")
    check("링크 변환", link == '<a href="https://a.com/x_y?a=1&amp;b=2">차트 보기</a>', link)
    check("맨 링크", tg.to_html("<https://a.com>") == '<a href="https://a.com">https://a.com</a>')
    esc = tg.to_html("S&P 500 <급등> 3>2")
    check("& < > 이스케이프", esc == "S&amp;P 500 &lt;급등&gt; 3&gt;2", esc)
    check("코드 안도 이스케이프", tg.to_html("`a<b`") == "<code>a&lt;b</code>")
    check("URL 밑줄은 기울임 아님", "<i>" not in tg.to_html("<https://binance.com/en/trade/A_USDT|차트>"))
    check("단어 속 별표 무시", tg.to_html("2*3*4") == "2*3*4")
    for code in ("rocket", "point_right", "dollar", "thinking_face", "newspaper", "mag_right", "link", "new"):
        out = tg.to_html(f":{code}: 테스트")
        check(f"이모지 :{code}: 매핑", out.split(" ")[0] == tg.EMOJI[code], out)
    check("모르는 코드는 그대로", tg.emojify(":no_such_code:") == ":no_such_code:")

    # 코드베이스 문자열 리터럴에 실제로 쓰인 슬랙 이모지 코드가 전부 매핑돼 있는지
    import ast
    root = Path(__file__).resolve().parent.parent
    used: set[str] = set()
    for py in list((root / "radar").rglob("*.py")) + [root / "run_once.py", root / "watch.py"]:
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                used |= set(re.findall(r"(?<![\w/]):([a-z][a-z0-9_+\-]*):(?![\w/])", node.value))
    missing = sorted(c for c in used if c not in tg.EMOJI)
    check("코드베이스 이모지 코드 전부 매핑", not missing, str(missing))


def test_telegram_split() -> None:
    from radar.notify import telegram as tg
    print("\n[텔레그램 분할]")

    def unit(i: int) -> str:
        body = "\n".join(f'• <a href="https://x.com/{i}/{j}">기사 {j}</a> 설명 문장입니다 &amp; 더 있습니다'
                         for j in range(12))
        return f"<b>🚀 COIN{i}</b> — 급등했습니다\n{body}"

    units = ["<b>헤더</b>"] + [unit(i) for i in range(40)]
    chunks = tg.split_messages(units)
    check("여러 메시지로 분할", len(chunks) >= 2, str(len(chunks)))
    check("모든 조각 4096자 이하", all(len(c) <= 4096 for c in chunks), str([len(c) for c in chunks]))
    check("태그 짝 유지(b/a/i)", all(c.count("<b>") == c.count("</b>") and c.count("<a ") == c.count("</a>")
                                   and c.count("<i>") == c.count("</i>") for c in chunks))
    joined = "\n".join(chunks)
    check("코인 순서 유지", all(joined.find(f"COIN{i}<") < joined.find(f"COIN{i + 1}<") for i in range(39)))
    check("코인 단위가 쪼개지지 않음",
          all((f"COIN{i}<" in c) == (f'/{i}/11"' in c) for c in chunks for i in range(40)))
    check("이어서 표시", "(이어서 2/" in chunks[1])
    check("짧으면 1개", tg.split_messages(["<b>a</b>", "b"]) == ["<b>a</b>\n\nb"])
    big = "\n".join(f"<b>줄 {i}</b> " + "가" * 300 for i in range(40))
    parts = tg.split_messages([big])
    check("한 단위가 길면 줄 경계로", len(parts) >= 3 and all(len(p) <= 4096 for p in parts)
          and all(p.count("<b>") == p.count("</b>") for p in parts))
    huge = tg.split_messages(["&amp;" * 2000])
    check("한 줄 초과해도 엔티티 안 끊김",
          all(len(p) <= 4096 and not re.search(r"&[a-z]*$", p) for p in huge))


def test_telegram_tokens() -> None:
    from radar.notify import telegram as tg
    print("\n[텔레그램 토큰·토픽 선택]")
    with _EnvGuard():
        check("변수 없으면 미설정", not tg.available("pump"))
        check("미설정이면 조용히 skipped", tg.send("pump", ["x"])["status"] == "skipped")
        os.environ["TELEGRAM_BOT_TOKEN"] = "shared"
        os.environ["TELEGRAM_CHAT_ID"] = "-100123"
        check("공용 토큰 폴백", tg.token_for("pump") == "shared" and tg.token_for("stock") == "shared")
        os.environ["TELEGRAM_BOT_TOKEN_PUMP"] = "pump-bot"
        os.environ["TELEGRAM_BOT_TOKEN_TREND"] = "trend-bot"
        check("kind 전용 토큰 우선(pump)", tg.token_for("pump") == "pump-bot")
        check("kind 전용 토큰 우선(trend)", tg.token_for("trend") == "trend-bot")
        check("전용 없으면 공용(stock)", tg.token_for("stock") == "shared")
        del os.environ["TELEGRAM_BOT_TOKEN"]
        check("공용 없어도 전용만으로 사용", tg.available("pump") and not tg.available("stock"))
        os.environ["TELEGRAM_TOPIC_TREND"] = "42"
        check("토픽 id 정수", tg.topic_for("trend") == 42 and tg.topic_for("pump") is None)


class _Resp:
    def __init__(self, status: int, data: dict):
        self.status_code, self._d, self.text = status, data, str(data)

    def json(self):
        return self._d


def test_telegram_send() -> None:
    from radar.notify import telegram as tg
    print("\n[텔레그램 전송 로직 — 가짜 서버]")
    calls: list = []
    sleeps: list = []
    queue: list = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return queue.pop(0) if queue else _Resp(200, {"ok": True, "result": {}})

    orig_post, orig_sleep = tg.requests.post, tg.time.sleep
    tg.requests.post, tg.time.sleep = fake_post, lambda s: sleeps.append(s)
    try:
        with _EnvGuard():
            os.environ.update(TELEGRAM_BOT_TOKEN="SECRET123", TELEGRAM_CHAT_ID="-1001", TELEGRAM_TOPIC_PUMP="7")
            queue[:] = [_Resp(429, {"ok": False, "parameters": {"retry_after": 3}}), _Resp(200, {"ok": True})]
            r = tg.send("pump", ["<b>a</b>", "b"], photo="https://quickchart.io/chart?c=1", caption="<b>c</b>")
            check("사진+본문 전송 성공", r["status"] == "ok" and r["photo"] == "ok" and r["sent"] == 2, str(r))
            check("429 는 retry_after 만큼 쉬고 1회 재시도", 3 in sleeps and len(calls) == 4, f"{sleeps} {len(calls)}")
            check("사진 먼저", calls[0][0].endswith("/sendPhoto") and calls[-1][0].endswith("/sendMessage"))
            body = calls[-1][1]
            check("스레드·HTML·미리보기끔", body.get("message_thread_id") == 7 and body.get("parse_mode") == "HTML"
                  and body.get("disable_web_page_preview") is True and body.get("chat_id") == "-1001", str(body))
            check("메시지 사이 1초", sleeps.count(1.0) >= 2, str(sleeps))

            calls.clear()
            queue[:] = [_Resp(400, {"ok": False, "description": "Bad Request: wrong file"}),
                        _Resp(200, {"ok": True})]
            r = tg.send("pump", ["본문"], photo="https://bad", caption="x")
            check("사진 실패해도 본문 전송", r["photo"] == "failed" and r["sent"] == 1 and r["status"] == "ok", str(r))

            calls.clear()
            queue[:] = [_Resp(400, {"ok": False, "description": "Bad Request: can't parse entities"}),
                        _Resp(200, {"ok": True})]
            r = tg.send("pump", ["<b>깨진"])
            check("HTML 파싱 실패시 평문 재전송", r["status"] == "ok" and "parse_mode" not in calls[-1][1], str(r))

            queue[:] = [_Resp(500, {"ok": False, "description": "boom SECRET123"})]
            r = tg.send("pump", ["x"])
            check("실패는 error + 토큰 노출 없음", r["status"] == "error" and "SECRET123" not in str(r), str(r))
    finally:
        tg.requests.post, tg.time.sleep = orig_post, orig_sleep


def test_charts() -> None:
    from radar.notify import charts
    print("\n[차트 URL]")
    vals = [100 + i * 0.37 + (i % 7) for i in range(100)]
    url = charts.line_chart_url(vals, "SOL/USDT 5m price")
    check("quickchart URL", url.startswith("https://quickchart.io/chart?w=800&h=400"))
    check("URL 2000자 이하", 0 < len(url) <= 2000, str(len(url)))
    ds = charts.downsample(vals)
    check("점 48개 이하", len(ds) == 48 and ds[-1] == vals[-1])
    check("데이터 부족시 빈값", charts.line_chart_url([1.0], "x") == "")


def test_glossary() -> None:
    from radar import glossary
    print("\n[용어 풀이]")
    used: set = set()
    a = glossary.annotate_line("펀딩비가 올랐고 김프도 붙었습니다", used)
    b = glossary.annotate_line("다시 펀딩비와 김프 이야기입니다", used)
    check("처음 나올 때 풀이", "펀딩비(선물" in a and "김프(한국" in a, a)
    check("같은 메시지에선 한 번만", "(" not in b, b)
    c = glossary.annotate_line("<https://x.com/MDD|MDD 차트> `MDD`", set())
    check("링크·코드 안은 안 건드림", c == "<https://x.com/MDD|MDD 차트> `MDD`", c)
    d = glossary.annotate_line("*숏 스퀴즈 의심* — 숏 스퀴즈로 보입니다", set())
    check("굵게 안이면 굵게 뒤에 풀이", d.startswith("*숏 스퀴즈 의심* (숏 스퀴즈:") and d.count("하락에 건") == 1, d)
    e = glossary.annotate_line("역프 상태입니다", set())
    check("풀이 속 용어는 다시 안 풀기", e.count("(") == 1, e)
    check("영문 약어 경계", glossary.annotate_line("ABCB 토큰", set()) == "ABCB 토큰")


_HAEYO = re.compile(r"(?:해요|이에요|예요|에요|어요|아요|여요|워요|와요|돼요|봐요|줘요|세요|네요|죠|까요|래요|"
                    r"게요|군요|나요|지요|거든요|잖아요)(?=$|[\s.,!?)\]\"'~·—…:])")


def _fake_messages() -> list:
    from radar import runner
    ctx = reason.MarketContext()
    ctx.offline = True
    ctx.btc_chg24 = 3.0
    ctx.themes = {"AAA": ["밈코인"], "BBB": ["밈코인"]}
    ctx.pumping_themes = {"밈코인": 2}
    ctx.trending = {"AAA"}
    ctx.upbit_krw = {"AAA": {"price": 1500.0}, "BBB": {"price": 1300.0}}
    ctx.usdkrw = 1400.0
    raw = [{"symbol": "AAAUSDT", "base": "AAA", "price": 1.0, "market": "binance", "direction": "up",
            "chg5m": 3.2, "chg15m": 8.1, "chg1h": 12, "chg24h": 98.5, "qvol": 3e7, "vol_x": 4.2,
            "closes": [1 + i / 50 for i in range(48)]},
           {"symbol": "BBBUSDT", "base": "BBB", "price": 1.0, "market": "bitget", "direction": "down",
            "chg15m": -9, "chg24h": -12, "qvol": 2e6, "vol_x": 0},
           {"symbol": "ZZZUSDT", "base": "ZZZ", "price": 0.00012, "market": "bitget", "direction": "up",
            "chg15m": 6, "chg24h": 25, "qvol": 9e5, "vol_x": 0}]
    items = [{"key": h["base"], "raw": h, "reason": reason.explain_crypto(h, ctx)} for h in raw]
    items[0]["reason"]["evidence"].append({"w": 70, "tag": "숏 스퀴즈 의심", "icon": "🔥",
                                           "text": "펀딩비와 미결제약정을 함께 봐야 합니다"})
    pump_msg = runner.build_pump_msg(items, when="09/15 10:17", translate=False)

    trend_rows = [{"symbol": f"C{i}USDT", "base": f"C{i}", "score": 80 - i, "rs30": 20.5, "rs7": 4.1,
                   "fit": [0.9, 0.7, 0.3][i % 3], "near_high": 0.97, "mdd14": -12.3, "ema_stacked": i % 2 == 0,
                   "price": 2.5, "chg24": 1.2, "qvol": 5e7, "ratio_tail": [1 + j / 100 for j in range(48)]}
                  for i in range(3)]
    trend_msg = runner.build_trend_msg(trend_rows, {"C0USDT"}, {"universe_top_n": 250}, 65, when="09/15 10:17")

    b, s_theme = _fake_theme_block()
    stats = {"hits": 1, "misses": 1, "pending": 1, "resolved": 2, "rate": 50.0}
    now = time.time()
    results = [{"status": "hit", "name": "둘째소재", "symbol": "000002", "result_pct": 3.1, "ref_price": 5000,
                "ts": now - 86400, "deadline": now - 3600, "leader": "대장전자", "reported": 0},
               {"status": "pending", "name": "셋째정보", "symbol": "000003", "result_pct": 0.8, "ref_price": 8000,
                "ts": now - 3600, "deadline": now + 86400, "leader": "대장전자", "reported": 0}]
    stock_msg = runner.build_stock_market_msg("KR", "정규장", [b], results, stats, s_theme,
                                              when="09/16 10:17", translate=False)
    closed_msg = runner.build_stock_market_msg("US", "휴장", [], results[:1], stats, s_theme,
                                               when="09/16 10:17", translate=False, calls_allowed=False)
    return [pump_msg, trend_msg, stock_msg, closed_msg]


def test_tone() -> None:
    import ast
    print("\n[말투 — 합니다체]")
    msgs = _fake_messages()
    for m in msgs:
        slack = m.slack_text()
        tg_text = "\n".join(m.telegram_chunks())
        left = _HAEYO.findall(slack) + _HAEYO.findall(tg_text)
        check(f"{m.kind} 메시지에 해요체 없음", not left, str(left))
        check(f"{m.kind} 메시지가 합니다체", len(re.findall(r"(?:습니다|입니다|합니다)", slack)) >= 3)
        check(f"{m.kind} 텔레그램 헤더=토픽 이름", m.title in tg_text.split("\n")[0])
        check(f"{m.kind} 텔레그램에 슬랙 문법 잔재 없음",
              not re.search(r"<https?://|(?<![A-Za-z0-9])\*[^*\n]+\*", tg_text))
    check("trend 관찰 목록 고지 유지", "매수 추천이 아니라 관찰 목록입니다" in msgs[1].slack_text())
    check("pump 용어 풀이 1회", msgs[0].slack_text().count("선물 투자자끼리") == 1)
    check("pump 차트 사진", msgs[0].photo.startswith("https://quickchart.io/") and len(msgs[0].caption) <= 1024)
    check("trend 차트 사진", msgs[1].photo.startswith("https://quickchart.io/"))

    root = Path(__file__).resolve().parent.parent
    files = [root / "radar" / "runner.py", root / "radar" / "engine" / "reason.py", root / "radar" / "preview.py",
             root / "radar" / "notify" / "message.py", root / "radar" / "notify" / "sender.py",
             root / "radar" / "glossary.py", root / "run_once.py", root / "radar" / "markets.py",
             root / "radar" / "engine" / "theme_follow.py", root / "radar" / "sources" / "themes.py"]
    bad = []
    for py in files:
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and re.search(r"[가-힣]", node.value) and _HAEYO.search(node.value)):
                bad.append(f"{py.name}: {node.value[:40]}")
    check("소스 문자열 전체에 해요체 없음", not bad, str(bad))


def test_delivery() -> None:
    from radar import runner
    from radar.notify import sender
    print("\n[동시 발송·쿨다운]")
    msg = _fake_messages()[0]
    o_slack, o_tg, o_arch = sender._send_slack, sender.telegram.send, sender._archive
    sender._archive = lambda *a, **k: None
    try:
        def boom(*a, **k):
            raise RuntimeError("down")

        sender._send_slack = boom
        sender.telegram.send = lambda kind, chunks, photo="", caption="": {"status": "ok", "sent": len(chunks)}
        r = sender.deliver(msg)
        check("슬랙 예외여도 텔레그램 전송", r["delivered"] and r["telegram"]["status"] == "ok" and r["slack"] == "none")

        sender._send_slack = lambda kind, text, blocks: ("webhook", [])
        sender.telegram.send = boom
        r = sender.deliver(msg)
        check("텔레그램 예외여도 슬랙 전송", r["delivered"] and r["slack"] == "webhook"
              and r["telegram"]["status"] == "error")

        sender._send_slack = lambda kind, text, blocks: ("none", ["x"])
        sender.telegram.send = lambda *a, **k: {"status": "skipped"}
        check("둘 다 실패면 delivered=False", sender.deliver(msg)["delivered"] is False)
    finally:
        sender._send_slack, sender.telegram.send, sender._archive = o_slack, o_tg, o_arch

    tmp = Path(tempfile.mkdtemp()) / "c.db"
    orig_db, orig_deliver = store.DB_PATH, runner.deliver
    store.DB_PATH = tmp
    try:
        con = store.connect()
        runner.deliver = lambda m: {"slack": "none", "telegram": {"status": "error"}, "delivered": False}
        info = runner._finish("pump", con, msg, [("AAAUSDT", "8")], {})
        check("전송 실패면 쿨다운 미기록", not store.recently_alerted(con, "pump", "AAAUSDT", 60) and info["marked"] == 0)
        runner.deliver = lambda m: {"slack": "none", "telegram": {"status": "partial"}, "delivered": True}
        info = runner._finish("pump", con, msg, [("AAAUSDT", "8")], {})
        check("한쪽이라도 성공하면 쿨다운 기록", store.recently_alerted(con, "pump", "AAAUSDT", 60) and info["marked"] == 1)
        check("결과 JSON 에 telegram 표시", info.get("telegram", {}).get("status") == "partial")
        con.close()
    finally:
        store.DB_PATH, runner.deliver = orig_db, orig_deliver



def _fake_theme_block():
    from radar import runner
    from radar.engine import theme_follow as tf
    s = tf.settings({})
    members = [
        {"market": "KR", "symbol": "000001", "code": "000001", "name": "대장전자", "price": 13000, "chg": 29.9,
         "trade_value": 1300, "mcap": 1000, "vol_x": 8.0, "open": 10500, "high": 13000, "limit_up": True,
         "why": "", "series": [round(i * 0.6 + (1.0 if i % 2 else 0.0), 2) for i in range(50)]},
        {"market": "KR", "symbol": "000002", "code": "000002", "name": "둘째소재", "price": 5000, "chg": 1.2,
         "trade_value": 300, "mcap": 2200, "vol_x": 1.1, "why": "양자암호 장비 개발.",
         "series": [round(i * 0.02 + (0.0 if i % 2 else 0.3), 2) for i in range(50)]},
        {"market": "KR", "symbol": "000003", "code": "000003", "name": "셋째정보", "price": 8000, "chg": 27.0,
         "trade_value": 500, "mcap": 1500, "vol_x": 5.0, "why": "",
         "series": [round(i * 0.55 + (0.9 if i % 2 else 0.0), 2) for i in range(50)]},
        {"market": "KR", "symbol": "000004", "code": "000004", "name": "넷째", "price": 3000, "chg": 3.0,
         "trade_value": 20, "mcap": 300, "vol_x": 2.0, "why": ""},
    ]
    theme = {"id": "KR426", "name": "양자암호/양자컴퓨팅", "chg": 5.1, "rise": 3, "total": 4, "breadth": 0.75,
             "keywords": ["양자암호"]}
    leader = tf.pick_leader(members, s, "KR")
    peers = tf.rank_followers(leader, members, s, "KR")
    b = runner._block("KR", theme, leader, peers, members, s, offline=True)
    leader["catalyst"] = [{"title": "대장전자, 양자암호 칩 공급 계약 체결", "url": "https://n.news/1"}]
    return b, s


def test_markets() -> None:
    from datetime import datetime
    from radar import markets as mk
    print("\n[장 시간]")
    K = mk.KST
    tue10 = datetime(2026, 9, 15, 10, 0, tzinfo=K)
    check("한국 평일 10시 정규장", mk.kr_session(tue10) == "정규장")
    check("한국 16시 휴장", mk.kr_session(datetime(2026, 9, 15, 16, 0, tzinfo=K)) == mk.CLOSED)
    check("한국 토요일 휴장", mk.kr_session(datetime(2026, 9, 19, 10, 0, tzinfo=K)) == mk.CLOSED)
    check("미국 서머타임 KST 23:00 정규장", mk.us_session(datetime(2026, 9, 15, 23, 0, tzinfo=K)) == "정규장")
    check("미국 KST 18:00 프리마켓", mk.us_session(datetime(2026, 9, 15, 18, 0, tzinfo=K)) == "프리마켓")
    check("미국 KST 05:30 애프터마켓", mk.us_session(datetime(2026, 9, 16, 5, 30, tzinfo=K)) == "애프터마켓")
    check("미국 겨울 KST 23:00 은 프리마켓", mk.us_session(datetime(2026, 1, 13, 23, 0, tzinfo=K)) == "프리마켓")
    check("미국 겨울 KST 23:40 정규장", mk.us_session(datetime(2026, 1, 13, 23, 40, tzinfo=K)) == "정규장")
    check("미국 토요일 휴장", mk.us_session(datetime(2026, 9, 20, 0, 0, tzinfo=K)) == mk.CLOSED)
    check("한국 콜 마감 = 다음 거래일 15:30",
          mk.hit_deadline("KR", tue10.timestamp()) == datetime(2026, 9, 16, 15, 30, tzinfo=K).timestamp())
    check("금요일 콜 마감 = 월요일",
          mk.hit_deadline("KR", datetime(2026, 9, 18, 10, 0, tzinfo=K).timestamp())
          == datetime(2026, 9, 21, 15, 30, tzinfo=K).timestamp())
    check("미국 콜 마감 = 다음 거래일 16:00 ET",
          mk.hit_deadline("US", datetime(2026, 9, 15, 23, 0, tzinfo=K).timestamp())
          == datetime(2026, 9, 17, 5, 0, tzinfo=K).timestamp())


def test_theme_logic() -> None:
    from radar.engine import theme_follow as tf
    from radar.sources import themes as th
    print("\n[테마 추적·판정]")
    s = tf.settings({"hit_pct": 3.0, "_comment": "x"})
    check("설정 덮어쓰기", s["hit_pct"] == 3.0 and s["overheat_ratio"] == 0.8 and "_comment" not in s)
    check("미국 테마 사전", th.us_theme_of("IONQ") == "quantum" and th.us_theme_of("rklb") == "space")
    check("업종으로 테마 추정", th.us_theme_of("XYZ", "Uranium") == "nuclear" and th.us_theme_of("XYZ", "Banks") == "")
    check("한국 테마 키워드", th.kr_keywords("로봇(산업용/협동로봇 등)") == ["로봇"]
          and th.kr_keywords("양자암호/양자컴퓨팅") == ["양자암호", "양자컴퓨팅"])
    row = th.kr_row({"itemCode": "062970", "stockName": "한국첨단소재", "closePrice": "3,360", "closePriceRaw": "3360",
                     "fluctuationsRatio": "29.98", "accumulatedTradingValue": "134,216", "marketValue": "1,012",
                     "compareToPreviousPrice": {"name": "UPPER_LIMIT"}, "tradeStopType": {"name": "TRADING"}},
                    "양자 투자")
    check("네이버 테마 종목 파싱", row["price"] == 3360 and row["trade_value"] == 1342.2 and row["mcap"] == 1012
          and row["limit_up"] and row["why"] == "양자 투자", str(row))

    b, s0 = _fake_theme_block()
    check("대장주 = 거래대금 받친 최고 상승", b["leader"]["symbol"] == "000001")
    check("2·3등 = 대장 제외, 거래대금·시총·동조 순", [p["symbol"] for p in b["peers"]] == ["000003", "000002"],
          str([(p["symbol"], p["follow_score"]) for p in b["peers"]]))
    check("판정: 이미 따라감·과열", b["peers"][0]["verdict"] == "overheated")
    check("판정: 아직 안 움직임", b["peers"][1]["verdict"] == "lagging")
    lead = {"symbol": "A", "chg": 20}
    check("판정: 따라가는 중", tf.verdict(lead, {"chg": 6, "vol_x": 2.0}, s0) == "following")
    check("거래량 안 붙으면 아직", tf.verdict(lead, {"chg": 6, "vol_x": 1.0}, s0) == "lagging")
    check("관찰 콜은 덜 오른 종목만", b["calls"] == ["000002"], str(b["calls"]))
    check("거래대금 부족하면 대장 아님",
          tf.pick_leader([{"symbol": "A", "chg": 30, "trade_value": 10}], s0, "KR") is None)
    weak = tf.momentum(lead, [lead, {"symbol": "B", "chg": 0.1}, {"symbol": "C", "chg": -1}], s0)
    check("대장 혼자 상승 = 테마 힘 약함", weak["weak"])
    check("테마 힘 약하면 콜 없음", not tf.should_call(lead, {"chg": 0.1}, "lagging", weak, s0))
    check("과열 종목은 콜 없음", not tf.should_call(lead, {"chg": 19}, "overheated", {"weak": False}, s0))
    check("많이 빠진 종목은 콜 없음", not tf.should_call(lead, {"chg": -5}, "lagging", {"weak": False}, s0))
    big = {"symbol": "B", "chg": 1.0, "vol_x": 3.0, "mcap": 50000, "co_move": 0.9}
    check("덩치 20배 넘게 크면 콜 제외",
          tf.call_blocker({**lead, "mcap": 1000}, big, "lagging", {"weak": False}, s0) == "대장주보다 덩치가 너무 큼")
    quiet = {"symbol": "Q", "chg": 0.5, "vol_x": 0.4, "co_move": 0.1}
    check("거래량·장중 동조 근거 없으면 콜 제외",
          tf.call_blocker(lead, quiet, "lagging", {"weak": False}, s0) == "거래량·장중 동조 근거 부족")
    check("거래량 적어도 장중 동조 높으면 콜", tf.should_call(lead, {**quiet, "co_move": 0.6}, "lagging", {"weak": False}, s0))
    check("판정 흐름 이탈(하락)",
          tf.call_blocker(lead, {"chg": -2, "vol_x": 3}, "lagging", {"weak": False}, s0) == "흐름 이탈(하락)")

    inv = {"symbol": "A", "name": "대장", "price": 95, "high": 100, "open": 90, "chg": 20}
    check("무효: 고점 대비 5% 밀림", tf.leader_invalid(inv, s0).startswith("고점 대비"))
    check("무효: 시가 아래", tf.leader_invalid({**inv, "price": 89, "high": 90}, s0) == "시가 아래로 내려간")
    check("정상이면 무효 아님", tf.leader_invalid({**inv, "price": 99}, s0) == "")
    txt = tf.invalidation_text(inv, s0, lambda x: f"{x:,.0f}원")
    check("무효 조건 문구", "무효" in txt and "100원" in txt and "90원" in txt and "5%" in txt, txt)
    check("이미 무효면 콜 없음", not tf.should_call(inv, {"chg": 1}, "lagging", {"weak": False}, s0))
    check("장중 동조 상관도", (tf.co_move([0, 1, 0, 1, 0, 1, 0, 1, 0], [0, 2, 0, 2, 0, 2, 0, 2, 0]) or 0) > 0.99)


def test_call_tracking() -> None:
    from radar import runner
    from radar.engine import theme_follow as tf
    from radar.sources import themes as th
    print("\n[관찰 콜 결과 추적]")
    tmp = Path(tempfile.mkdtemp()) / "calls.db"
    orig_db, orig_daily = store.DB_PATH, th.kr_daily
    store.DB_PATH = tmp
    try:
        con = store.connect()
        now = time.time()
        cid = store.add_call(con, "KR", "000002", 5000, 2.0, now + 3600, name="둘째소재", theme="양자",
                             leader="대장전자", ts=now)
        c = store.open_calls(con, "KR")[0]
        st, pct, _ = tf.evaluate_call(c, 5050, 5200, now, use_high=False)
        check("+1% 는 진행 중(콜 당일 고가 무시)", st == "pending" and pct == 1.0, f"{st} {pct}")
        st, pct, _ = tf.evaluate_call(c, 5050, 5200, now, use_high=True)
        check("다음 거래일 고가 +4% 면 적중", st == "hit" and pct == 4.0, f"{st} {pct}")
        st, pct, _ = tf.evaluate_call(c, 4900, None, now + 7200)
        check("기한 지나면 빗나감", st == "miss" and pct == -2.0, f"{st} {pct}")

        store.update_call(con, cid, status="hit", result_pct=4.0, best_price=5200, last_price=5050)
        cid2 = store.add_call(con, "KR", "000003", 8000, 2.0, now + 3600, ts=now)
        store.update_call(con, cid2, status="miss", result_pct=-1.0, best_price=8000, last_price=7920)
        store.add_call(con, "KR", "000004", 3000, 2.0, now + 3600, ts=now)
        st = store.call_stats(con, "KR")
        check("누적 적중률 1/2 = 50%", st["hits"] == 1 and st["resolved"] == 2 and st["rate"] == 50.0
              and st["pending"] == 1, str(st))
        check("보고 대상 = 미보고 결과 + 진행 중", len(store.calls_for_report(con, "KR")) == 3)
        store.mark_reported(con, [cid, cid2])
        check("보고 후엔 진행 중만", [r["symbol"] for r in store.calls_for_report(con, "KR")] == ["000004"])
        check("시장별 분리 집계", store.call_stats(con, "US")["resolved"] == 0)

        th.kr_daily = lambda code: {"last": 3090.0, "high": 3090.0, "date": "2026-01-01"}
        n = runner._resolve_calls(con, "KR", tf.settings({}), now)
        row = store.open_calls(con, "KR")
        check("실행 시 시세로 자동 판정(적중)", n == 1 and not row and store.call_stats(con, "KR")["hits"] == 2,
              f"{n} {row}")
        con.close()
    finally:
        store.DB_PATH, th.kr_daily = orig_db, orig_daily


def test_news_relevance() -> None:
    from radar.sources import news as nw
    print("\n[뉴스 관련성]")
    check("티커는 대소문자 구분(POWER ≠ power)",
          not nw.title_matches("Ethiopia cuts power to bitcoin miners", tickers=["POWER"]))
    check("대문자 티커는 매칭", nw.title_matches("POWER token jumps 30%", tickers=["POWER"]))
    check("$티커 매칭", nw.title_matches("Why $IONQ soared today", tickers=["IONQ"]))
    check("주제 단어 없으면 탈락",
          not nw.title_matches("Latest chapter in laptop saga", must=["Saga"], context=["crypto", "token"]))
    check("주제 단어 접두 매칭", nw.title_matches("Saga cryptocurrency rallies", must=["Saga"], context=["crypto"]))
    check("단어 경계(Sagan 오탐 방지)", not nw.title_matches("Carl Sagan documentary", must=["Saga"]))
    check("회사명 핵심 추출", nw.company_core("IonQ, Inc.") == "IonQ" and nw.company_core("AB Inc.") == "")
    check("한글 종목명·테마 키워드", nw.title_matches("양자암호 관련주 강세", must=["대장전자", "양자암호"]))


def test_stock_msg() -> None:
    from radar.notify import charts
    print("\n[주식 테마 메시지]")
    msgs = _fake_messages()
    kr, closed = msgs[2], msgs[3]
    txt = kr.slack_text()
    check("관찰 대상 고지(추천 아님)", "매수·매도 추천이 아니라 관찰 대상 종목 목록입니다" in txt)
    check("콜 제외 사유 표시", "에서 제외했습니다 — 셋째정보: 이미 과열" in txt)
    check("관찰 콜 근거 3줄", "📣 *관찰 콜*" in txt and "— 둘째소재" in txt
          and all(k in txt for k in ("① 재료", "② 연결고리", "③ 숫자")))
    check("재료 뉴스 인용", "양자암호 칩 공급 계약" in txt)
    check("무효 조건 표시", "⛔ 무효 조건" in txt and "이 콜은 무효입니다" in txt)
    check("판정 라벨", "⚪ 이미 따라감·과열" in txt and "🟡 아직 안 움직임" in txt)
    check("결과 추적 + 누적 적중률", "✅ 적중" in txt and "⏳ 진행 중" in txt and "누적 적중률 1/2 (50%)" in txt)
    check("용어 풀이 1회(대장주)", txt.count("같은 테마 안에서 가장 먼저") == 1)
    check("'시가총액' 속 '시가' 오탐 없음", "시가(그날" not in txt.split("시가총액")[0][-3:] and "시가(그날 장이 열릴 때 처음 거래된 가격)총액" not in txt)
    check("시장 제목", "🇰🇷 한국 테마 추적" in kr.slack_header())
    check("비교 차트(대장 vs 2·3등)", kr.photo.startswith("https://quickchart.io/") and len(kr.photo) <= 2000
          and "Leader" in kr.photo)
    ctext = closed.slack_text()
    check("장 닫힌 시장은 결과만", "지난 콜 결과만" in ctext and "관찰 콜* —" not in ctext and "누적 적중률" in ctext)
    check("텔레그램 분할 4096 이하", all(len(c) <= 4096 for c in kr.telegram_chunks()))
    url = charts.multi_line_chart_url({"Leader A": list(range(390)), "#2 B": list(range(200)), "#3 C": [1, 2] * 50},
                                      "Intraday change %")
    check("다중선 차트 URL 2000자 이하", 0 < len(url) <= 2000, str(len(url)))


def main() -> int:
    test_indicators()
    test_btc_trend()
    test_store()
    test_filters()
    test_reason()
    test_telegram_convert()
    test_telegram_split()
    test_telegram_tokens()
    test_telegram_send()
    test_charts()
    test_glossary()
    test_tone()
    test_delivery()
    test_markets()
    test_theme_logic()
    test_call_tracking()
    test_news_relevance()
    test_stock_msg()
    if "--live" in sys.argv:
        test_live()
    print(f"\n{'=' * 46}\n결과: {PASS} PASS / {FAIL} FAIL\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

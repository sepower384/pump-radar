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


def main() -> int:
    test_indicators()
    test_btc_trend()
    test_store()
    test_filters()
    test_reason()
    if "--live" in sys.argv:
        test_live()
    print(f"\n{'=' * 46}\n결과: {PASS} PASS / {FAIL} FAIL\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

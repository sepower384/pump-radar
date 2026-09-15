# 🛰 PUMP RADAR

무료 공개 데이터만으로 **① BTC 대비 우상향 코인 발굴 · ② 급등 코인 · ③ 급등 주식(미국·한국)** 을 찾아
**"왜 올랐는지"까지 붙여서 슬랙으로** 쏜다. API 키 0개로 동작한다.

```
                    ┌ 바이낸스(전 종목 시세·캔들·선물 OI/펀딩)
                    ├ 비트겟(바이낸스 미상장 밈코인)
   무료 데이터 ─────┼ 업비트(원화 시세·김프·상장공지)
                    ├ 코인게코(트렌딩·테마·시총)
                    ├ 야후파이낸스(미국 급등주) · 네이버금융(한국 급등주)
                    └ 구글뉴스 RSS + 크립토 RSS 6종(급등 이유)
                            │
                   ┌────────┴────────┐
        BTC 우상향 점수판        급등 감지 + 이유 추론
                   └────────┬────────┘
                          슬랙
```

---

## 1. 5분 설치

```powershell
cd C:\dev\pump-radar
pip install requests

# 슬랙 웹훅 넣기
copy .env.example .env
notepad .env          # SLACK_WEBHOOK_URL= 에 붙여넣기

python run_once.py test      # 슬랙에 테스트 메시지가 오면 성공
```

### 슬랙 웹훅 만드는 법 (2분, 무료)
1. <https://api.slack.com/apps> → **Create New App** → *From scratch* → 워크스페이스 선택
2. 왼쪽 **Incoming Webhooks** → 스위치 **On**
3. **Add New Webhook to Workspace** → 알림 받을 채널 선택 → Allow
4. 생성된 `https://hooks.slack.com/services/...` 를 `.env` 의 `SLACK_WEBHOOK_URL=` 에 붙여넣기

> 채널을 나누고 싶으면 웹훅을 3개 만들어 `SLACK_WEBHOOK_TREND` / `_PUMP` / `_STOCK` 에 각각 넣는다.

### 웹훅을 못 만들 때 — 로그인된 크롬으로 보내기
`config.json` 의 `slack.backend` 를 `"playwright"` 로 두고, 크롬을 이렇게 띄운다:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:LOCALAPPDATA\ChromeRadar"
# 그 창에서 슬랙에 한 번 로그인해두면 끝
pip install playwright
```
단점: **그 PC가 켜져 있어야 한다.** 서버/클라우드에서는 웹훅만 쓸 수 있다.

---

## 2. 실행

| 명령 | 하는 일 |
|---|---|
| `python run_once.py` | 전부 1회 스캔 |
| `python run_once.py pump` | 코인 급등만 |
| `python run_once.py trend` | BTC 우상향만 |
| `python run_once.py stock` | 주식 급등만 |
| `python run_once.py test` | 슬랙+텔레그램 연결 테스트 |
| `python run_once.py preview-telegram` | 실제 데이터로 3종 메시지를 만들어 `data\outbox\preview_telegram_<kind>.html` 로만 저장 (전송·알림기록 없음) |
| `python tests\test_all.py` | 자체 검증 (오프라인) |

### 텔레그램 동시 발송 (슬랙과 병행)
| 환경변수 | 내용 |
|---|---|
| `TELEGRAM_BOT_TOKEN_PUMP` / `_TREND` / `_STOCK` | kind별 전용 봇 토큰 (없으면 공용 `TELEGRAM_BOT_TOKEN`) |
| `TELEGRAM_CHAT_ID` | 슈퍼그룹 id (`-100…`) |
| `TELEGRAM_TOPIC_PUMP` / `_TREND` / `_STOCK` | 토픽 스레드 id |

변수가 없으면 조용히 건너뛴다. 쿨다운은 슬랙·텔레그램 중 하나라도 성공하면 기록한다.
| `python tests\test_all.py --live` | 실제 API까지 검증 |

### 창 없이 24시간 돌리기 (내 PC)
```powershell
cd C:\dev\pump-radar\scripts
.\install_task.ps1          # 작업 스케줄러 등록 + 즉시 시작 (검은 창 안 뜸)
.\install_task.ps1 -Remove  # 해제
.\stop_watch.ps1            # 지금 도는 것만 중지
```
로그: `data\watch.log` · 전송된 알림 원문 보관: `data\outbox\YYYYMMDD.md`

### PC를 꺼도 돌리기 (GitHub Actions, 무료)
1. 이 폴더를 **비공개 저장소**로 push
2. 저장소 **Settings → Secrets and variables → Actions** 에 `SLACK_WEBHOOK_URL` 등록
3. `.github/workflows/radar.yml` 이 10분마다 급등 스캔, 4시간마다 BTC 우상향 리포트를 돌린다

> Actions 는 최소 주기가 5분이고 혼잡하면 지연된다. **1분 단위 실시간이 필요하면**
> 오라클 클라우드 무료 VM(Always Free)이나 아무 상시 서버에 올려 `watch.py` 를 돌리는 게 낫다.

---

## 3. 뭘 어떻게 판단하는가

### ① BTC 대비 우상향 (`btc_trend`)
USDT 가격이 아니라 **코인/BTC 비율 차트**를 본다. 비트코인보다 강한 코인만 걸러내기 위해서다.
4시간봉 180개(≈30일)로 0~100점:

| 항목 | 배점 | 의미 |
|---|---|---|
| 7일 BTC 대비 초과수익 | 20 | 최근 상대강도 |
| 30일 BTC 대비 초과수익 | 15 | 추세의 길이 |
| 일평균 기울기(로그 회귀) | 20 | 오르는 속도 |
| 추세 정합도 R² | 15 | 계단식 우상향인지, 튀는 건지 |
| 30일 고점 근접도 | 15 | 신고가 부근인지 |
| EMA20 위에 머문 비율 | 8 | 지지받고 있는지 |
| EMA20 > EMA50 정배열 | 7 | 구조 |
| 14일 MDD 페널티 | −15 | 변동성만 큰 종목 감점 |

### ② 급등 감지 (`pump_crypto`)
- 매 사이클 **전 종목 시세 스냅샷**을 저장 → 5분/15분/1시간 변동률을 싸게 계산
- 후보만 5분봉을 받아 **거래대금이 평소의 몇 배인지** 검증 (가짜 급등 제거)
- 바이낸스 미상장 종목은 **비트겟**에서 별도로 훑는다 (신규 밈코인 커버)

### ③ 왜 올랐나 (`reason`)
증거를 세기 순으로 쌓아 상위 4개를 붙인다.

| 순위 | 근거 | 출처 |
|---|---|---|
| 100 | 거래소 상장/공지 | 업비트 공지 API |
| 88·84 | 뉴스 | 크립토 RSS 6종 + **구글뉴스 종목 검색**(한/영) |
| 75 | 숏스퀴즈 (가격↑ + 미결제약정↓) | 바이낸스 선물 |
| 70 | 선물 자금유입 (미결제약정↑) | 바이낸스 선물 |
| 60 | 테마 순환매 (같은 섹터 2종목 이상 동시 급등) | 코인게코 카테고리 |
| 55 | 롱 과열 (펀딩비) | 바이낸스 선물 |
| 50 | 국내 매수세 (김프) | 업비트 |
| 45 | 검색 급증 | 코인게코 트렌딩 |
| 35 | 거래량 폭증만 | 바이낸스 |
| 0 | **원인 미확인 → "수급(세력) 주도 의심"** | — |

`GEMINI_API_KEY` 를 넣으면 이 근거들을 한 문장으로 요약해 맨 위에 붙인다. **없어도 동작한다.**

주식은 미국=야후 종목 RSS + 구글뉴스, 한국=네이버 종목뉴스 + 구글뉴스로 같은 방식.
시황·증시요약 기사는 이유가 될 수 없으므로 걸러낸다.

---

## 4. 튜닝 (`config.json`)

알림이 **너무 많으면** 올리고, **너무 적으면** 내린다.

```jsonc
"btc_trend":   { "score_threshold": 65,  "cooldown_min": 720 }   // 점수 기준·재알림 간격
"pump_crypto": { "chg_5m_pct": 3.0,      "volume_surge_x": 3.0,  // 급등 민감도
                 "cooldown_min": 60,     "include_dumps": true } // 급락도 받을지
"pump_stock":  { "us": {"min_change_pct": 8.0}, "kr": {"min_change_pct": 8.0} }
"scan":        { "interval_sec": 300, "quiet_hours_kst": [1,2,3,4,5,6] }  // 새벽 알림 끄기
```

---

## 5. 알아둘 것

- **투자 판단 도구가 아니라 탐지 도구다.** 급등은 이미 오른 뒤에 잡히고, "원인 미확인"은
  대개 좋은 신호가 아니다. 알림은 *확인할 거리*이지 매수 신호가 아니다.
- 무료 API에는 호출 제한이 있다. 코인게코는 캐시(15분~6시간)로 감싸 두었고, 주기를
  1분 밑으로 내리면 차단될 수 있다.
- 야후·네이버는 공개 데이터를 그대로 읽는다. 사이트 개편 시 `radar/sources/stocks.py` 만
  고치면 된다. (2026-09-12: 네이버 금융 PC가 Next.js로 개편되며 `sise_rise.naver` HTML
  테이블이 사라져, 한국 급등주는 모바일 공개 API `m.stock.naver.com/api/stocks/up/{KOSPI|KOSDAQ}`
  로 교체했다. 코스피·코스닥 각각 상위 100종목을 JSON으로 받아 거래대금도 정확해졌다.)

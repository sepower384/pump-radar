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

## 4-1. 정기 보고서 (주간·월간·분기·연간 PDF)

보낸 알림은 `history/YYYY-MM.jsonl`에 쌓이고, 클라우드에선 저장소의 **`history` 브랜치**에 영구 보관된다
(sqlite 캐시는 비워질 수 있어서). 2시간 주기 실행이 기한을 확인해 급등탐정 방으로 PDF를 보낸다.

| 보고서 | 보내는 때 (KST) | 내용 |
|---|---|---|
| 주간 | 월요일 9시 이후 첫 실행 | 지난주 월~일 |
| 월간 | 매월 1일 9시 이후 | 지난달 |
| 분기 | 1·4·7·10월 1일 9시 이후 | 지난 분기 |
| 연간 | 1월 1일 9시 이후 | 작년 |

- 코인 급등은 **알림 뒤 24시간 1시간봉으로 채점**한다: +5% 이상 더 오르면 '추가 상승', 24시간 뒤 -10% 이하면 '되밀림'.
  확정된 채점은 `history/outcomes.json`에 캐시.
- 구성: 표지(KPI 4개 + 핵심 요약) → 이유별 성적표·시간대·기간 흐름 → 가장 크게 오른 코인 → 미국·한국 테마·관찰 콜 성적 → 다음에 지켜볼 것·읽는 법.
- 문장은 숫자에서 바로 나오는 것만 쓴다(LLM 없음). 발송 기록은 `history/reports_sent.json`.
- 과거분: `python run_once.py backfill`이 outbox 원문(2026-09-13~)을 읽어 채운다(중복 없음).

```bash
python run_once.py report week --partial          # 이번 주 중간 집계 PDF (전송 없음)
python run_once.py report month --at 2026-10-01   # 특정 시점 기준 지난달
python run_once.py report week --send             # 지난주 보고서를 지금 보내기
gh workflow run radar.yml -f mode="report week --partial"   # 클라우드에서 생성 → Actions 산출물
```

## 4-2. 첫 포착 이후 추적 (`followup`)

"그때 알려준 거, 그래서 어떻게 됐나"를 자동으로 따라간다. 기준은 언제나 **첫 포착 가격**이다.
같은 자산이 또 잡혀도 새 기록을 만들지 않고 **재포착 회차**만 올린다(설정 `dedupe_days`, 기본 30일).

- 구간: **1일 · 3일 · 7일 · 30일** (`followup.horizons_h`). 각 구간의 끝 가격 + 그 구간의 최고/최저를 잰다.
- 시세(키 불필요): 코인=바이낸스·비트겟 1시간봉(30일 구간은 비트겟만 4시간봉), 미국주식=야후 일봉, 한국주식=네이버 일봉.
- 확정된 구간은 `history/followups.json`에 굳혀 두고 다시 받지 않는다. 79종목 갱신에 약 4초.
- **주 1회(기본 월요일 09시 KST, `digest.every`=`week`/`day`)** 각 방에 성적표를 보낸다: 지난 성적표 이후 새로 확정된 것
  → 지금 추적 중인 것(가장 밀린 것 포함) → 구간별 평균·오른 비율. 매일 보내면 틀린 날이 매일 드러나 방이 지친다. 주간·월간 PDF에도 「첫 포착 이후, 그래서 얼마나 갔나」 페이지로 들어간다.
- 대장주 추적을 위해 주식 알림 기록에 대장주 `price`를 함께 남긴다(2026-09-20부터. 그 이전 기록은 가격이 없어 제외).

```bash
python run_once.py followup           # 갱신 + 기한이면 성적표 발송
python run_once.py followup --dry     # 갱신만(전송 없음)
python run_once.py followup --force   # 오늘 이미 보냈어도 지금 다시
```

## 4-3. 만족도 조사 (`survey`) — 매달 1일

매달 **1일 10시(KST)** 이후 첫 실행에서 스터디방에 텔레그램 **익명 투표 2개**를 올린다.

1. 만족도 5점 — "지난 한 달, 포착과 성적표가 투자 판단에 도움이 됐나요?"
2. 복수 선택 — "다음 달에 가장 보고 싶은 것" (성적표 상세 / 이유 심층 / 알림 줄이기·늘리기 / 주식·코인 비중 / 용어 설명 …)

- 투표 집계는 매 실행마다 `getUpdates`로 받아 `history/survey.json`에 저장하고, 다음 달 조사 메시지와
  월간 PDF에 「이 방 만족도 조사 결과」로 요약한다(만족도 5점 평균 자동 계산).
- 자유 서술 피드백은 **봇에게 1:1 메시지**로 받는다. 원문은 **공개 저장소에 남기지 않는다** —
  `TELEGRAM_ADMIN_CHAT_ID`로 바로 전달하고, 기록에는 건수만 남긴다(원문은 `history/private/`, git 제외).

```bash
python run_once.py survey             # 기한이면 조사 발송 + 응답·피드백 수집
python run_once.py survey --force     # 지금 바로 조사 올리기
python run_once.py survey-results     # 지금까지 모인 결과
```

## 5. 알아둘 것

- **투자 판단 도구가 아니라 탐지 도구다.** 급등은 이미 오른 뒤에 잡히고, "원인 미확인"은
  대개 좋은 신호가 아니다. 알림은 *확인할 거리*이지 매수 신호가 아니다.
- 무료 API에는 호출 제한이 있다. 코인게코는 캐시(15분~6시간)로 감싸 두었고, 주기를
  1분 밑으로 내리면 차단될 수 있다.
- 야후·네이버는 공개 데이터를 그대로 읽는다. 사이트 개편 시 `radar/sources/stocks.py` 만
  고치면 된다. (2026-09-12: 네이버 금융 PC가 Next.js로 개편되며 `sise_rise.naver` HTML
  테이블이 사라져, 한국 급등주는 모바일 공개 API `m.stock.naver.com/api/stocks/up/{KOSPI|KOSDAQ}`
  로 교체했다. 코스피·코스닥 각각 상위 100종목을 JSON으로 받아 거래대금도 정확해졌다.)

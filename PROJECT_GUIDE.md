# GoodNews AI — 프로젝트 전체 지침

> 이 문서는 새 대화창에서 프로젝트 맥락을 즉시 공유하기 위한 지침서입니다.
> 코드 작업은 커서 AI와 함께, 설계/기획 논의는 Claude와 함께 진행합니다.
> 최종 갱신: 2026-06-17 (daily/weekly/midterm 구조화 출력 전환, dry-run
> 시뮬레이션 트리거 버그 수정 반영)

---

## 1. 서비스 개요

**GoodNews AI**는 미국 주식 4,000여 개 종목의 뉴스를 FMP API로 수집하고,
Gemini로 AI 요약 리포트를 생성해 개인 투자자에게 제공하는 백엔드 서비스입니다.

**핵심 가치**
- 영문 뉴스를 한국어로 자동 요약
- 호재 / 악재 / 중립 감성 분류
- 일간 / 주간 / 중장기(12주 집계) 3가지 주기 리포트
- 중복 뉴스 자동 제거 (크로스 티커 병합 포함)

**타겟 사용자**: 미국 주식에 투자하지만 모든 영문 뉴스를 팔로우하기 어려운
개인 투자자

**주기 구조 변경 안내**: 초기 설계에는 monthly/yearly 주기가 있었으나
실제 운용 가치가 낮아 제거했고, 대신 **12주 단위로 누적 성과와 추세를
보여주는 midterm 리포트**로 대체했다(`migrate_remove_monthly_yearly.py`로
DB에서도 제거 완료). 현재 `digest_type`은 `daily` / `weekly` / `midterm`
세 가지만 유효하다.

---

## 2. 기술 스택

| 분류 | 기술 |
|------|------|
| 언어 | Python 3.13 |
| 웹 프레임워크 | FastAPI (코드 작성 완료, 실제 기동/테스트는 아직 미착수) |
| DB | PostgreSQL (Supabase 무료 플랜, Seoul 리전) |
| ORM | SQLAlchemy 2.0 (asyncpg) |
| 뉴스 수집 | FMP API `/stable/news/stock` |
| 일반 시장 뉴스 | FMP API `/stable/news/general-latest` (섹터 리포트용) |
| 가격/벤치마크 | FMP API `/stable/historical-price-eod/light`,
  `/stable/historical-sector-performance` |
| 종목 유니버스 | FMP API `/stable/company-screener` |
| AI 요약 | Google Gemini — `gemini-2.5-flash`(weekly/midterm/sector),
  `gemini-2.5-flash-lite`(daily) |
| 구조화 출력 | Gemini `response_schema`(Pydantic) — daily/weekly/midterm
  공통 적용 (아래 8절 참고) |
| 중복 제거 | Jaccard similarity (단어 집합 기준) |
| 스케줄러 | Celery + Redis (코드 작성 완료, 실제 Celery worker로
  장시간 가동 검증은 아직 미착수) |
| 시뮬레이션 | `scripts/simulate_range.py` — Celery 없이 기간을 순차
  실행하며 dry-run/실DB 모드 모두 지원 (아래 9절 참고) |
| HTTP 클라이언트 | httpx (비동기), requests (동기) |
| 로깅 | loguru |
| 설정 관리 | pydantic-settings |

**클라우드 배포 / 프론트엔드**: 아직 착수 전(0%). 현재는 로컬 환경에서
백엔드 파이프라인과 dry-run 시뮬레이션만 검증된 상태다.

---

## 3. 전체 데이터 파이프라인

```
[1단계] 유니버스 빌드 (주 1회, 일요일 02:00 ET)
  FMP /stable/company-screener
  → NASDAQ / NYSE / AMEX 상장
  → 시총 1억 USD 이상
  → ETF·펀드·워런트·우선주 등 제외
  → 약 4,000개 종목 → data/universe/universe_current.csv

[2단계] 뉴스 수집
  [백필] 2026-01-01 ~ 현재 (최초 1회, 완료됨)
    FMP /stable/news/stock
    → 티커 1개씩 개별 요청 (배치 요청 시 limit이 전체에 적용되는 문제 회피)
    → 월별 폴더(data/backfill/YYYY_MM/TICKER.json)로 저장

  [일간] 매일 2회 (closing 21:00 ET, premarket 08:00 ET)
    최근 뉴스 수집 → articles 테이블에 url_hash 기준 중복 체크 후 INSERT

[3단계] 중복 제거
  [백필] 로컬에서 전처리 후 DB 업로드 (완료됨)
    ① 동일 티커 내 URL 완전 동일 → 제거
    ② 크로스 티커 URL 중복 → tickers 배열에 모든 종목 태그 병합 (1건으로)
    ③ 동일 티커 내 유사 뉴스 → Jaccard similarity ≥ 0.8 → 본문 더 긴 것만 유지
    → data/clean/YYYY_MM.json → Supabase articles 테이블 업로드

  [일간] DB의 url_hash PK + ON CONFLICT 로 자동 처리

[4단계] AI 요약 생성 — daily
  articles 테이블에서 ticker별 최근 뉴스 조회
  → 뉴스 건수로 분기
      4건 이하 → PROMPT_SIMPLE (자유 텍스트, 가벼운 형식)
      5건 이상 → PROMPT_FULL  (구조화 출력 — 8절 참고)
  → Gemini 호출 → sentiment 결정 → news_summaries Upsert

[5단계] AI 요약 생성 — weekly (월요일 draft → 금요일 final)
  이번 주 daily 리포트들(또는 원본 뉴스로 보완)을 입력으로
  → 구조화 출력으로 주간 흐름/호재·악재/온도 변화/종합 판단 생성
  → 금요일에는 가격 벤치마크(S&P500, 섹터)도 함께 계산해 price_change_pct 저장

[6단계] AI 요약 생성 — sector news (금요일, weekly-final 30분 후)
  그 주 일반 시장 뉴스를 12개 카테고리로 분류해 섹터별 요약 생성
  → sector_news_summaries Upsert

[7단계] AI 요약 생성 — midterm (금요일, sector-news 30분 후)
  최근 최대 12주의 weekly final + 가격 벤치마크 + 섹터 뉴스를 입력으로
  → 구조화 출력으로 중장기 흐름/추세/누적 성과(숫자는 직접 계산)/
    섹터 비교/종합 판단 생성. weekly final이 1~2개뿐이면 LLM 없이
    템플릿만으로 생성(_build_midterm_template)
  → 트리거 조건은 12절 참고

[8단계] API 서빙 (FastAPI, 코드 작성 완료·실행 테스트 전)
  GET /summary/{ticker}?digest_type=daily
  GET /feed?tickers=AAPL,NVDA&digest_type=daily
  GET /summary/{ticker}/all
  GET /universe/stats
  POST /universe/build
```

---

## 4. DB 테이블 구조

### articles (원본 뉴스 저장)
```sql
url_hash     VARCHAR(64)   PK  -- SHA256(url)
title        TEXT
text         TEXT              -- 본문 전문
published_at TIMESTAMPTZ
source       VARCHAR(100)      -- 출처 도메인 (reuters.com 등)
url          TEXT
tickers      TEXT[]            -- PostgreSQL 배열 ["NVDA", "AVGO"]
created_at   TIMESTAMPTZ       -- DEFAULT NOW()
```
- 인덱스: `published_at` (정렬), `tickers` GIN 인덱스 (배열 검색)

### news_summaries (AI 요약 저장 — daily/weekly/midterm 공용)
```sql
ticker            VARCHAR(10)   PK
digest_type       VARCHAR(10)   PK  -- daily | weekly | midterm
report_date       DATE          PK, NULLABLE
                                 -- daily: ET 기준 날짜
                                 -- weekly: 해당 주 월요일
                                 -- midterm: 해당 주 월요일(생성 시점)
version           VARCHAR(10)   -- daily: closing|premarket
                                 -- weekly: draft|final
                                 -- midterm: final (또는 LLM 미사용 시 템플릿)
summary_text      TEXT          -- AI 요약 텍스트 (고정 템플릿 + LLM 내용, 8절 참고)
sentiment         VARCHAR(10)   -- bullish | bearish | mixed | neutral (NULL 가능 — midterm 템플릿 경로)
source_urls       JSONB         -- 원문 URL 리스트 (daily 전용)
price_change_pct  FLOAT         -- weekly/midterm 전용, 누적/주간 변동률
updated_at        TIMESTAMPTZ
```

**주의**: 코드 내 일부 주석은 여전히 "daily | weekly | monthly | yearly"라고
표기되어 있으나 이는 갱신되지 않은 옛 주석이다. monthly/yearly는
`migrate_remove_monthly_yearly.py`로 완전히 제거됐고, midterm이 그
역할을 대체한다.

### weekly_benchmarks (S&P500 / 섹터 주간 변동률)
```sql
benchmark_type   VARCHAR(10)   -- sp500 | sector
benchmark_name   VARCHAR(50)   -- 'SP500' 또는 섹터명(Technology 등)
exchange         VARCHAR(10)   -- sector 전용 (NASDAQ/NYSE/AMEX), sp500은 NULL
week_monday      DATE
change_pct       FLOAT
```
PK: (benchmark_type, benchmark_name, exchange, week_monday)

### sector_news_summaries (섹터별 주간 시장 뉴스 요약)
```sql
category       VARCHAR(50)   -- 12개 카테고리 중 하나
week_monday    DATE
summary_text   TEXT
sentiment      VARCHAR(10)
```
PK: (category, week_monday)

---

## 5. 프로젝트 파일 구조

```
goodnews/
├── app/
│   ├── api/
│   │   └── main.py              FastAPI 서버 (작성 완료, 실행 테스트 전)
│   ├── core/
│   │   └── config.py            환경변수 (pydantic-settings)
│   ├── models/
│   │   └── database.py          SQLAlchemy 모델 + 모든 DB 조회/upsert 함수
│   ├── scheduler/
│   │   ├── fmp_collector.py     FMP 뉴스 수집 (비동기 배치)
│   │   ├── price_collector.py   가격/섹터 벤치마크 수집
│   │   └── tasks.py             Celery 태스크 + beat_schedule + 트리거 로직
│   ├── summarizer/
│   │   ├── llm_summarizer.py    Gemini 요약 — daily/weekly/midterm/sector_news
│   │   └── deduplicator.py      중복 제거 (Jaccard)
│   └── universe/
│       ├── fmp_client.py
│       ├── fmp_company_screener.py
│       ├── fmp_etf_stock_reference.py
│       ├── fmp_profile_enrich.py
│       ├── finviz_like_equity_filter.py
│       ├── universe_pipeline.py
│       ├── universe_runner.py
│       ├── universe_reason_codes.py
│       ├── ticker_store.py
│       └── universe_save.py
├── scripts/
│   ├── backfill_news.py             백필 뉴스 수집
│   ├── backfill_benchmarks_news.py  백필 벤치마크/섹터뉴스
│   ├── deduplicate_backfill.py      백필 중복 제거
│   ├── upload_backfill.py           clean JSON → Supabase 업로드
│   ├── retry_failed_tickers.py      백필 실패 종목 재시도
│   ├── build_universe_run.py        유니버스 빌드 실행
│   ├── select_sample_100.py         시뮬레이션용 100종목 샘플 선정
│   ├── simulate_range.py            기간 시뮬레이션 (dry-run 지원, 9절 참고)
│   ├── analyze_simulation_results.py  시뮬레이션 비용/결과 분석
│   ├── migrate_remove_monthly_yearly.py  monthly/yearly DB 제거 마이그레이션
│   ├── test_fmp_only.py
│   ├── test_news_fetch.py
│   ├── test_pipeline.py
│   ├── test_summarizer.py
│   ├── test_daily_pipeline.py
│   ├── test_weekly_pipeline.py
│   └── test_sector_news.py
├── tests/
│   ├── test_midterm_trigger.py      should_generate_midterm() 단위 테스트
│   └── test_midterm_structured.py   render_midterm_report/summarize_midterm 테스트
├── sim_results/                     dry-run 시뮬레이션 출력 (gitignore 대상)
├── data/
│   ├── universe/universe_current.csv
│   ├── backfill/YYYY_MM/TICKER.json
│   └── clean/YYYY_MM.json
├── .env
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## 6. 환경변수 (.env)

```
FMP_API_KEY=...
FMP_BASE_URL=https://financialmodelingprep.com/api/v3

GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash           # weekly/midterm/sector_news
GEMINI_MODEL_LITE=gemini-2.5-flash-lite # daily

DATABASE_URL=postgresql+asyncpg://...@aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres

REDIS_URL=redis://localhost:6379/0      # Celery용 (미가동 검증)

UNIVERSE_DATA_DIR=./data/universe
UNIVERSE_MIN_MARKET_CAP=100000000.0
UNIVERSE_EXCHANGES=NASDAQ,NYSE,AMEX

TICKER_BATCH_SIZE=30
MAX_NEWS_PER_TICKER=50

API_HOST=0.0.0.0
API_PORT=8000
```

---

## 7. FMP API 엔드포인트 사용 현황

| 엔드포인트 | 용도 | 비고 |
|-----------|------|------|
| `/stable/company-screener` | 종목 유니버스 수집 | 시총 버킷별 호출 |
| `/stable/etf-list` | ETF 블랙리스트 | |
| `/stable/stock-list` | 종목 타입 참조 | |
| `/stable/profile` | 신규 종목 필드 보강 | 선택적 |
| `/stable/news/stock` | 종목별 뉴스 수집 | `symbols`, `from`, `to`, `page` |
| `/stable/news/general-latest` | 일반 시장 뉴스 (sector news용) | `/stable/general-news`는 404, 이 엔드포인트가 정식 |
| `/stable/historical-price-eod/light` | 종목/지수 EOD 가격 | S&P500은 `^GSPC` |
| `/stable/historical-sector-performance` | 섹터 변동률 | `exchange` 파라미터 명시 권장 |

**주의**: `/api/v3/stock_news`는 현재 플랜(Premium Annual)에서 403 →
`/stable/news/stock` 사용.

---

## 8. AI 요약 — 구조화 출력 방식 (daily / weekly / midterm)

### 설계 원칙

기존에는 LLM에게 마크다운 헤더(`[오늘의 핵심 한 줄]` 등)까지 포함한
전체 텍스트를 직접 쓰게 시키고, 그 텍스트를 정규식으로 다시 파싱해서
sentiment 등을 추출했다. 이 방식은 LLM이 헤더 문구나 형식을 미세하게
다르게 쓸 때마다 파싱이 깨지는 문제가 있었다.

현재는 다음 방식으로 통일했다.

1. LLM은 Pydantic 스키마(JSON)로 **내용 필드만** 채운다
   (`response_schema`로 Gemini structured output 강제).
2. 헤더 · 섹션 순서 · 불릿 형식 · 구두점은 Python 쪽 고정 템플릿 함수
   (`render_full_report`, `render_weekly_report`, `render_midterm_report`)
   가 조립한다.
3. sentiment는 텍스트 정규식 파싱이 아니라 LLM이 준 enum 값
   (`bullish`/`bearish`/`mixed`/`neutral`)을 그대로 검증해서 쓴다.
4. 항목이 비면(예: 호재 없음) 섹션 자체를 생략하거나 "없음"으로
   채우는 처리도 템플릿이 결정론적으로 담당한다.
5. 사전에 정확히 계산된 숫자(예: midterm의 누적 수익률, alpha)는
   LLM이 다시 쓰지 않고 템플릿이 직접 삽입한다 — LLM은 그 숫자에 대한
   해석 문장만 작성한다.

### daily (PROMPT_SIMPLE / PROMPT_FULL)

- 뉴스 4건 이하: `PROMPT_SIMPLE` — 자유 텍스트, 가벼운 형식 유지
- 뉴스 5건 이상: `PROMPT_FULL` → `FullReportData` 스키마
  - `headline`, `positives`, `negatives`, `neutral_items`,
    `temperature_gap`, `checkpoint_section`(시장 반응과 체크포인트 —
    "시장이 틀렸다"는 대비 구도가 아니라 병기하는 진단), `sentiment`,
    `sentiment_reason`, `investor_view`, `next_watch`
  - 출력 헤더: `[중립/매크로]`, `[시장 반응과 체크포인트]`,
    `[다음에 체크해야 할 뉴스]` 등 (AI적 표현을 피해 자연스러운
    한국어 워딩으로 조정됨)

### weekly (PROMPT_WEEKLY_FROM_DAILIES / FROM_ARTICLES / UPDATE)

- `WeeklyReportData` 스키마 → `headline`, `weekly_flow`,
  `positives`/`negatives`(각 항목은 `{content, category}`),
  `sentiment_start`/`sentiment_end`/`temperature_reason`(주간 온도
  변화), `next_watch`, `sentiment`, `sentiment_reason`
- 카테고리(`WEEKLY_CATEGORIES`)는 5개로 통일:
  `실적_재무`, `사업_운영`, `시장평가`, `경영_인사`, `거시_섹터`
  (호재/악재가 서로 다른 카테고리명을 쓰던 과거 방식을 통합)

### midterm (PROMPT_MIDTERM)

- `MidtermReportData` 스키마 → `headline`, `flow_narrative`,
  `trend_items`(weekly와 동일한 5개 카테고리 재사용),
  `trend_interpretation`, `benchmark_interpretation`,
  `sector_comparison`, `sentiment`, `sentiment_reason`
- `[누적 성과 vs 벤치마크]`의 숫자 5줄(이 종목/S&P500/섹터 누적
  수익률, 시장 대비·섹터 대비 alpha)은 LLM이 작성하지 않고
  `summarize_midterm()`에서 미리 계산한 값을 템플릿이 직접 포맷
- `sector_comparison`은 날짜를 하나씩 나열하거나("1월 26일 주간에는
  ~") 시간 흐름을 서술하는 방식("과거에는 ~했지만 최근엔 ~")을
  금지하고, 현재 시점의 동조/디커플링 상태를 진단하는 정적인 문단으로
  쓰도록 프롬프트에 명시
- weekly final이 1~2개뿐이면 LLM 호출 없이 `_build_midterm_template()`
  으로 템플릿만 반환 (sentiment는 NULL)

---

## 9. dry-run 시뮬레이션 (`scripts/simulate_range.py`)

운영 스케줄(Celery)을 가동하지 않고도, 지정한 기간을 하루씩 순차
실행하며 daily/weekly/sector_news/midterm 전체 경로를 검증할 수 있다.

```bash
python scripts/simulate_range.py --start 2026-01-01 --end 2026-03-31 \
    --tickers AAPL,NVDA --dry-run
```

- `--dry-run`: 운영 DB에 쓰지 않고 `sim_results/dry_run_{start}_{end}/`
  에 종목별 JSON 파일로 저장. `usage_summary.json`에 LLM 호출 수/토큰
  사용량도 모델별·digest_type별로 집계됨.
- `--universe`: 전체 유니버스(약 4,000종목)로 실행 — 시간/비용이
  매우 크므로 주의.
- `--no-sector-news`, `--no-midterm`: 해당 단계 생략 (기본값은 둘 다
  포함).
- 샘플 100종목: `scripts/select_sample_100.py`로 시총 대형/중형/소형
  균등 + 11개 섹터 균등 배분으로 선정(`sim_results/sample_100_tickers.txt`).

**주의 — dry-run과 트리거 판단의 관계**: midterm 트리거(`should_generate_
midterm`)와 그 입력 조회(`get_recent_weekly_finals_for_midterm`,
`get_weekly_benchmarks_series`, `get_sector_news_series`)는 원래 항상
운영 DB만 조회하도록 작성되어 있었다. dry-run에서는 weekly/benchmark/
sector_news 결과가 DB가 아니라 로컬 파일에만 쌓이므로, 이 조회들이
"이번 시뮬레이션에서 방금 만든 결과"를 못 보고 매번 빈 값으로
판단해 **midterm이 영원히 트리거되지 않는 버그**가 있었다. 이는
`DryRunContext`가 weekly final/midterm/benchmark/sector_news를
메모리에도 함께 보관하고, dry-run일 때 운영 DB 조회 결과와 이 로컬
메모리를 병합해서 보도록 수정해 해결했다(`simulate_range.py`만 수정,
운영 코드인 `database.py`/`tasks.py`는 변경하지 않음). 같은 작업에서
`run()`의 금요일 처리 순서도 운영 스케줄과 동일하게
`weekly-final → sector-news → midterm` 순서로 정정했다.

100종목 × 2026-01-01~02-28(59일) dry-run 결과로 트리거 수정이 정상
동작함을 확인했다(`midterm: ok=4, template=2, skip=14, fail=0`).

---

## 10. Celery 배치 스케줄 (미국 ET 기준, 코드 작성 완료·실가동 미검증)

| 태스크 | 실행 시각 | 설명 |
|--------|-----------|------|
| `daily-closing` | 매일 21:00 ET | 장 마감 후 daily 리포트 |
| `daily-premarket` | 매일 08:00 ET | 장 시작 전 밤사이 업데이트 |
| `weekly-draft` | 매주 월요일 08:00 ET | 주간 초안 |
| `weekly-final` | 매주 금요일 21:00 ET | 주간 최종본 + 가격 벤치마크 |
| `weekly-sector-news` | 매주 금요일 21:30 ET | 섹터별 시장 뉴스 (weekly-final 30분 후) |
| `weekly-midterm` | 매주 금요일 22:00 ET | 중장기 리포트 (sector-news 30분 후) |
| `universe-weekly` | 매주 일요일 02:00 ET | 유니버스 재빌드 |

```bash
# 수동 실행 예시
celery -A app.scheduler.tasks call tasks.daily_digest
celery -A app.scheduler.tasks worker --loglevel=info
celery -A app.scheduler.tasks beat --loglevel=info
```

---

## 11. API 엔드포인트 (작성 완료, 실행/테스트는 미착수)

| Method | URL | 설명 |
|--------|-----|------|
| GET | `/health` | 서버 상태 확인 |
| GET | `/summary/{ticker}?digest_type=daily` | 단일 종목 요약 조회 |
| GET | `/summary/{ticker}/all` | 모든 주기 요약 한번에 |
| GET | `/feed?tickers=AAPL,NVDA&digest_type=daily` | 관심 종목 피드 |
| GET | `/universe/stats` | 현재 유니버스 통계 |
| POST | `/universe/build` | 유니버스 빌드 즉시 트리거 (백그라운드) |

**확인 필요**: `main.py`의 `digest_type` 쿼리 파라미터 validation
패턴이 아직 `^(daily|weekly|monthly|yearly)$`로 남아있다. 실제
`digest_type`은 `daily|weekly|midterm`이므로, 실행 테스트 전에 이
패턴을 갱신해야 한다.

---

## 12. midterm 트리거 규칙 (`should_generate_midterm`)

다음 중 하나라도 만족하면 그 주 금요일에 midterm을 생성한다.

1. 이번 주 weekly final이 없으면 무조건 생성하지 않음(최우선 조건)
2. 직전 주에도 weekly final이 있었으면 생성(연속 2주 누적)
3. 마지막 midterm이 없거나, 마지막 midterm으로부터 42일(6주) 이상
   지났으면 강제 생성(첫 발행 또는 장기 공백 보정)

`MIDTERM_FORCE_INTERVAL_DAYS = 42`. 테스트는
`tests/test_midterm_trigger.py`에서 다양한 weekly final 발생 패턴
(연속/격주/단발 등)으로 검증됨.

---

## 13. 주요 결정사항 및 이유

| 결정 | 이유 |
|------|------|
| Gemini 선택 | Claude보다 저렴, 한국어 품질 충분 |
| daily는 flash-lite, weekly/midterm/sector는 flash | 비용 대비 품질 균형 |
| monthly/yearly 제거 → midterm 도입 | 월간/연간은 실사용 가치가 낮고, 12주 누적 추세가 더 유용 |
| daily/weekly/midterm 구조화 출력(JSON) 전환 | 자유 텍스트 생성 + 정규식 파싱은 형식이 흔들리고 깨지기 쉬움. 헤더/순서는 고정 템플릿, LLM은 내용만 |
| 카테고리 5개로 통합(weekly/midterm 공유) | 호재/악재가 각자 다른 카테고리명을 쓰던 방식은 후속 분석(midterm 추세 집계)을 어렵게 함 |
| 누적 성과 숫자는 템플릿이 직접 삽입 | LLM이 사전 계산된 숫자를 잘못 베껴 쓸 위험 제거 |
| Map-Reduce 제거 | 스니펫이 짧아 1단계 요약으로 충분 |
| 중복 제거를 로컬에서 | DB 부하 없이 깨끗한 데이터만 업로드 |
| Supabase 무료 플랜 | 1년 rolling 유지 시 무료 범위 내 |
| `/stable/news/stock` 사용 | `/api/v3/stock_news`는 현재 플랜에서 403 |
| dry-run 시뮬레이션 프레임워크 도입 | Celery/실DB 없이도 전체 파이프라인 검증 가능 |
| dry-run에서 DryRunContext에 메모리 보관 | 트리거 판단 함수가 항상 운영 DB만 보던 구조라, dry-run 결과를 못 보고 영원히 스킵되는 버그 해결 |

---

## 14. 현재 진행 상태 요약

**완료 + 검증됨**
- [x] 유니버스 빌드, 뉴스 백필/수집(2026-01~06), 중복 제거, DB 업로드
- [x] Supabase 연결, articles/news_summaries/weekly_benchmarks/
      sector_news_summaries 테이블
- [x] daily(closing/premarket), weekly(draft/final), sector news,
      midterm 전체 로직 — dry-run으로 100종목×수개월 분량 동작 검증
- [x] daily/weekly/midterm 구조화 출력(JSON 스키마 + 고정 템플릿) 전환
- [x] dry-run 시뮬레이션 프레임워크 + midterm 트리거 버그 수정

**코드는 있으나 미검증**
- [ ] FastAPI 서버 실제 기동 및 엔드포인트 테스트
- [ ] Celery + Redis로 실제 스케줄 가동 (지금까지는 시뮬레이션 스크립트로만 검증)

**미착수**
- [ ] 4,000종목 전체 유니버스로 실제 운영 전환
- [ ] 클라우드 배포
- [ ] 프론트엔드 (대시보드)
- [ ] 사용자 관심종목 등록/관리 API
- [ ] 요약 품질 A/B 테스트
- [ ] 1년 지난 뉴스 자동 삭제 로직

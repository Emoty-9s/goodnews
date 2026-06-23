# GoodNews AI — 프로젝트 전체 지침

> 이 문서는 새 대화창에서 프로젝트 맥락을 즉시 공유하기 위한 지침서입니다.
> 코드 작업은 Claude Code와 함께, 설계/기획 논의는 Claude와 함께 진행합니다.
> 최종 갱신: 2026-06-23
> (DB 보관 정책 전면 정비 + Supabase 스키마 적용 완료)

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

**주기 구조**: 현재 `digest_type`은 `daily` / `weekly` / `midterm` 세 가지만 유효하다.
`monthly` / `yearly`는 완전 제거됨.

---

## 2. 기술 스택

| 분류 | 기술 |
|------|------|
| 언어 | Python 3.13 |
| 웹 프레임워크 | FastAPI (Railway 배포 완료, /health 응답 확인) |
| 호스팅 | Railway (Hobby 플랜, $5/월 기본 + 사용량) |
| DB | PostgreSQL (Supabase, Seoul 리전) |
| ORM | SQLAlchemy 2.0 (asyncpg) |
| DB 커넥션 풀 | Supabase Supavisor 트랜잭션 모드(포트 6543), pool_size=20, max_overflow=20, statement_cache_size=0, prepared_statement_name_func=uuid4 |
| 뉴스 수집 | FMP API `/stable/news/stock` |
| 일반 시장 뉴스 | FMP API `/stable/news/general-latest` (섹터 리포트용) |
| 가격/벤치마크 | FMP API `/stable/historical-price-eod/light`, `/stable/historical-sector-performance` |
| 종목 유니버스 | FMP API `/stable/company-screener` |
| AI 요약 | Google Gemini — `gemini-2.5-flash`(weekly/midterm/sector), `gemini-2.5-flash-lite`(daily) |
| 구조화 출력 | Gemini `response_schema`(Pydantic) — daily/weekly/midterm 공통 적용 |
| 중복 제거 | Jaccard similarity (단어 집합 기준) |
| 스케줄러 | Celery + Redis (Railway 배포 완료, 실가동 검증은 아직 미착수) |
| 브로커 | Redis (Railway Redis 플러그인, 같은 프로젝트 내 사설 네트워크) |
| HTTP 클라이언트 | httpx (비동기), requests (동기) |
| 로깅 | loguru (콘솔 + 파일 동시 출력) |
| 설정 관리 | pydantic-settings (SettingsConfigDict, extra="ignore") |

---

## 3. 클라우드 인프라 구성 (2026-06-23 현재)

### Railway 프로젝트: "Good News" (production 환경)

| 서비스 이름 | 역할 | 상태 | Start Command |
|---|---|---|---|
| `API_MAIN` | FastAPI 웹 서버 | Online, /health 응답 확인 | `sh -c "uvicorn app.api.main:app --host 0.0.0.0 --port $PORT"` |
| `worker` | Celery 워커 (배치 실행) | Online (실가동 미검증) | `celery -A app.scheduler.tasks worker --loglevel=info` |
| `beat` | Celery 비트 (스케줄러) | Online (실가동 미검증) | `celery -A app.scheduler.tasks beat --loglevel=info` |
| `Redis` | 메시지 브로커 | Online | (Railway 관리형) |

**공개 도메인**: `https://apimain-production-daaf.up.railway.app`
- `API_MAIN`에만 부여. `worker` / `beat`에는 도메인 없음.

**DB**: Railway 외부 — Supabase (Seoul 리전, 포트 6543 트랜잭션 풀러)
- Railway 비용 절감을 위해 Railway 내부 DB를 쓰지 않음.

### 환경변수 (세 서비스 공통)

| 키 | 비고 |
|---|---|
| `FMP_API_KEY` | 직접 입력 |
| `GEMINI_API_KEY` | 직접 입력 |
| `DATABASE_URL` | Supabase 6543 트랜잭션 풀러 주소 |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` (Railway 참조식) |
| `APP_ENV` | `production` |
| `PORT` | `8000` (API_MAIN 전용, uvicorn 포트 고정용) |

---

## 4. 전체 데이터 파이프라인

```
[1단계] 유니버스 빌드 (주 1회, 일요일 02:00 ET)
  FMP /stable/company-screener
  → NASDAQ / NYSE / AMEX 상장, 시총 1억 USD 이상, ETF·워런트·우선주 등 제외
  → 약 4,000개 종목 → data/universe/universe_current.csv

[2단계] 뉴스 수집
  [백필] 최근 12주치 수집 필요 (midterm 생성용)
  [일간] 매일 2회 (closing 21:00 ET, overnight 08:00 ET)

[3단계] 중복 제거
  [일간] DB의 url_hash PK + ON CONFLICT 로 자동 처리

[4단계] AI 요약 생성 — daily (closing → overnight)
[5단계] AI 요약 생성 — weekly (월요일 draft → 금요일 final)
[6단계] AI 요약 생성 — sector news (금요일 21:30 ET)
[7단계] AI 요약 생성 — midterm (금요일 22:00 ET)

[8단계] API 서빙 (FastAPI — Railway 배포 완료)
  GET /health                                      ← 확인 완료
  GET /summary/{ticker}?digest_type=daily|weekly|midterm
  GET /feed?tickers=AAPL,NVDA&digest_type=daily
  GET /summary/{ticker}/all
  GET /universe/stats
  POST /universe/build
```

---

## 5. DB 테이블 구조 및 데이터 보관 정책

### news_summaries (AI 요약 — daily/weekly/midterm 공용)
```sql
ticker            VARCHAR(10)   PK
digest_type       VARCHAR(10)   PK  -- daily | weekly | midterm
report_date       DATE          PK, NULLABLE
version           VARCHAR(10)       -- closing|overnight / draft|final / final
summary_text      TEXT
sentiment         VARCHAR(10)       -- bullish | bearish | mixed | neutral
source_urls       JSONB
price_change_pct  FLOAT             -- weekly/midterm 전용
updated_at        TIMESTAMPTZ
```

**버전 생명주기**:
- `daily/closing` → overnight 생성 시 즉시 삭제 (`delete_closing_for_overnight()`)
- `daily/overnight` → 7일 후 삭제 (`delete_old_daily_reports()`)
- `weekly/draft` → final 생성 시 즉시 삭제 (`delete_draft_for_final()`)
- `weekly/final` → 12주 후 삭제 (`delete_old_weekly_data()`)
- `midterm/final` → 새 버전 생성 시 이전 삭제 (`upsert_midterm()` — ticker당 1개 유지)

### articles (종목 원본 뉴스)
```sql
url_hash     VARCHAR(64)   PK  -- SHA256(url)
title        TEXT
text         TEXT
published_at TIMESTAMPTZ
source       VARCHAR(100)
url          TEXT
tickers      TEXT[]            -- GIN 인덱스
created_at   TIMESTAMPTZ
```
**보관**: 7일 (`delete_old_news_articles()` — 매일 closing 실행 시 자동 삭제)

### market_news_articles (일반 시장 뉴스 — 종목 태그 없음)
```sql
url_hash     VARCHAR(64)   PK
title        TEXT
text         TEXT
url          TEXT
source       VARCHAR(100)
published_at TIMESTAMPTZ
```
**보관**: 7일 (`delete_old_news_articles()` — articles와 함께 처리)

### weekly_benchmarks (S&P500 / 섹터 주간 변동률)
```sql
benchmark_type   VARCHAR(10)   -- sp500 | sector
benchmark_name   VARCHAR(50)
exchange         VARCHAR(10)
week_monday      DATE
change_pct       FLOAT
```
**보관**: 12주 (`delete_old_weekly_data()`)
**용도**: weekly final 가격 첨부 + midterm 12주 벤치마크 시계열 입력

### sector_news_summaries (섹터별 주간 뉴스 요약)
```sql
category       VARCHAR(50)
week_monday    DATE
summary_text   TEXT
sentiment      VARCHAR(10)
```
**보관**: 12주 (`delete_old_weekly_data()`)
**용도**: midterm 생성 시 섹터 맥락 입력 (12주 시계열)

---

## 6. 자동 삭제 함수 전체 목록

| 함수 | 호출 시점 | 삭제 대상 |
|------|----------|----------|
| `delete_old_daily_reports()` | 매일 21:00 closing 실행 시 | daily 7일 초과분 |
| `delete_old_news_articles()` | 매일 21:00 closing 실행 시 | articles + market_news_articles 7일 초과분 |
| `delete_closing_for_overnight()` | overnight 생성 성공 직후 (종목별) | 같은 날짜 closing 1건 |
| `delete_draft_for_final()` | weekly final 생성 성공 직후 (종목별) | 같은 주 draft 1건 |
| `delete_old_weekly_data()` | 매주 월요일 08:00 draft 실행 시 | weekly/benchmarks/sector_news 12주 초과분 |
| `upsert_midterm()` 내부 | midterm 생성 시 (종목별) | 해당 ticker 기존 midterm 전체 |

---

## 7. 프로젝트 파일 구조

```
goodnews/
├── app/
│   ├── api/
│   │   └── main.py              FastAPI 서버 (Railway 배포 완료)
│   ├── core/
│   │   └── config.py            환경변수 (pydantic-settings v2)
│   ├── models/
│   │   └── database.py          SQLAlchemy 모델 + 모든 DB 조회/upsert/삭제 함수
│   ├── scheduler/
│   │   ├── fmp_collector.py     FMP 뉴스 수집
│   │   ├── price_collector.py   가격/섹터 벤치마크 수집
│   │   └── tasks.py             Celery 태스크 + beat_schedule
│   ├── summarizer/
│   │   ├── llm_summarizer.py    Gemini 요약
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
│   ├── backfill_news.py
│   ├── backfill_benchmarks_news.py
│   ├── deduplicate_backfill.py
│   ├── upload_backfill.py           ← 다음 단계: 최근 12주치 데이터 Supabase 업로드
│   ├── retry_failed_tickers.py
│   ├── build_universe_run.py
│   ├── select_sample_100.py
│   ├── simulate_range.py
│   └── analyze_simulation_results.py
├── tests/
│   ├── test_midterm_trigger.py      pytest 23개 전부 PASSED
│   └── test_midterm_structured.py
├── Dockerfile
├── docker-compose.yml               로컬 개발용
├── requirements.txt
├── scripts/init_db.sql              Supabase 적용 완료 (2026-06-23)
├── DEPLOY_RAILWAY.md
└── PROJECT_GUIDE.md                 이 파일
```

---

## 8. Celery 배치 스케줄 (미국 ET 기준)

| 태스크 | 실행 시각 | 설명 |
|--------|-----------|------|
| `daily-closing` | 매일 21:00 ET | 장 마감 후 daily closing 리포트 생성 |
| `daily-premarket` | 매일 08:00 ET | overnight 리포트 생성 (전날 closing → overnight 교체) |
| `weekly-draft` | 매주 월요일 08:00 ET | 주간 초안 생성 |
| `weekly-final` | 매주 금요일 21:00 ET | 주간 최종본 생성 + draft 삭제 + 가격 벤치마크 저장 |
| `weekly-sector-news` | 매주 금요일 21:30 ET | 섹터별 시장 뉴스 요약 생성 |
| `weekly-midterm` | 매주 금요일 22:00 ET | 중장기 리포트 생성 (이전 midterm 삭제 후 새로 저장) |
| `universe-weekly` | 매주 일요일 02:00 ET | 유니버스 재빌드 |

> Railway worker/beat 서비스는 Online 상태이나 **실제 ET 시각 트리거 및
> 장시간 안정성은 아직 검증되지 않음**.

---

## 9. AI 요약 — 구조화 출력 방식

LLM은 Pydantic 스키마(JSON)로 내용 필드만 채우고, 헤더·섹션·불릿 형식은
Python 고정 템플릿 함수(`render_full_report`, `render_weekly_report`,
`render_midterm_report`)가 조립한다.

**LLM 호출 안정성**
- 재시도: 503 발생 시 exponential backoff(5→10→20→40→80초), 최대 5회
- 출력 토큰 한도: `max_output_tokens=16000`
- 입력 글자수 상한: `_trim_to_char_budget(max_chars=12000)`

**모델 분리**
- daily: `gemini-2.5-flash-lite` (비용 절감)
- weekly / midterm / sector: `gemini-2.5-flash`

---

## 10. dry-run 시뮬레이션

```bash
python scripts/simulate_range.py --start 2026-01-01 --end 2026-03-31 \
    --tickers AAPL,NVDA --dry-run --concurrency 10
```

**검증 결과 (100종목 × 2026-01-01~03-31)**
```
daily        : ok=2511, skip=6487, fail=2
weekly_draft : ok=902,  skip=398,  fail=0
weekly_final : ok=968,  skip=331,  fail=1
midterm      : ok=900,  skip=400,  fail=0
```

---

## 11. midterm 트리거 규칙

1. 이번 주 weekly final이 없으면 생성하지 않음 (최우선)
2. 직전 주에도 weekly final이 있었으면 생성 (연속 2주 누적)
3. 마지막 midterm으로부터 42일(6주) 이상 지났으면 강제 생성

`MIDTERM_FORCE_INTERVAL_DAYS = 42`

---

## 12. 주요 결정사항

| 결정 | 이유 |
|------|------|
| Railway 선택 | FastAPI + Celery worker/beat + Redis 멀티 서비스를 한 프로젝트에서 관리 가능, 배포 간단 |
| DB는 Railway 외부(Supabase) 유지 | Railway 내부 DB 추가 시 비용 급증 |
| Gemini 선택 | Claude보다 저렴, 한국어 품질 충분 |
| daily는 flash-lite, weekly/midterm/sector는 flash | 비용 대비 품질 균형 |
| Supabase 트랜잭션 모드(6543) | 세션 모드(5432)는 동시 연결 15개 제한 → 병렬 실행 시 오류 |
| extra="ignore" (config.py) | Railway가 주입하는 RAILWAY_* 등 미정의 env var로 인한 ValidationError 방지 |
| version "premarket" → "overnight" | closing의 연장선임을 명확히 하기 위해 개념적으로 올바른 이름으로 변경 |
| closing/draft 즉시 삭제 | overnight/final 생성 시 이전 버전 즉시 삭제 → DB에 항상 최신본 1개만 유지 |
| midterm ticker당 1개 유지 | 새 버전 생성 시 DELETE + INSERT → API가 항상 최신본 반환 보장 |
| 보관 기간 단축 | articles/daily 7일, weekly/benchmarks/sector 12주 → midterm 생성에 필요한 최소한만 유지 |

---

## 13. 현재 진행 상태 (2026-06-23 기준)

### 완료 + 검증됨
- [x] 유니버스 빌드, 뉴스 수집, 중복 제거
- [x] daily/weekly/midterm/sector_news 전체 파이프라인 로직
- [x] 구조화 출력(JSON 스키마 + 고정 템플릿) 전환
- [x] dry-run 시뮬레이션 (100종목 × 3개월 검증)
- [x] Railway 배포 (API_MAIN /health 응답 확인, worker/beat Online)
- [x] **DB 보관 정책 전면 정비 (2026-06-23)**
  - version `"premarket"` → `"overnight"` 변경
  - overnight 생성 시 closing 즉시 삭제 (`delete_closing_for_overnight`)
  - weekly final 생성 시 draft 즉시 삭제 (`delete_draft_for_final`)
  - midterm DELETE + INSERT 방식으로 교체 (ticker당 항상 1개 유지)
  - weekly/benchmarks/sector_news 보관 기간 52주 → 12주
  - articles/market_news_articles 7일 자동 삭제 추가 (`delete_old_news_articles`)
- [x] **API digest_type 패턴 정비 (2026-06-23)**
  - `monthly|yearly` 완전 제거, `midterm` 추가 (main.py 4곳)
- [x] **Supabase DB 스키마 최신화 (2026-06-23)**
  - `scripts/init_db.sql` 전면 재작성 (5개 테이블, NULLS NOT DISTINCT)
  - Supabase SQL Editor 실행 완료 → 테이블 5개 생성 확인

### 다음 단계
- [ ] **최근 12주치 백필 데이터 Supabase 업로드**
  - `upload_backfill.py` 확인 후 최근 12주 범위만 업로드
  - 업로드 후 `/summary/AAPL?digest_type=daily` 등 실제 API 응답 확인
- [ ] **Celery worker/beat 실가동 검증**
  - ET 시각 트리거 정상 동작 확인
  - 장시간 안정성 검증

### 미착수
- [ ] 4,000종목 전체 유니버스로 실제 운영 전환
- [ ] 프론트엔드 (대시보드)
- [ ] 사용자 관심종목 등록/관리 API
- [ ] 요약 품질 A/B 테스트

---

## 14. 월 예상 비용 (2026-06 기준)

| 항목 | 금액 | 비고 |
|------|------|------|
| Railway (Hobby) | $10~30 | api+worker+beat+Redis, 사용량에 따라 변동 |
| Supabase | $25 | DB, 컴퓨트 크레딧 $10 포함 |
| Gemini API | $5~10 | 100종목 기준 / 4,000종목 전체 시 $150~250 |
| FMP API | 기존 구독 유지 | 배포와 무관한 고정 비용 |
| **합계 (신규 인프라)** | **$40~65** | 100종목 파일럿 기준 |

> Gemini Batch API(50% 할인) 적용 시 전체 운영 비용 약 30~40% 절감 가능.

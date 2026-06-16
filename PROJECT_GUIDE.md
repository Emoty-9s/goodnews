# GoodNews AI — 프로젝트 전체 지침

> 이 문서는 새 대화창에서 프로젝트 맥락을 즉시 공유하기 위한 지침서입니다.
> 코드 작업은 커서 AI와 함께, 설계/기획 논의는 Claude와 함께 진행합니다.

---

## 1. 서비스 개요

**GoodNews AI**는 미국 주식 4,000여 개 종목의 뉴스를 FMP API로 수집하고,
Gemini 2.5 Flash로 AI 요약 리포트를 생성해 개인 투자자에게 제공하는 백엔드 서비스입니다.

**핵심 가치**
- 영문 뉴스를 한국어로 자동 요약
- 호재 / 악재 / 중립 감성 분류
- 일간 / 주간 / 월간 / 연간 4가지 주기 리포트
- 중복 뉴스 자동 제거 (크로스 티커 병합 포함)

**타겟 사용자**: 미국 주식에 투자하지만 모든 영문 뉴스를 팔로우하기 어려운 개인 투자자

---

## 2. 기술 스택

| 분류 | 기술 |
|------|------|
| 언어 | Python 3.13 |
| 웹 프레임워크 | FastAPI |
| DB | PostgreSQL (Supabase 무료 플랜) |
| ORM | SQLAlchemy 2.0 (asyncpg) |
| 뉴스 수집 | FMP API `/stable/news/stock` |
| 종목 유니버스 | FMP API `/stable/company-screener` |
| AI 요약 | Google Gemini 2.5 Flash |
| 중복 제거 | Jaccard similarity (단어 집합 기준) |
| 스케줄러 | Celery + Redis (미구현, 향후 추가) |
| HTTP 클라이언트 | httpx (비동기), requests (동기) |
| 로깅 | loguru |
| 설정 관리 | pydantic-settings |

---

## 3. 전체 데이터 파이프라인

```
[1단계] 유니버스 빌드 (주 1회, 일요일 02:00)
  FMP /stable/company-screener
  → NASDAQ / NYSE / AMEX 상장
  → 시총 1억 USD 이상
  → ETF·펀드·워런트·우선주 등 제외
  → 약 4,000개 종목 → data/universe/universe_current.csv

[2단계] 뉴스 수집
  [백필] 2026-01-01 ~ 현재 (최초 1회)
    FMP /stable/news/stock
    → 월별 × 30개 배치 × 페이지네이션
    → data/backfill/YYYY_MM/TICKER.json

  [일간] 매일 1회
    최근 24시간 뉴스
    → articles 테이블에 URL 해시 기준 중복 체크 후 INSERT

[3단계] 중복 제거
  [백필] 로컬에서 전처리 후 DB 업로드
    ① URL 완전 동일 → 제거
    ② 크로스 티커 중복 → tickers 배열에 모든 종목 태그 병합 (1건으로)
    ③ 동일 티커 내 유사 뉴스 → Jaccard similarity > 0.8 → 본문 긴 것 유지
    → data/clean/YYYY_MM.json → Supabase articles 테이블

  [일간] DB의 url_hash PK로 자동 처리
    ON CONFLICT (url_hash) DO UPDATE SET tickers = tickers || 새티커

[4단계] AI 요약 생성
  articles 테이블에서 ticker별 뉴스 조회
  → 뉴스 건수 판단
      4건 이하 → PROMPT_SIMPLE (오늘 무슨 일 / 요약 / 다음 주목 뉴스)
      5건 이상 → PROMPT_FULL  (핵심 한 줄 / 호재 / 악재 / 온도차 / 주가영향 / 투자자관점)
  → Gemini 2.5 Flash 호출
  → sentiment 판단: "호재 우세" → bullish / "악재 우세" → bearish / 그 외 → neutral
  → news_summaries 테이블 Upsert

[5단계] API 서빙 (FastAPI)
  GET /summary/{ticker}?digest_type=daily
  GET /feed?tickers=AAPL,NVDA&digest_type=daily
  GET /universe/stats
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
- 현재 데이터: 130,037건 (2026-01 ~ 2026-06)
- 1년 rolling: 항상 200MB 이하 유지 예정 (1년 지난 뉴스 자동 삭제)

### news_summaries (AI 요약 저장)
```sql
ticker        VARCHAR(10)  PK
digest_type   VARCHAR(10)  PK  -- daily | weekly | monthly | yearly
summary_text  TEXT             -- 마크다운 형식 한국어 요약
sentiment     VARCHAR(10)      -- bullish | bearish | neutral
source_urls   JSONB            -- 원문 URL 리스트
updated_at    TIMESTAMPTZ
```

---

## 5. 프로젝트 파일 구조

```
goodnews/
├── app/
│   ├── api/
│   │   └── main.py              FastAPI 서버 + 엔드포인트
│   ├── core/
│   │   └── config.py            환경변수 (pydantic-settings)
│   ├── models/
│   │   └── database.py          SQLAlchemy 모델 (Article, NewsSummary)
│   ├── scheduler/
│   │   ├── fmp_collector.py     FMP 뉴스 수집 (비동기 배치)
│   │   └── tasks.py             Celery 태스크 (4가지 주기 + 유니버스 빌드)
│   ├── summarizer/
│   │   ├── llm_summarizer.py    Gemini 요약 (프롬프트 2단계 분기)
│   │   └── deduplicator.py      중복 제거 (Jaccard)
│   └── universe/
│       ├── fmp_client.py        FMP HTTP 클라이언트 (requests, 재시도 포함)
│       ├── fmp_company_screener.py  시총 버킷별 종목 수집
│       ├── fmp_etf_stock_reference.py  ETF 블랙리스트
│       ├── finviz_like_equity_filter.py  보통주 필터 엔진
│       ├── universe_pipeline.py     유니버스 빌드 파이프라인
│       ├── universe_runner.py       파이프라인 Python API 래퍼
│       ├── ticker_store.py          universe_current.csv → list[str]
│       └── universe_save.py         CSV/Parquet 저장
├── scripts/
│   ├── backfill_news.py         백필 수집 (월별 JSON 저장)
│   ├── deduplicate_backfill.py  백필 중복 제거
│   ├── upload_backfill.py       clean JSON → Supabase 업로드
│   ├── build_universe_run.py    유니버스 빌드 실행
│   ├── test_fmp_only.py         FMP API 연결 테스트
│   ├── test_news_fetch.py       30개 티커 뉴스 수집 테스트
│   └── test_summarizer.py       LLM 요약 테스트
├── data/
│   ├── universe/
│   │   └── universe_current.csv  4,004개 종목 (included)
│   ├── backfill/
│   │   └── YYYY_MM/TICKER.json  원본 수집 데이터
│   └── clean/
│       └── YYYY_MM.json         중복 제거 완료 데이터
├── .env                         환경변수 (API 키 등)
├── .cursor/rules                커서 AI 프로젝트 컨텍스트
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## 6. 환경변수 (.env)

```
FMP_API_KEY=...                  FMP Premium Annual Plan
FMP_BASE_URL=https://financialmodelingprep.com/api/v3

GEMINI_API_KEY=...               Google AI Studio
GEMINI_MODEL=gemini-2.5-flash

ANTHROPIC_API_KEY=dummy          미사용 (하위 호환용)

DATABASE_URL=postgresql+asyncpg://postgres.xoorohfdlpzpeoavfybe:...
             @aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres

REDIS_URL=redis://localhost:6379/0   (Celery용, 미구현)

UNIVERSE_DATA_DIR=./data/universe
UNIVERSE_MIN_MARKET_CAP=100000000.0
UNIVERSE_EXCHANGES=NASDAQ,NYSE,AMEX

TICKER_BATCH_SIZE=30
MAX_NEWS_PER_TICKER=50
```

---

## 7. FMP API 엔드포인트 사용 현황

| 엔드포인트 | 용도 | 비고 |
|-----------|------|------|
| `/stable/company-screener` | 종목 유니버스 수집 | 시총 버킷별 호출 |
| `/stable/etf-list` | ETF 블랙리스트 | |
| `/stable/stock-list` | 종목 타입 참조 | |
| `/stable/profile` | 신규 종목 필드 보강 | 선택적 |
| `/stable/news/stock` | 뉴스 수집 | `symbols`, `from`, `to`, `page` 파라미터 |

**주의**: `/api/v3/stock_news`는 현재 플랜(Premium Annual)에서 403 → `/stable/news/stock` 사용

---

## 8. AI 요약 프롬프트 구조

### PROMPT_SIMPLE (뉴스 4건 이하)
```
[오늘 무슨 일]
- 사건1 [호재/악재/중립]
- 사건2 [호재/악재/중립]

[요약]
전체 흐름 2-3문장

[다음에 주목할 뉴스]
- 후속 뉴스 1, 2
```

### PROMPT_FULL (뉴스 5건 이상)
```
[오늘의 핵심 한 줄]
[호재]
[악재 및 우려]
[중립/섹터]
[오늘의 온도차]
[시장 반응 vs 실제 상황]
[주가 영향] 호재 우세 / 악재 우세 / 혼조 / 중립
[투자자 관점] 단기 / 장기
[다음에 주목할 뉴스]
```

**감성 판단 규칙**
> 핵심 기준: "확정 여부"가 아닌 "구체성과 신뢰도"로 판단

- 호재: 주가에 긍정적 영향을 줄 가능성이 높은 뉴스
  - 확정된 긍정 결과 (실적 어닝 비트, 매출 성장)
  - 구체적 수치/일정이 포함된 투자·사업 계획 발표
  - 애널리스트 목표주가 상향 / 투자의견 업그레이드
  - 수익 전망 상향 (가이던스 상향)
  - 구체적 규모가 명시된 계약 또는 파트너십 체결
  - 신제품 출시, FDA 승인 등 구체적 긍정 이벤트

- 악재: 주가에 부정적 영향을 줄 가능성이 높은 뉴스
  - 확정된 부정 결과 (실적 미스, 매출 감소)
  - 규제 조사 착수, 소송 제기, 벌금 부과
  - 애널리스트 목표주가 하향 / 투자의견 다운그레이드
  - 수익 전망 하향 (가이던스 하향)
  - CEO 돌연 사임, 핵심 인력 이탈
  - 리콜, 생산 중단, 제품 결함 확인

- 중립: 영향 불명확하거나 단순 정보성 뉴스
  - 출처 불명확한 루머 또는 단순 추측성 보도
  - 특정 종목 영향이 불명확한 섹터 전반 뉴스
  - 수치/일정 없는 막연한 계획 발표
  - 단순 인사 발표 (맥락 없는 CEO 교체 등)
  - 시장 전반 논평 또는 거시경제 뉴스

---

## 9. 현재 완료된 작업

- [x] FMP API 연결 및 뉴스 수집 파이프라인
- [x] 4,004개 종목 유니버스 빌드 (시총 1억 USD 이상, NASDAQ/NYSE/AMEX)
- [x] 2026년 1월~6월 백필 수집 (원본 162,536건)
- [x] 중복 제거 (130,037건 확정, 20% 제거)
- [x] Supabase PostgreSQL 연결 (무료 플랜, Seoul 리전)
- [x] articles + news_summaries 테이블 생성
- [x] 백필 데이터 DB 업로드 (130,037건)
- [x] Gemini 2.5 Flash 요약 작동 확인 (AAPL 50건 테스트)

## 10. 미완료 / 다음 작업

- [ ] 일간 자동 수집 → DB 저장 파이프라인 연결
- [ ] 요약 생성 → news_summaries 저장 자동화
- [ ] Celery 배치 스케줄러 설정 (Redis 필요)
- [ ] 1년 지난 뉴스 자동 삭제 로직
- [ ] FastAPI 서버 실제 실행 및 엔드포인트 테스트
- [ ] 클라우드 배포 (Railway 또는 Render)
- [ ] 프론트엔드 (Next.js 대시보드)

---

## 11. 주요 결정사항 및 이유

| 결정 | 이유 |
|------|------|
| Gemini 2.5 Flash 선택 | Claude보다 저렴 (약 $18/월 예상), 한국어 품질 충분 |
| Map-Reduce 제거 | 스니펫이 짧아 1단계 요약으로 충분 |
| 중복 제거를 로컬에서 | DB 부하 없이 깨끗한 데이터만 업로드 |
| Supabase 무료 플랜 | 1년 rolling 유지 시 200MB 이하로 무료 범위 내 |
| `/stable/news/stock` | `/api/v3/stock_news`는 현재 플랜에서 403 |
| 크로스 티커 병합 | 같은 뉴스를 여러 종목 리포트에 재사용 가능 |
| Jaccard similarity | sentence-transformers보다 빠르고 가벼움 |
| 페이지네이션 (MAX_PAGES=20) | 월 최대 1,000건 제한으로 무한루프 방지 |
| 감성 판단 기준 | "확정 여부" 아닌 "구체성과 신뢰도" 기준으로 변경 |

---

## 12. 비용 현황

| 항목 | 비용 |
|------|------|
| FMP API | 기존 플랜 사용 중 |
| Gemini 2.5 Flash | 예상 ~$18/월 |
| Supabase | $0 (무료 플랜) |
| 서버 | 미정 (로컬 실행 중) |
| **합계** | **~$18/월** |

-- ============================================================
-- GoodNews AI — DB 초기화 SQL
-- PostgreSQL 15+ / Supabase 호환
-- ============================================================
-- 실행 방법: Supabase SQL Editor 에 전체 붙여넣고 실행
-- 멱등성 보장: IF NOT EXISTS / OR REPLACE 사용 → 재실행 안전
-- ============================================================


-- ────────────────────────────────────────
-- 1. news_summaries
--    AI 요약 리포트 (daily / weekly / midterm 공용)
-- ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS news_summaries (
    ticker            VARCHAR(10)   NOT NULL,
    digest_type       VARCHAR(10)   NOT NULL
                        CHECK (digest_type IN ('daily', 'weekly', 'midterm')),
    report_date       DATE,
    version           VARCHAR(10),
    summary_text      TEXT,
    sentiment         VARCHAR(10)
                        CHECK (sentiment IN ('positive', 'negative', 'mixed', 'neutral')),
    source_urls       JSONB         DEFAULT '[]'::jsonb,
    price_change_pct  FLOAT,
    updated_at        TIMESTAMPTZ   DEFAULT NOW()
);

-- 복합 PK: NULLS NOT DISTINCT 로 report_date=NULL 도 충돌 키 인식 (PostgreSQL 15+)
ALTER TABLE news_summaries
    DROP CONSTRAINT IF EXISTS uq_news_summaries_composite;
ALTER TABLE news_summaries
    ADD CONSTRAINT uq_news_summaries_composite
    UNIQUE NULLS NOT DISTINCT (ticker, digest_type, report_date);

CREATE INDEX IF NOT EXISTS idx_ns_digest_date
    ON news_summaries (digest_type, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_ns_sentiment
    ON news_summaries (sentiment);
CREATE INDEX IF NOT EXISTS idx_ns_updated
    ON news_summaries (updated_at DESC);


-- ────────────────────────────────────────
-- 2. articles
--    종목 태그 원본 뉴스 (7일 보관)
-- ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS articles (
    url_hash     VARCHAR(64)   PRIMARY KEY,
    title        TEXT,
    text         TEXT,
    published_at TIMESTAMPTZ,
    source       VARCHAR(100),
    url          TEXT,
    tickers      TEXT[],
    created_at   TIMESTAMPTZ   DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_articles_published_at
    ON articles (published_at);
CREATE INDEX IF NOT EXISTS ix_articles_tickers_gin
    ON articles USING GIN (tickers);


-- ────────────────────────────────────────
-- 3. market_news_articles
--    일반 시장 뉴스 — 종목 태그 없음 (7일 보관)
-- ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_news_articles (
    url_hash     VARCHAR(64)   PRIMARY KEY,
    title        TEXT,
    text         TEXT,
    url          TEXT,
    source       VARCHAR(100),
    published_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_market_news_published_at
    ON market_news_articles (published_at);


-- ────────────────────────────────────────
-- 4. weekly_benchmarks
--    S&P500 / 섹터별 주간 등락률 (12주 보관)
-- ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS weekly_benchmarks (
    benchmark_type   VARCHAR(10)   NOT NULL
                        CHECK (benchmark_type IN ('sp500', 'sector')),
    benchmark_name   VARCHAR(50)   NOT NULL,
    exchange         VARCHAR(10),
    week_monday      DATE          NOT NULL,
    change_pct       FLOAT
);

ALTER TABLE weekly_benchmarks
    DROP CONSTRAINT IF EXISTS uq_weekly_benchmarks_composite;
ALTER TABLE weekly_benchmarks
    ADD CONSTRAINT uq_weekly_benchmarks_composite
    UNIQUE NULLS NOT DISTINCT (benchmark_type, benchmark_name, exchange, week_monday);

CREATE INDEX IF NOT EXISTS idx_wb_week_monday
    ON weekly_benchmarks (week_monday DESC);


-- ────────────────────────────────────────
-- 5. sector_news_summaries
--    섹터별 주간 뉴스 요약 (12주 보관)
-- ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sector_news_summaries (
    category       VARCHAR(50)   NOT NULL,
    week_monday    DATE          NOT NULL,
    summary_text   TEXT,
    sentiment      VARCHAR(10)
                     CHECK (sentiment IN ('positive', 'negative', 'mixed', 'neutral')),

    CONSTRAINT sector_news_summaries_pkey
        PRIMARY KEY (category, week_monday)
);

CREATE INDEX IF NOT EXISTS idx_sns_week_monday
    ON sector_news_summaries (week_monday DESC);


-- ────────────────────────────────────────
-- 6. macro_indicators
--    거시경제 지표 (주 1회 수집, 최신값 유지)
-- ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS macro_indicators (
    name        VARCHAR(50)   NOT NULL,   -- 'cpi', 'fed_funds_rate', 'nfp' 등
    date        DATE          NOT NULL,   -- 발표일
    value       FLOAT,                   -- 실측값
    previous    FLOAT,                   -- 전월/전분기값
    estimate    FLOAT,                   -- 예상치 (있을 때만)
    unit        VARCHAR(20),             -- '%', 'K', 'index' 등
    CONSTRAINT macro_indicators_pkey PRIMARY KEY (name, date)
);

CREATE INDEX IF NOT EXISTS idx_macro_name_date
    ON macro_indicators (name, date DESC);


-- ────────────────────────────────────────
-- 7. universe_tickers
--    FMP 유니버스 스냅샷 (연 1회 빌드, Supabase가 유일한 source-of-truth)
--    운용: TRUNCATE + INSERT 방식으로 전체 교체. 개별 행 UPDATE는 아래 트리거로 updated_at 자동 갱신.
-- ────────────────────────────────────────

-- updated_at 자동 갱신 트리거 함수 (다른 테이블에도 재사용 가능)
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS universe_tickers (
    symbol               TEXT          PRIMARY KEY,
    company_name         TEXT,
    exchange             TEXT,
    exchange_short_name  TEXT,
    country              TEXT,
    currency             TEXT,
    sector               TEXT,
    industry             TEXT,
    market_cap           FLOAT,
    price                FLOAT,
    beta                 FLOAT,
    volume               FLOAT,
    is_actively_trading  BOOLEAN,
    universe_status      TEXT
                           CHECK (universe_status IN ('included', 'excluded')),
    snapshot_date        DATE,
    created_at_utc       TIMESTAMPTZ   DEFAULT NOW(),
    updated_at           TIMESTAMPTZ   DEFAULT NOW()
);

-- symbol 은 이미 PK이나 명시적 UNIQUE INDEX 추가 (ORM 호환 + 문서화 목적)
CREATE UNIQUE INDEX IF NOT EXISTS uix_universe_tickers_symbol
    ON universe_tickers (symbol);

CREATE INDEX IF NOT EXISTS idx_universe_tickers_status
    ON universe_tickers (universe_status);

-- 개별 UPDATE 시 updated_at 자동 갱신
DROP TRIGGER IF EXISTS trg_universe_tickers_updated_at ON universe_tickers;
CREATE TRIGGER trg_universe_tickers_updated_at
    BEFORE UPDATE ON universe_tickers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ────────────────────────────────────────
-- 8. fetch_failures
--    FMP 뉴스 수집 실패(429 등, 재시도 소진) 기록 — 검증/재실행 안전망
--    digest_type 컬럼은 daily 외 weekly/midterm 향후 확장 대비 미리 둠
-- ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fetch_failures (
    id             SERIAL        PRIMARY KEY,
    ticker         VARCHAR(20)   NOT NULL,
    digest_type    VARCHAR(20)   NOT NULL,
    report_date    DATE          NOT NULL,
    attempt_count  INT           NOT NULL DEFAULT 1,
    last_error     TEXT,
    resolved_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (ticker, digest_type, report_date)
);

CREATE INDEX IF NOT EXISTS idx_fetch_failures_unresolved
    ON fetch_failures (digest_type, report_date)
    WHERE resolved_at IS NULL;


-- ============================================================
-- 보관 정책 요약 (코드 자동 삭제 기준)
-- ============================================================
-- news_summaries / daily    → 7일    delete_old_daily_reports()
-- news_summaries / weekly   → 12주   delete_old_weekly_data()
-- news_summaries / midterm  → 새 버전 생성 시 이전 삭제 (upsert_midterm)
-- articles                  → 7일    delete_old_news_articles()
-- market_news_articles      → 7일    delete_old_news_articles()
-- weekly_benchmarks         → 12주   delete_old_weekly_data()
-- sector_news_summaries     → 12주   delete_old_weekly_data()
-- macro_indicators          → 6개월  delete_old_macro_indicators()
-- ============================================================

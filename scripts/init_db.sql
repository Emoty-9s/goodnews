-- GoodNews AI - DB 초기화 SQL
-- PostgreSQL 15+ / Supabase 호환

-- 뉴스 요약 테이블 (핵심)
CREATE TABLE IF NOT EXISTS news_summaries (
    ticker          VARCHAR(10)  NOT NULL,
    digest_type     VARCHAR(10)  NOT NULL CHECK (digest_type IN ('daily','weekly','monthly','yearly')),
    summary_text    TEXT,
    sentiment       VARCHAR(10)  CHECK (sentiment IN ('bullish','bearish','neutral')),
    source_urls     JSONB        DEFAULT '[]'::jsonb,
    updated_at      TIMESTAMPTZ  DEFAULT NOW(),

    PRIMARY KEY (ticker, digest_type)
);

-- 인덱스: sentiment 기반 필터링용
CREATE INDEX IF NOT EXISTS idx_summaries_sentiment
    ON news_summaries (sentiment);

-- 인덱스: 최신 업데이트 기준 정렬용
CREATE INDEX IF NOT EXISTS idx_summaries_updated
    ON news_summaries (updated_at DESC);

-- 사용자 관심 종목 테이블 (선택 사항)
CREATE TABLE IF NOT EXISTS user_watchlists (
    id          SERIAL       PRIMARY KEY,
    user_id     VARCHAR(100) NOT NULL,
    ticker      VARCHAR(10)  NOT NULL,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),

    UNIQUE (user_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_user
    ON user_watchlists (user_id);

-- 샘플 데이터 (테스트용)
INSERT INTO news_summaries (ticker, digest_type, summary_text, sentiment, source_urls)
VALUES (
    'AAPL', 'daily',
    '## AAPL 일간 요약\n\n**[호재]** Apple이 Q4 실적에서 매출 $94.9B(YoY +6%), EPS $1.64(예상치 상회)를 기록하며 강세를 보였습니다.\n\n**[중립]** Vision Pro 판매량에 대한 애널리스트 의견이 엇갈리고 있습니다.',
    'bullish',
    '["https://example.com/apple-earnings"]'
)
ON CONFLICT (ticker, digest_type) DO NOTHING;

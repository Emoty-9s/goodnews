-- migrate_sentiment.sql
-- sentiment 값 체계 변환: bullish/bearish → positive/negative
-- 실행 전 백업 권장: pg_dump goodnews > backup_before_migrate.sql

-- 기존 데이터 값 변환
UPDATE news_summaries SET sentiment = 'positive' WHERE sentiment = 'bullish';
UPDATE news_summaries SET sentiment = 'negative' WHERE sentiment = 'bearish';

-- CHECK 제약 재설정
ALTER TABLE news_summaries DROP CONSTRAINT IF EXISTS news_summaries_sentiment_check;
ALTER TABLE news_summaries ADD CONSTRAINT news_summaries_sentiment_check
    CHECK (sentiment IN ('positive', 'negative', 'mixed', 'neutral'));

-- sector_news_summaries도 동일하게 처리
UPDATE sector_news_summaries SET sentiment = 'positive' WHERE sentiment = 'bullish';
UPDATE sector_news_summaries SET sentiment = 'negative' WHERE sentiment = 'bearish';

ALTER TABLE sector_news_summaries DROP CONSTRAINT IF EXISTS sector_news_summaries_sentiment_check;
ALTER TABLE sector_news_summaries ADD CONSTRAINT sector_news_summaries_sentiment_check
    CHECK (sentiment IN ('positive', 'negative', 'mixed', 'neutral'));

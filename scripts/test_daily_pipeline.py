#!/usr/bin/env python3
"""
Celery 없이 일간 파이프라인(Phase1 closing → Phase2 premarket)을 직접 실행하는 시범 스크립트.

AAPL, NKE, BAC 3개 종목만 처리하고, 실행 후 DB 결과를 검증한다.
실행: python scripts/test_daily_pipeline.py
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from sqlalchemy import text

from app.models.database import AsyncSessionLocal
from app.scheduler.tasks import run_daily_closing, run_daily_premarket

TEST_TICKERS = ["AAPL", "NKE", "BAC"]
ET = ZoneInfo("America/New_York")


async def verify():
    today_et = datetime.now(ET).date()
    yesterday_et = today_et - timedelta(days=1)

    print("\n========== 검증 ==========")
    async with AsyncSessionLocal() as session:
        # 1) articles 에 최근 24시간 뉴스가 들어갔는지
        since = datetime.now(ET) - timedelta(hours=24)
        r = await session.execute(
            text(
                "SELECT count(*) FROM articles "
                "WHERE (:t1 = ANY(tickers) OR :t2 = ANY(tickers) OR :t3 = ANY(tickers)) "
                "AND published_at >= :since"
            ),
            {"t1": "AAPL", "t2": "NKE", "t3": "BAC", "since": since},
        )
        art_count = r.scalar()
        print(f"[1] articles (테스트 종목, 최근24h): {art_count}건")

        # 2) closing 리포트 (report_date=오늘)
        r = await session.execute(
            text(
                "SELECT ticker, version, report_date, sentiment, length(summary_text) AS len "
                "FROM news_summaries "
                "WHERE digest_type='daily' AND version='closing' AND report_date=:d "
                "AND ticker = ANY(:tks) ORDER BY ticker"
            ),
            {"d": today_et, "tks": TEST_TICKERS},
        )
        closing_rows = r.mappings().all()
        print(f"\n[2] closing 리포트 (report_date={today_et}): {len(closing_rows)}건")
        for row in closing_rows:
            print(f"    {row['ticker']:5} | {row['version']:9} | {row['sentiment']:8} | {row['len']}자")

        # 3) premarket 리포트 (report_date=어제, closing 이 premarket 으로 갱신됨)
        r = await session.execute(
            text(
                "SELECT ticker, version, report_date, sentiment, length(summary_text) AS len "
                "FROM news_summaries "
                "WHERE digest_type='daily' AND version='premarket' AND report_date=:d "
                "AND ticker = ANY(:tks) ORDER BY ticker"
            ),
            {"d": yesterday_et, "tks": TEST_TICKERS},
        )
        pre_rows = r.mappings().all()
        print(f"\n[3] premarket 리포트 (report_date={yesterday_et}): {len(pre_rows)}건")
        for row in pre_rows:
            print(f"    {row['ticker']:5} | {row['version']:9} | {row['sentiment']:8} | {row['len']}자")


async def main():
    print("=== Phase1: closing 실행 ===")
    await run_daily_closing(test_tickers=TEST_TICKERS)

    print("\n=== Phase2: premarket 실행 (실제 밤사이 윈도우) ===")
    await run_daily_premarket(test_tickers=TEST_TICKERS)

    # 데모: 새벽 시간대라 밤사이 윈도우가 비어있을 수 있어,
    # 최근 24시간 윈도우로 premarket 생성 경로를 강제 시연한다.
    print("\n=== Phase2(데모): premarket 강제 실행 (최근 24h 윈도우) ===")
    demo_since = datetime.now(ET) - timedelta(hours=24)
    await run_daily_premarket(test_tickers=TEST_TICKERS, since_override=demo_since)

    await verify()


if __name__ == "__main__":
    asyncio.run(main())

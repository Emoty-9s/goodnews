#!/usr/bin/env python3
"""
빠른 동작 테스트 스크립트
사용법: python scripts/test_pipeline.py

FMP API → 뉴스 수집 → LLM 요약 전체 파이프라인을 AAPL 1개로 빠르게 검증.
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from app.scheduler.fmp_collector import FMPNewsCollector, get_since_datetime
from app.summarizer.llm_summarizer import summarize_ticker


async def test_fmp_api():
    """FMP API 연결 테스트"""
    print("\n[1] FMP API 뉴스 수집 테스트 (AAPL, daily)")
    collector = FMPNewsCollector()
    from datetime import timezone
    since = get_since_datetime("daily")

    news_by_ticker = await collector.fetch_all(
        all_tickers=["AAPL", "NVDA"],
        since=since,
        limit_per_batch=10,
    )

    for ticker, news_list in news_by_ticker.items():
        print(f"  ✓ {ticker}: {len(news_list)}건 수집")
        if news_list:
            print(f"    최신 뉴스: {news_list[0].get('title', '')[:60]}...")

    return news_by_ticker


def test_llm_summary(news_by_ticker: dict):
    """LLM 요약 테스트"""
    print("\n[2] LLM Map-Reduce 요약 테스트 (AAPL, daily)")

    ticker = "AAPL"
    news_list = news_by_ticker.get(ticker, [])

    if not news_list:
        print("  ⚠ 뉴스 없음 - 더미 데이터로 테스트")
        news_list = [
            {
                "title": "Apple Reports Q4 2024 Earnings: Revenue $94.9B, EPS $1.64",
                "text": "Apple Inc. reported fourth quarter fiscal year 2024 results with revenue of $94.9 billion, up 6% year over year. EPS came in at $1.64, beating analyst estimates of $1.60.",
                "url": "https://example.com/apple-earnings"
            }
        ]

    result = summarize_ticker(ticker, news_list, "daily")

    print(f"  ✓ 티커: {result['ticker']}")
    print(f"  ✓ 감성: {result['sentiment']}")
    print(f"  ✓ 요약 (앞 200자):\n{result['summary_text'][:200]}...")
    return result


async def main():
    print("=" * 50)
    print("GoodNews AI - 파이프라인 테스트")
    print("=" * 50)

    try:
        news_by_ticker = await test_fmp_api()
    except Exception as e:
        print(f"  ✗ FMP API 오류: {e}")
        news_by_ticker = {}

    try:
        test_llm_summary(news_by_ticker)
    except Exception as e:
        print(f"  ✗ LLM 요약 오류: {e}")

    print("\n[완료] 테스트 종료")


if __name__ == "__main__":
    asyncio.run(main())

"""
백필용 historical 데이터 수집:
A) S&P500 + 섹터(33종) 주간 벤치마크 → weekly_benchmarks
B) 일반 시장뉴스 → market_news_articles
C) 섹터 주간뉴스 12카테고리 → sector_news_summaries

23개 주차: 2026-01-05 ~ 2026-06-08 (월요일 기준)
이미 저장된 주차는 스킵 (재실행 가능)
"""

import asyncio
import sys
from datetime import date, timedelta

from loguru import logger
from sqlalchemy import text

from app.models.database import (
    AsyncSessionLocal,
    get_market_news_for_week,
    get_weekly_benchmarks,
    insert_market_news,
    upsert_sector_news,
    upsert_weekly_benchmark,
)
from app.scheduler.fmp_collector import fetch_general_news
from app.scheduler.price_collector import (
    fetch_sector_weekly_changes,
    fetch_sp500_weekly_change,
)
from app.summarizer.llm_summarizer import summarize_sector_news

# ──────────────────────────────────────────
# 대상 주차 (월요일 리스트)
# ──────────────────────────────────────────
ALL_WEEKS = [date(2026, 1, 5) + timedelta(weeks=i) for i in range(23)]
# 검증: ALL_WEEKS[-1] == date(2026, 6, 8)

# 실행 인자로 주차 수 지정 가능: python -m scripts.backfill_benchmarks_news 2
_n = int(sys.argv[1]) if len(sys.argv) > 1 else len(ALL_WEEKS)
WEEK_MONDAYS = ALL_WEEKS[:_n]


# ──────────────────────────────────────────
# Phase A: 가격/벤치마크
# ──────────────────────────────────────────

async def run_phase_a():
    logger.info(f"===== Phase A: 가격/벤치마크 ({len(WEEK_MONDAYS)}주차) =====")
    for week_monday in WEEK_MONDAYS:
        week_friday = week_monday + timedelta(days=4)

        existing = await get_weekly_benchmarks(week_monday)
        if existing["sp500"] is not None:
            logger.info(f"[A] {week_monday} 이미 존재, 스킵")
            continue

        sp500 = await fetch_sp500_weekly_change(week_monday, week_friday)
        await upsert_weekly_benchmark("sp500", "SP500", None, week_monday, sp500)

        sectors = await fetch_sector_weekly_changes(week_monday, week_friday)
        for (sector, exchange), pct in sectors.items():
            await upsert_weekly_benchmark("sector", sector, exchange, week_monday, pct)

        logger.info(
            f"[A] {week_monday} 완료 — sp500={sp500:.2f}%, 섹터 {len(sectors)}종"
        )


# ──────────────────────────────────────────
# Phase B: 일반 시장뉴스
# ──────────────────────────────────────────

async def run_phase_b():
    logger.info(f"===== Phase B: 일반 시장뉴스 ({len(WEEK_MONDAYS)}주차) =====")
    for week_monday in WEEK_MONDAYS:
        week_friday = week_monday + timedelta(days=4)

        existing = await get_market_news_for_week(week_monday, week_friday)
        if existing:
            logger.info(f"[B] {week_monday} 이미 존재 ({len(existing)}건), 스킵")
            continue

        articles = await fetch_general_news(
            from_date=week_monday.isoformat(),
            to_date=week_friday.isoformat(),
        )
        inserted = await insert_market_news(articles)
        logger.info(
            f"[B] {week_monday} 완료 — 수집 {len(articles)}건, INSERT {inserted}건"
        )


# ──────────────────────────────────────────
# Phase C: 섹터 주간뉴스 생성 (Flash, 23회)
# ──────────────────────────────────────────

async def _sector_news_exists(week_monday: date) -> bool:
    """sector_news_summaries에 해당 주차 데이터가 하나라도 있으면 True."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT 1 FROM sector_news_summaries "
                "WHERE week_monday = :wm LIMIT 1"
            ),
            {"wm": week_monday},
        )
        return result.first() is not None


async def run_phase_c():
    logger.info(f"===== Phase C: 섹터뉴스 생성 ({len(WEEK_MONDAYS)}주차) =====")
    for week_monday in WEEK_MONDAYS:
        week_friday = week_monday + timedelta(days=4)

        if await _sector_news_exists(week_monday):
            logger.info(f"[C] {week_monday} 이미 존재, 스킵")
            continue

        articles = await get_market_news_for_week(week_monday, week_friday)
        if not articles:
            logger.warning(f"[C] {week_monday} 뉴스 없음, 스킵")
            continue

        result = summarize_sector_news(articles)
        if result is None:
            logger.warning(f"[C] {week_monday} 생성 실패")
            continue

        for category, data in result.items():
            await upsert_sector_news(
                category=category,
                week_monday=week_monday,
                summary_text=data["summary_text"],
                sentiment=data["sentiment"],
            )
        logger.info(f"[C] {week_monday} 완료 — {len(result)}개 카테고리")


# ──────────────────────────────────────────
# 엔트리포인트
# ──────────────────────────────────────────

async def main():
    logger.info(f"백필 대상: {WEEK_MONDAYS[0]} ~ {WEEK_MONDAYS[-1]} ({len(WEEK_MONDAYS)}주)")
    await run_phase_a()
    await run_phase_b()
    await run_phase_c()
    logger.info("===== 백필 완료 =====")


asyncio.run(main())

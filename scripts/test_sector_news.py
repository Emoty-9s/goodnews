#!/usr/bin/env python3
"""
일반 시장 뉴스 → 섹터별 주간 리포트 파이프라인 시범 실행.

이번 주(월~금) = 2026-06-08~06-12 (데이터는 06-08~06-11 존재).
실행: python scripts/test_sector_news.py
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from sqlalchemy import text

from app.models.database import AsyncSessionLocal
from app.scheduler.tasks import run_weekly_sector_news, _week_monday

ET = ZoneInfo("America/New_York")


async def show_results():
    week_monday = _week_monday(datetime.now(ET).date())
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text(
                "SELECT category, sentiment, length(summary_text) AS len, summary_text "
                "FROM sector_news_summaries WHERE week_monday = :d "
                "ORDER BY category"
            ),
            {"d": week_monday},
        )
        rows = r.mappings().all()

    print(f"\n========== 결과 (week_monday={week_monday}) ==========")
    print(f"생성된 카테고리: {len(rows)} / 12개")
    for row in rows:
        print(f"  {row['category']:22} | {row['sentiment']:8} | {row['len']}자")

    print("\n----- 샘플 (최대 2개) -----")
    for row in rows[:2]:
        print("\n" + "=" * 60)
        print(row["summary_text"][:900])


async def main():
    print("=== run_weekly_sector_news(test=True) 실행 ===")
    await run_weekly_sector_news(test=True)
    await show_results()


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
Celery 없이 주간 파이프라인(draft → final)을 직접 실행하는 시범 스크립트.

AAPL, NKE, BAC 3개 종목만 처리하고, 실행 후 DB 결과를 검증한다.
실행: python scripts/test_weekly_pipeline.py
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

# Windows 콘솔(cp949) 출력 깨짐 방지
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from sqlalchemy import text

from app.models.database import AsyncSessionLocal
from app.scheduler.tasks import run_weekly_draft, run_weekly_final, _week_monday

TEST_TICKERS = ["AAPL", "NKE", "BAC"]
ET = ZoneInfo("America/New_York")


def _fmt_pct(v):
    return f"{v:+.2f}%" if v is not None else "  N/A"


async def show_weekly(label: str, version: str):
    week_monday = _week_monday(datetime.now(ET).date())
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text(
                "SELECT ticker, version, report_date, sentiment, price_change_pct, "
                "       length(summary_text) AS len "
                "FROM news_summaries "
                "WHERE digest_type='weekly' AND version=:v AND report_date=:d "
                "AND ticker = ANY(:tks) ORDER BY ticker"
            ),
            {"v": version, "d": week_monday, "tks": TEST_TICKERS},
        )
        rows = r.mappings().all()
    print(f"\n[{label}] weekly/{version} (report_date={week_monday}): {len(rows)}건")
    for row in rows:
        print(
            f"    {row['ticker']:5} | {row['version']:6} | "
            f"{row['sentiment']:8} | 변동 {_fmt_pct(row['price_change_pct'])} | "
            f"{row['len']}자"
        )


async def show_benchmarks():
    """S&P500 + 테스트 종목 섹터(거래소별) 벤치마크 출력."""
    week_monday = _week_monday(datetime.now(ET).date())
    async with AsyncSessionLocal() as session:
        sp = await session.execute(
            text(
                "SELECT change_pct FROM weekly_benchmarks "
                "WHERE benchmark_type='sp500' AND week_monday=:d"
            ),
            {"d": week_monday},
        )
        sp_row = sp.first()

        sec = await session.execute(
            text(
                "SELECT benchmark_name, exchange, change_pct FROM weekly_benchmarks "
                "WHERE benchmark_type='sector' AND week_monday=:d "
                "AND benchmark_name = ANY(:sectors) "
                "ORDER BY benchmark_name, exchange"
            ),
            {
                "d": week_monday,
                "sectors": ["Technology", "Consumer Cyclical", "Financial Services"],
            },
        )
        sec_rows = sec.mappings().all()

        cnt = await session.execute(
            text(
                "SELECT count(*) FROM weekly_benchmarks "
                "WHERE benchmark_type='sector' AND week_monday=:d"
            ),
            {"d": week_monday},
        )
        total_sectors = cnt.scalar()

    print(f"\n[벤치마크 검증] (week_monday={week_monday})")
    print(f"    S&P500: {_fmt_pct(sp_row[0] if sp_row else None)}")
    print(f"    섹터 벤치마크 총 {total_sectors}건 저장 (11섹터 × 3거래소=33 기대)")
    print("    테스트 종목 관련 섹터:")
    for row in sec_rows:
        print(
            f"      {row['benchmark_name']:20} | {row['exchange']:6} | "
            f"{_fmt_pct(row['change_pct'])}"
        )


async def main():
    print("=== Weekly Phase1: draft 실행 ===")
    await run_weekly_draft(test_tickers=TEST_TICKERS)
    await show_weekly("Phase1 검증", "draft")

    print("\n=== Weekly Phase2: final 실행 (가격 벤치마크 포함) ===")
    await run_weekly_final(test_tickers=TEST_TICKERS)
    await show_weekly("Phase2 검증", "final")
    await show_benchmarks()

    print(
        "\n참고: draft 와 final 은 동일 키(ticker, weekly, 월요일)라 "
        "final 실행 시 같은 행이 final 로 갱신됩니다 (이후 draft 조회 0건이 정상)."
    )


if __name__ == "__main__":
    asyncio.run(main())

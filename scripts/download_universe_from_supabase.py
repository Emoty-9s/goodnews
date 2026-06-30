#!/usr/bin/env python3
"""
scripts/download_universe_from_supabase.py

Supabase universe_tickers 테이블 전체를 읽어 로컬 universe_current.csv 로 저장.
로컬 디버깅·검토용. 운영 source-of-truth는 Supabase DB.

Usage:
    python scripts/download_universe_from_supabase.py
    python scripts/download_universe_from_supabase.py --data-dir ./data/universe
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.models.database import AsyncSessionLocal

DEFAULT_DATA_DIR = ROOT / "data" / "universe"


async def download_universe(data_dir: Path) -> int:
    """
    universe_tickers 전체 조회 → CSV 저장.
    반환값: 저장된 행 수.
    """
    logger.info("[download] Supabase universe_tickers 전체 조회 중…")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT symbol, company_name, exchange, exchange_short_name, "
                "       country, currency, sector, industry, "
                "       market_cap, price, beta, volume, "
                "       is_actively_trading, universe_status, "
                "       snapshot_date, created_at_utc, updated_at "
                "FROM universe_tickers "
                "ORDER BY symbol"
            )
        )
        rows = result.mappings().all()

    if not rows:
        logger.warning("[download] universe_tickers 테이블이 비어 있습니다.")
        return 0

    df = pd.DataFrame([dict(r) for r in rows])
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "universe_current.csv"
    df.to_csv(out_path, index=False)
    logger.info("[download] %d개 종목 → %s", len(df), out_path.resolve())
    return len(df)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Supabase universe_tickers → 로컬 universe_current.csv 다운로드"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"저장 디렉터리 (기본: {DEFAULT_DATA_DIR})",
    )
    args = parser.parse_args()

    count = await download_universe(args.data_dir)
    if count:
        logger.info("[download] 완료: %d개 종목 저장", count)
    else:
        logger.warning("[download] 저장된 종목 없음")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

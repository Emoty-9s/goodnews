#!/usr/bin/env python3
"""
scripts/upload_universe_to_supabase.py

로컬 universe_current.csv 를 읽어 Supabase universe_tickers 테이블에 upsert.
build_universe.py --upload-to-supabase 와 달리, CSV가 이미 있을 때 단독 실행용.

Usage:
    python scripts/upload_universe_to_supabase.py
    python scripts/upload_universe_to_supabase.py --data-dir ./data/universe
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

from app.models.database import upsert_universe_tickers

DEFAULT_DATA_DIR = ROOT / "data" / "universe"


async def upload_universe(data_dir: Path) -> int:
    """
    universe_current.csv 를 읽어 Supabase에 upsert.
    반환값: upsert된 행 수.
    """
    csv_path = data_dir / "universe_current.csv"
    if not csv_path.exists():
        logger.error("[upload] CSV 없음: %s", csv_path.resolve())
        return 0

    logger.info("[upload] CSV 로드: %s", csv_path.resolve())
    df = pd.read_csv(csv_path, low_memory=False)
    if df.empty:
        logger.warning("[upload] CSV가 비어 있습니다.")
        return 0

    logger.info("[upload] %d개 행 → Supabase universe_tickers upsert 중…", len(df))
    rows = df.to_dict(orient="records")
    count = await upsert_universe_tickers(rows)
    logger.info("[upload] 완료: %d개 종목 upsert", count)
    return count


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="로컬 universe_current.csv → Supabase universe_tickers 업로드"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"CSV 위치 디렉터리 (기본: {DEFAULT_DATA_DIR})",
    )
    args = parser.parse_args()

    count = await upload_universe(args.data_dir)
    if not count:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

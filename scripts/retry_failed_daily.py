#!/usr/bin/env python3
"""
scripts/retry_failed_daily.py

daily-closing 뉴스 수집 실패(429 등, fetch_failures 미해결) 티커를 수동으로 재시도한다.
Celery 없이 로컬/스테이징에서 즉시 실행할 때 사용.

Usage:
    python -m scripts.retry_failed_daily
    python -m scripts.retry_failed_daily --date 2026-07-01
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.scheduler.tasks import retry_failed_daily


def main() -> None:
    parser = argparse.ArgumentParser(
        description="daily-closing 뉴스 수집 실패 티커 수동 재시도"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="대상 report_date (YYYY-MM-DD). 생략 시 오늘",
    )
    args = parser.parse_args()

    report_date = date.fromisoformat(args.date) if args.date else date.today()
    print(f"[RETRY-FAILED-DAILY] {report_date} 미해결 실패 재시도 시작 (pass 1)")
    asyncio.run(retry_failed_daily(report_date, pass_num=1))
    print("[RETRY-FAILED-DAILY] 완료")


if __name__ == "__main__":
    main()

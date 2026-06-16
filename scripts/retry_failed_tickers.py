#!/usr/bin/env python3
"""
backfill_stats.json 의 errors 에 기록된 실패 종목만 재수집.

에러 형식: "[2026_01] NKE: HTTPStatusError: ..."
→ 월(2026_01)과 티커(NKE)를 파싱해 해당 종목만 fetch_ticker 로 다시 받는다.

실행: python scripts/retry_failed_tickers.py
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
from loguru import logger

ROOT = Path(__file__).parent.parent.resolve()
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from backfill_news import (
    BACKFILL_DIR,
    CONCURRENCY,
    build_month_ranges,
    fetch_ticker,
    normalize_article,
    ticker_file_path,
)

STATS_PATH = BACKFILL_DIR / "backfill_stats.json"
ERROR_PATTERN = re.compile(r"\[(\d{4}_\d{2})\]\s+([A-Za-z0-9.\-]+):")


def parse_failures(errors: list[str]) -> list[tuple[str, str]]:
    """errors 문자열 리스트에서 (month_key, ticker) 추출 (중복 제거)."""
    failures = []
    seen = set()
    for line in errors:
        m = ERROR_PATTERN.search(line)
        if not m:
            logger.warning(f"파싱 불가 (건너뜀): {line}")
            continue
        key = (m.group(1), m.group(2).upper())
        if key not in seen:
            seen.add(key)
            failures.append(key)
    return failures


async def run():
    if not STATS_PATH.exists():
        logger.error(f"통계 파일이 없습니다: {STATS_PATH}")
        sys.exit(1)

    with open(STATS_PATH, "r", encoding="utf-8") as f:
        stats = json.load(f)

    errors = stats.get("errors", [])
    if not errors:
        logger.info("errors 가 비어 있습니다. 재수집할 종목이 없습니다.")
        return

    failures = parse_failures(errors)
    if not failures:
        logger.error("errors 에서 (월, 티커) 를 파싱하지 못했습니다.")
        sys.exit(1)

    # 월별 from/to 범위 매핑
    month_map = {mk: (fd, td) for mk, fd, td in build_month_ranges()}

    logger.info(f"재수집 대상: {len(failures)}건 (월/티커)")

    sem = asyncio.Semaphore(CONCURRENCY)
    still_failed: list[str] = []
    recovered = 0
    recovered_articles = 0

    async with httpx.AsyncClient() as client:
        tasks = []
        meta = []
        for month_key, ticker in failures:
            if month_key not in month_map:
                msg = f"[{month_key}] {ticker}: 알 수 없는 월 (월 범위에 없음)"
                logger.error(msg)
                still_failed.append(msg)
                continue
            from_date, to_date = month_map[month_key]
            tasks.append(fetch_ticker(client, sem, ticker, from_date, to_date))
            meta.append(month_key)

        results = await asyncio.gather(*tasks)

    for month_key, (ticker, articles, err) in zip(meta, results):
        if err is not None:
            logger.error(f"[{month_key}] 재시도 실패: {err}")
            still_failed.append(f"[{month_key}] {err}")
            continue

        (BACKFILL_DIR / month_key).mkdir(parents=True, exist_ok=True)
        normalized = [normalize_article(item, ticker) for item in articles]
        with open(ticker_file_path(month_key, ticker), "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)

        recovered += 1
        recovered_articles += len(normalized)
        logger.info(f"[{month_key}] {ticker} 재수집 완료 ({len(normalized)}건)")

    # 통계의 errors 를 남은 실패만으로 갱신
    stats["errors"] = still_failed
    with open(STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    logger.info(
        f"재수집 완료: {recovered}/{len(failures)}개 종목 성공, "
        f"총 {recovered_articles:,}건 / 남은 실패 {len(still_failed)}건"
    )
    if still_failed:
        logger.info("남은 실패는 backfill_stats.json errors 에 갱신됨 → 다시 실행 가능")


if __name__ == "__main__":
    asyncio.run(run())

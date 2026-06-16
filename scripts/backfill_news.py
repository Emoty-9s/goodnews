#!/usr/bin/env python3
"""
FMP /stable/news/stock 백필 수집기.

2026-01-01 ~ 오늘까지, universe_current.csv 의 included 종목 뉴스를
월별 폴더(data/backfill/YYYY_MM/TICKER.json)로 저장한다.

- 티커 1개씩 개별 요청 (배치 요청 시 limit 이 전체에 적용돼 종목이 누락됨)
- Semaphore(5) 로 동시 5개씩 처리, 요청 간 0.3초 sleep
- 티커별 페이지네이션으로 월 전체 뉴스 수집
- 실행: python scripts/backfill_news.py
"""
import asyncio
import calendar
import json
import sys
from datetime import date, datetime
from pathlib import Path

import httpx
from loguru import logger

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.core.config import get_settings
from app.universe.ticker_store import load_tickers_from_csv

settings = get_settings()

# ── 설정 ──────────────────────────────────────────
BACKFILL_DIR = ROOT / "data" / "backfill"
UNIVERSE_CSV = ROOT / "data" / "universe" / "universe_current.csv"
NEWS_ENDPOINT = "https://financialmodelingprep.com/stable/news/stock"

START_YEAR = 2026
START_MONTH = 1

CONCURRENCY = 25
SLEEP_BETWEEN = 0.3
LIMIT_PER_REQUEST = 50
MAX_PAGES = 20  # 최대 1,000건/월/종목
PROGRESS_EVERY = 100  # N개 종목마다 진행상황 로그


# ── 월 범위 생성 ───────────────────────────────────

def build_month_ranges() -> list[tuple[str, str, str]]:
    """
    (month_key, from_date, to_date) 리스트 반환.
    예: ("2026_01", "2026-01-01", "2026-01-31")
    현재 월은 to_date 가 오늘까지.
    """
    today = date.today()
    ranges = []
    year, month = START_YEAR, START_MONTH
    while (year, month) <= (today.year, today.month):
        last_day = calendar.monthrange(year, month)[1]
        from_date = date(year, month, 1)
        to_date = date(year, month, last_day)
        if (year, month) == (today.year, today.month):
            to_date = today
        ranges.append(
            (
                f"{year}_{month:02d}",
                from_date.isoformat(),
                to_date.isoformat(),
            )
        )
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return ranges


def ticker_file_path(month_key: str, ticker: str) -> Path:
    return BACKFILL_DIR / month_key / f"{ticker}.json"


def normalize_article(item: dict, symbol: str) -> dict:
    return {
        "title": item.get("title", ""),
        "text": item.get("text", ""),
        "publishedDate": item.get("publishedDate", ""),
        "url": item.get("url", ""),
        "source": item.get("site", ""),
        "symbol": symbol,
    }


# ── 티커 1개 수집 ──────────────────────────────────

async def fetch_ticker(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    ticker: str,
    from_date: str,
    to_date: str,
) -> tuple[str, list[dict], str | None]:
    """
    티커 1개에 대해 페이지네이션으로 전체 뉴스를 수집.

    symbols=<ticker> 1개만 보내므로 limit 이 해당 종목에만 적용된다.
    반환: (ticker, 기사 리스트, 오류 메시지 또는 None)
    """
    all_items: list[dict] = []
    seen_urls: set = set()

    try:
        for page in range(MAX_PAGES):
            params = {
                "symbols": ticker,
                "from": from_date,
                "to": to_date,
                "limit": LIMIT_PER_REQUEST,
                "page": page,
                "apikey": settings.fmp_api_key,
            }
            async with sem:
                response = await client.get(NEWS_ENDPOINT, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()
                await asyncio.sleep(SLEEP_BETWEEN)

            items = data if isinstance(data, list) else []
            new_items = [i for i in items if i.get("url") not in seen_urls]
            seen_urls.update(i.get("url") for i in new_items)
            all_items.extend(new_items)

            if len(items) < LIMIT_PER_REQUEST:
                break

        return ticker, all_items, None
    except Exception as e:
        return ticker, [], f"{ticker}: {type(e).__name__}: {e}"


# ── 월 단위 처리 ───────────────────────────────────

async def process_month(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    month_key: str,
    from_date: str,
    to_date: str,
    tickers: list[str],
    errors: list,
    ticker_totals: dict,
) -> int:
    """월 단위: 모든 티커를 개별로 동시 처리하고 티커별 파일로 저장."""
    month_dir = BACKFILL_DIR / month_key
    month_dir.mkdir(parents=True, exist_ok=True)

    total = len(tickers)
    tasks = [
        asyncio.create_task(fetch_ticker(client, sem, t, from_date, to_date))
        for t in tickers
    ]

    month_total = 0
    done = 0
    for fut in asyncio.as_completed(tasks):
        ticker, articles, err = await fut
        done += 1

        if err is not None:
            logger.error(f"[{month_key}] {err}")
            errors.append(f"[{month_key}] {err}")
            continue

        normalized = [normalize_article(item, ticker) for item in articles]
        with open(ticker_file_path(month_key, ticker), "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)

        ticker_totals[ticker] = ticker_totals.get(ticker, 0) + len(normalized)
        month_total += len(normalized)

        if done % PROGRESS_EVERY == 0 or done == total:
            logger.info(
                f"[{month_key}] 진행 {done}/{total} 종목, 누적 {month_total:,}건"
            )

    logger.info(f"[{month_key}] 완료: {total}개 종목, 총 {month_total:,}건 저장")
    return month_total


# ── 메인 ──────────────────────────────────────────

async def run():
    started_at = datetime.now()

    tickers = load_tickers_from_csv(UNIVERSE_CSV, status_filter="included")
    if not tickers:
        logger.error(
            f"universe 티커가 없습니다. 먼저 유니버스를 빌드하세요: {UNIVERSE_CSV}"
        )
        sys.exit(1)

    month_ranges = build_month_ranges()
    BACKFILL_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(
        f"백필 시작: {len(tickers):,}개 종목, "
        f"{len(month_ranges)}개월 ({month_ranges[0][0]} ~ {month_ranges[-1][0]}), "
        f"티커별 개별 요청 (동시 {CONCURRENCY})"
    )

    errors: list = []
    ticker_totals: dict[str, int] = {}
    grand_total = 0

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient() as client:
        for month_key, from_date, to_date in month_ranges:
            month_total = await process_month(
                client, sem, month_key, from_date, to_date,
                tickers, errors, ticker_totals,
            )
            grand_total += month_total

    finished_at = datetime.now()

    logger.info(
        f"전체 완료: {len(month_ranges)}개월, {len(tickers):,}개 종목, "
        f"총 {grand_total:,}건"
    )

    empty_tickers = sorted(t for t in tickers if ticker_totals.get(t, 0) == 0)
    stats = {
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
        "months": [m[0] for m in month_ranges],
        "total_tickers": len(tickers),
        "total_articles": grand_total,
        "empty_tickers": empty_tickers,
        "errors": errors,
    }

    stats_path = BACKFILL_DIR / "backfill_stats.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    logger.info(f"통계 저장: {stats_path}")
    logger.info(
        f"  종목 {len(tickers):,} / 뉴스 0건 종목 {len(empty_tickers):,} / 오류 {len(errors)}건"
    )


if __name__ == "__main__":
    asyncio.run(run())

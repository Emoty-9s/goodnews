"""
주간 가격/벤치마크 수집기
==========================
주간 리포트(final)에 첨부할 가격 변동률을 FMP에서 수집한다.

변동률 정의
-----------
* 종목/지수: (금요일 close - 월요일 open) / 월요일 open * 100
  - /stable/historical-price-eod/full 사용 (open, close 모두 제공)
  - 데이터는 날짜 내림차순 → 정렬 후 첫 거래일 open, 마지막 거래일 close 사용
  - S&P500 지수 심볼은 ^GSPC (^SPX 는 구독 미포함)
* 섹터: 일별 averageChange(%) 를 복리 합성 → ∏(1 + c/100) - 1
  - /stable/historical-sector-performance 사용
  - exchange 파라미터로 NASDAQ / NYSE / AMEX 각각 수집 (11섹터 × 3거래소 = 33회)

가격 데이터는 뉴스 분석(Gemini)과 분리한다 — 프롬프트에 넣지 않는다.
"""
import asyncio
from datetime import date

import httpx
from loguru import logger

from app.core.config import get_settings

settings = get_settings()

BASE = "https://financialmodelingprep.com"
EOD_FULL = f"{BASE}/stable/historical-price-eod/full"
SECTOR_PERF = f"{BASE}/stable/historical-sector-performance"

SP500_SYMBOL = "^GSPC"

# FMP available-sectors (universe_current.csv 의 sector 값과 1:1 일치)
SECTORS = [
    "Basic Materials",
    "Communication Services",
    "Consumer Cyclical",
    "Consumer Defensive",
    "Energy",
    "Financial Services",
    "Healthcare",
    "Industrials",
    "Real Estate",
    "Technology",
    "Utilities",
]

# AMEX 도 historical-sector-performance 지원 확인됨 (3거래소 모두 사용)
EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]

CONCURRENCY = 25
SLEEP_BETWEEN = 0.3


def _open_close_change(rows: list[dict]) -> float | None:
    """
    EOD full 응답(rows)에서 (마지막 거래일 close - 첫 거래일 open) / 첫 거래일 open * 100.
    데이터가 없거나 open 이 0/누락이면 None.
    """
    if not rows:
        return None
    valid = [r for r in rows if r.get("date")]
    if not valid:
        return None
    valid.sort(key=lambda r: r["date"])  # 날짜 오름차순
    first_open = valid[0].get("open")
    last_close = valid[-1].get("close")
    if not first_open or last_close is None:
        return None
    try:
        return (last_close - first_open) / first_open * 100
    except ZeroDivisionError:
        return None


def _compound_daily_changes(rows: list[dict]) -> float | None:
    """일별 averageChange(%) 리스트를 복리 합성 → 주간 % 변동률."""
    changes = [r.get("averageChange") for r in rows if r.get("averageChange") is not None]
    if not changes:
        return None
    factor = 1.0
    for c in changes:
        factor *= 1 + c / 100
    return (factor - 1) * 100


async def _get(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, params: dict):
    """공통 GET — 세마포어 + sleep, 실패 시 None."""
    full = {**params, "apikey": settings.fmp_api_key}
    try:
        async with sem:
            r = await client.get(url, params=full, timeout=30)
            await asyncio.sleep(SLEEP_BETWEEN)
        if r.status_code != 200:
            logger.warning(f"FMP {url} status={r.status_code} params={params}")
            return None
        return r.json()
    except httpx.HTTPError as e:
        logger.warning(f"FMP 요청 실패 {url} params={params}: {e}")
        return None


async def _fetch_price_change(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    symbol: str,
    from_date: str,
    to_date: str,
) -> float | None:
    data = await _get(
        client, sem, EOD_FULL,
        {"symbol": symbol, "from": from_date, "to": to_date},
    )
    if not isinstance(data, list):
        return None
    return _open_close_change(data)


async def fetch_weekly_price_change(
    ticker: str, week_monday: date, week_friday: date
) -> float | None:
    """단일 종목 주간 변동률 (월요일 open → 금요일 close)."""
    sem = asyncio.Semaphore(1)
    async with httpx.AsyncClient() as client:
        return await _fetch_price_change(
            client, sem, ticker, week_monday.isoformat(), week_friday.isoformat()
        )


async def fetch_all_weekly_price_changes(
    tickers: list[str], week_monday: date, week_friday: date
) -> dict[str, float]:
    """여러 종목 주간 변동률을 동시 수집. {ticker: pct} (None 은 제외)."""
    from_date = week_monday.isoformat()
    to_date = week_friday.isoformat()
    sem = asyncio.Semaphore(CONCURRENCY)
    logger.info(f"[PRICE] {len(tickers)}개 종목 주간 변동률 수집 시작")

    async with httpx.AsyncClient() as client:
        tasks = [
            _fetch_price_change(client, sem, t, from_date, to_date) for t in tickers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict[str, float] = {}
    for ticker, res in zip(tickers, results):
        if isinstance(res, Exception):
            logger.warning(f"[PRICE] {ticker} 변동률 수집 실패: {res}")
            continue
        if res is not None:
            out[ticker.upper()] = res
    logger.info(f"[PRICE] 변동률 수집 완료: {len(out)}/{len(tickers)}개")
    return out


async def fetch_sp500_weekly_change(
    week_monday: date, week_friday: date
) -> float | None:
    """S&P500(^GSPC) 주간 변동률."""
    sem = asyncio.Semaphore(1)
    async with httpx.AsyncClient() as client:
        pct = await _fetch_price_change(
            client, sem, SP500_SYMBOL,
            week_monday.isoformat(), week_friday.isoformat(),
        )
    logger.info(f"[PRICE] S&P500 주간 변동률: {pct}")
    return pct


async def fetch_sector_weekly_changes(
    week_monday: date, week_friday: date
) -> dict[tuple[str, str], float]:
    """
    11섹터 × 3거래소 주간 변동률 (일별 averageChange 복리 합성).
    반환: {(sector, exchange): pct} — None 은 제외.
    """
    from_date = week_monday.isoformat()
    to_date = week_friday.isoformat()
    sem = asyncio.Semaphore(CONCURRENCY)
    pairs = [(s, e) for s in SECTORS for e in EXCHANGES]
    logger.info(f"[PRICE] 섹터 변동률 수집 시작 ({len(pairs)}회)")

    async def one(sector: str, exchange: str):
        data = await _get(
            client, sem, SECTOR_PERF,
            {"sector": sector, "exchange": exchange,
             "from": from_date, "to": to_date},
        )
        rows = data if isinstance(data, list) else []
        return (sector, exchange), _compound_daily_changes(rows)

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[one(s, e) for s, e in pairs], return_exceptions=True
        )

    out: dict[tuple[str, str], float] = {}
    for res in results:
        if isinstance(res, Exception):
            logger.warning(f"[PRICE] 섹터 수집 실패: {res}")
            continue
        key, pct = res
        if pct is not None:
            out[key] = pct
    logger.info(f"[PRICE] 섹터 변동률 수집 완료: {len(out)}/{len(pairs)}개")
    return out

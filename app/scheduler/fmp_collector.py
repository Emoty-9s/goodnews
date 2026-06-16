import asyncio
import httpx
from datetime import datetime, timedelta, timezone
from loguru import logger

from app.core.config import get_settings

settings = get_settings()

NEWS_ENDPOINT = "https://financialmodelingprep.com/stable/news/stock"
GENERAL_NEWS_ENDPOINT = "https://financialmodelingprep.com/stable/news/general-latest"
MAX_PAGES = 20  # 티커당 최대 페이지 (페이지당 limit 건)
GENERAL_MAX_PAGES = 30  # 일반 뉴스 주간 수집용 (하루 ~75건 × 5일 ≈ 7페이지)


class FMPNewsCollector:
    def __init__(self):
        self.api_key = settings.fmp_api_key

    async def fetch_ticker(self, client, semaphore, ticker, from_date=None, limit=50):
        """
        티커 1개에 대해 페이지네이션으로 전체 뉴스를 수집.

        symbols=<ticker> 1개만 보내므로 limit 이 해당 종목에만 적용된다.
        (배치로 보내면 limit 이 응답 전체에 적용돼 뒤쪽 종목이 누락됨)
        반환: (ticker, 기사 리스트)
        """
        all_items = []
        seen_urls = set()
        try:
            for page in range(MAX_PAGES):
                params = {
                    "symbols": ticker,
                    "limit": limit,
                    "page": page,
                    "apikey": self.api_key,
                }
                if from_date:
                    params["from"] = from_date
                async with semaphore:
                    response = await client.get(NEWS_ENDPOINT, params=params, timeout=30)
                    response.raise_for_status()
                    data = response.json()
                    await asyncio.sleep(0.3)

                items = data if isinstance(data, list) else []
                new_items = [i for i in items if i.get("url") not in seen_urls]
                seen_urls.update(i.get("url") for i in new_items)
                all_items.extend(new_items)

                if len(items) < limit:
                    break

            return ticker, all_items
        except httpx.HTTPError as e:
            logger.error(f"FMP API 오류 (ticker: {ticker}): {e}")
            return ticker, []

    async def fetch_all(self, all_tickers, since=None, limit_per_batch=50, concurrency=25):
        """모든 티커를 개별 요청으로 동시 수집 (limit_per_batch = 티커당 limit)."""
        logger.info(
            f"총 {len(all_tickers)}개 티커 개별 요청 시작 (동시 {concurrency})"
        )

        if since and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        from_date = since.date().isoformat() if since else None

        semaphore = asyncio.Semaphore(concurrency)
        async with httpx.AsyncClient() as client:
            tasks = [
                self.fetch_ticker(client, semaphore, t, from_date, limit_per_batch)
                for t in all_tickers
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        news_by_ticker = {}
        total = 0
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"티커 수집 실패: {result}")
                continue
            ticker, items = result
            ticker = (ticker or "").upper()
            if not ticker:
                continue

            kept = []
            for item in items:
                if since:
                    pub_date_str = item.get("publishedDate", "")
                    try:
                        pub_date = datetime.fromisoformat(
                            pub_date_str.replace("Z", "+00:00")
                        )
                        if pub_date.tzinfo is None:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)
                        if pub_date < since:
                            continue
                    except (ValueError, AttributeError):
                        pass
                kept.append(item)

            if kept:
                news_by_ticker[ticker] = kept
                total += len(kept)

        logger.info(f"수집 완료: {len(news_by_ticker)}개 종목, 총 {total}건")
        return news_by_ticker


async def fetch_general_news(
    from_date: str, to_date: str, limit: int = 50
) -> list[dict]:
    """
    /stable/news/general-latest 를 페이지네이션으로 수집한다 (일반 시장 뉴스).

    - from_date / to_date: 'YYYY-MM-DD'
    - site 에 'youtube' 가 포함된 항목은 제외 (요약 품질 낮음)
    - symbol 은 항상 None (종목 태그 없음). insert_market_news 에서 정규화.

    반환: 원본 기사 dict 리스트 (title/text/url/site/publishedDate 등)
    """
    api_key = settings.fmp_api_key
    all_items: list[dict] = []
    seen_urls: set[str] = set()
    excluded_youtube = 0

    async with httpx.AsyncClient() as client:
        for page in range(GENERAL_MAX_PAGES):
            params = {
                "from": from_date,
                "to": to_date,
                "page": page,
                "limit": limit,
                "apikey": api_key,
            }
            try:
                response = await client.get(
                    GENERAL_NEWS_ENDPOINT, params=params, timeout=30
                )
                response.raise_for_status()
                data = response.json()
                await asyncio.sleep(0.3)
            except httpx.HTTPError as e:
                logger.error(f"일반 뉴스 수집 오류 (page {page}): {e}")
                break

            items = data if isinstance(data, list) else []
            if not items:
                break

            for it in items:
                site = (it.get("site") or "").lower()
                if "youtube" in site:
                    excluded_youtube += 1
                    continue
                url = it.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                all_items.append(it)

            if len(items) < limit:
                break

    logger.info(
        f"[GENERAL-NEWS] 수집 {len(all_items)}건 "
        f"(youtube 제외 {excluded_youtube}건, {from_date}~{to_date})"
    )
    return all_items


def get_since_datetime(digest_type: str) -> datetime:
    now = datetime.now(timezone.utc)
    if digest_type == "daily":
        return now - timedelta(hours=24)
    elif digest_type == "weekly":
        days_since_monday = now.weekday()
        monday = now - timedelta(days=days_since_monday)
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    elif digest_type == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif digest_type == "yearly":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"알 수 없는 digest_type: {digest_type}")
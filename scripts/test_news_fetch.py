#!/usr/bin/env python3
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings

LARGE_CAP = ["NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", "TSM", "AVGO", "TSLA", "META", "GOOG"]
MID_CAP = ["PHVS", "AXGN", "BAND", "ESTA", "BANR", "BBUC", "CEPU", "PLUS", "MCRI", "HTH"]
SMALL_CAP = ["REKR", "EMPD", "XGN", "ZVIA", "GEOS", "CCLD", "PZG", "VTIX", "SFBC", "NOEM"]

GROUPS = [
    ("대형주", LARGE_CAP),
    ("중형주", MID_CAP),
    ("소형주", SMALL_CAP),
]

NEWS_ENDPOINT = "https://financialmodelingprep.com/stable/news/stock"
OUTPUT_PATH = ROOT / "test_news_raw.json"


def parse_published_date(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


async def fetch_ticker(client: httpx.AsyncClient, symbol: str, api_key: str, since: datetime):
    params = {"symbols": symbol, "limit": 50, "apikey": api_key}
    try:
        response = await client.get(NEWS_ENDPOINT, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as e:
        print(f"  [오류] {symbol}: {e}")
        return symbol, []

    if not isinstance(data, list):
        return symbol, []

    recent = []
    for item in data:
        pub_date = parse_published_date(item.get("publishedDate", ""))
        if pub_date is None:
            continue
        if pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=timezone.utc)
        if pub_date < since:
            continue
        recent.append(
            {
                "title": item.get("title", ""),
                "publishedDate": item.get("publishedDate", ""),
                "text": item.get("text", ""),
                "url": item.get("url", ""),
                "site": item.get("site", ""),
            }
        )

    recent.sort(key=lambda x: x.get("publishedDate", ""), reverse=True)
    return symbol, recent


def print_ticker_news(group_label: str, symbol: str, news_list: list):
    print("=" * 28)
    print(f"[{group_label}] {symbol}")
    print("=" * 28)

    if not news_list:
        print("뉴스 0건 - 해당 없음")
        print()
        return

    print(f"뉴스 {len(news_list)}건")
    print()
    for idx, item in enumerate(news_list, 1):
        pub = parse_published_date(item.get("publishedDate", ""))
        pub_str = pub.strftime("%Y-%m-%d %H:%M") if pub else item.get("publishedDate", "")
        text = item.get("text", "") or ""
        print(f"[{idx}] {pub_str}")
        print(f"제목: {item.get('title', '')}")
        print(f"출처: {item.get('site', '')}")
        print(f"본문 길이: {len(text):,}자")
        print("본문 앞 500자:")
        print(f"  {text[:500]}")
        print(f"URL: {item.get('url', '')}")
        print("---")
    print()


async def main():
    settings = get_settings()
    api_key = settings.fmp_api_key
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    results = {}
    group_results = []

    async with httpx.AsyncClient() as client:
        for group_label, tickers in GROUPS:
            tasks = [fetch_ticker(client, symbol, api_key, since) for symbol in tickers]
            fetched = await asyncio.gather(*tasks)
            ordered = {symbol: news for symbol, news in fetched}
            news_for_group = [(symbol, ordered.get(symbol, [])) for symbol in tickers]
            group_results.append((group_label, news_for_group))

            for symbol, news_list in news_for_group:
                print_ticker_news(group_label, symbol, news_list)
                results[symbol] = news_list

    print_summary(group_results)
    save_json(results)


def print_summary(group_results):
    print("=" * 10 + " 수집 요약 " + "=" * 10)

    total_count = 0
    total_tickers = 0
    total_text_len = 0
    total_news_items = 0
    zero_tickers = []

    for group_label, news_for_group in group_results:
        group_count = sum(len(news) for _, news in news_for_group)
        total_count += group_count
        total_tickers += len(news_for_group)
        print(f"{group_label}: {len(news_for_group)}개 티커, 총 {group_count}건")

        for symbol, news in news_for_group:
            if not news:
                zero_tickers.append(symbol)
            for item in news:
                total_text_len += len(item.get("text", "") or "")
                total_news_items += 1

    print(f"뉴스 0건 티커: {len(zero_tickers)}개 → {zero_tickers}")
    print(f"전체: {total_tickers}개 티커, 총 {total_count}건")
    avg_len = (total_text_len / total_news_items) if total_news_items else 0
    print(f"평균 본문 길이: {avg_len:,.0f}자")


def save_json(results: dict):
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    size_bytes = OUTPUT_PATH.stat().st_size
    print()
    print(f"JSON 저장 완료: {OUTPUT_PATH}")
    print(f"파일 크기: {size_bytes:,} bytes ({size_bytes / 1024:,.1f} KB)")


if __name__ == "__main__":
    asyncio.run(main())

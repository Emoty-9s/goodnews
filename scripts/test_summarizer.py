#!/usr/bin/env python3
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

_key = os.environ.get("GEMINI_API_KEY", "").strip()
if not _key or _key in ("dummy", "여기에_키_입력"):
    print("GEMINI_API_KEY를 .env에 설정하세요")
    sys.exit(1)

from app.summarizer.llm_summarizer import summarize_ticker

NEWS_JSON_PATH = ROOT / "test_news_raw.json"
SUMMARY_DIR = ROOT / "data" / "summary"
TOP_N = 5


def print_result(result: dict, news_count: int):
    prompt_used = "SIMPLE" if news_count <= 4 else "FULL"
    print("=" * 60)
    print(f"티커: {result['ticker']}")
    print(f"뉴스 건수: {news_count}건")
    print(f"사용 프롬프트: {prompt_used}")
    print(f"digest_type: {result['digest_type']}")
    print(f"sentiment: {result['sentiment']}")
    print("-" * 60)
    print("summary_text:")
    print(result["summary_text"])
    print("=" * 60)
    print()


def save_results(results: dict):
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "generated_at": generated_at,
        "ticker_count": len(results),
        "results": results,
    }

    latest_path = SUMMARY_DIR / "summary_latest.json"
    history_path = SUMMARY_DIR / f"summary_{now.strftime('%Y%m%d_%H%M%S')}.json"

    for path in (latest_path, history_path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    print("저장 완료:")
    print(f"  {latest_path.relative_to(ROOT)}")
    print(f"  {history_path.relative_to(ROOT)}")


def main():
    if not NEWS_JSON_PATH.exists():
        print("test_news_raw.json 없음. 먼저 python scripts/test_news_fetch.py 실행하세요")
        sys.exit(1)

    with open(NEWS_JSON_PATH, "r", encoding="utf-8") as f:
        news_by_ticker = json.load(f)

    # 뉴스 0건 티커 스킵 + 건수 많은 순 정렬
    non_empty = [
        (ticker, articles)
        for ticker, articles in news_by_ticker.items()
        if articles
    ]
    non_empty.sort(key=lambda x: len(x[1]), reverse=True)
    targets = non_empty[:TOP_N]

    if not targets:
        print("테스트할 뉴스가 없습니다. (모든 티커 0건)")
        sys.exit(0)

    print(f"테스트 대상: 상위 {len(targets)}개 티커 (뉴스 건수 순)")
    print()

    start = time.time()
    results = {}
    for ticker, articles in targets:
        print(f"\n[{ticker}] 뉴스 {len(articles)}건 요약 시작")
        result = summarize_ticker(ticker, articles, "daily")
        news_count = len(articles)
        print_result(result, news_count)

        results[ticker] = {
            "ticker": result["ticker"],
            "digest_type": result["digest_type"],
            "sentiment": result["sentiment"],
            "news_count": news_count,
            "summary_text": result["summary_text"],
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    elapsed = time.time() - start
    print(f"전체 소요시간: {elapsed:.1f}초")
    print()

    save_results(results)


if __name__ == "__main__":
    main()

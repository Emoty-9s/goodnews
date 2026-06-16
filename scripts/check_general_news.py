"""
FMP 일반 시장 뉴스 엔드포인트 응답 구조 확인용 스크립트.

NOTE: /stable/general-news 는 404. 실제 엔드포인트는 /stable/news/general-latest.

확인 항목:
1. 응답 필드 구조 (title/text/url/publishedDate 등)
2. ticker / sector 등 분류 태그 존재 여부
3. 일주일치(월~금) 총 건수 + 페이지네이션 동작 + from/to 필터 동작
4. 실제 제목 샘플 (금융/시장 뉴스 vs 일반 시사뉴스 혼입 여부)

실행: python scripts/check_general_news.py
"""
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import httpx

from app.core.config import get_settings

settings = get_settings()
ENDPOINT = "https://financialmodelingprep.com/stable/news/general-latest"

FROM = "2026-06-08"
TO = "2026-06-12"
LIMIT = 50
MAX_PAGES = 30  # 페이지네이션 한계 확인용


async def fetch_page(client: httpx.AsyncClient, page: int):
    params = {
        "from": FROM,
        "to": TO,
        "page": page,
        "limit": LIMIT,
        "apikey": settings.fmp_api_key,
    }
    r = await client.get(ENDPOINT, params=params, timeout=30)
    if r.status_code != 200:
        print(f"  page={page} status={r.status_code} body={r.text[:300]}")
        return None
    return r.json()


async def main():
    async with httpx.AsyncClient() as client:
        # 1) 첫 페이지로 구조 확인
        print("=" * 70)
        print("(1) 응답 필드 구조 — page=0")
        print("=" * 70)
        first = await fetch_page(client, 0)
        if not isinstance(first, list) or not first:
            print("응답이 비어있거나 리스트가 아님:", type(first))
            print(json.dumps(first, ensure_ascii=False)[:500])
            return

        print(f"page=0 반환 건수: {len(first)}")
        print("\n첫 기사 전체 필드:")
        print(json.dumps(first[0], ensure_ascii=False, indent=2))

        all_keys = set()
        for it in first:
            all_keys.update(it.keys())
        print(f"\n전체 필드 키: {sorted(all_keys)}")

        # 2) ticker / sector 태그 존재 여부
        print("\n" + "=" * 70)
        print("(2) 분류 태그(ticker/sector/symbol/tags) 존재 여부")
        print("=" * 70)
        for key in ("symbol", "ticker", "tickers", "sector", "tags", "site", "publisher"):
            present = sum(1 for it in first if it.get(key) not in (None, "", []))
            sample = next((it.get(key) for it in first if it.get(key)), None)
            print(f"  {key:10}: {present}/{len(first)}건 존재 | 샘플={sample!r}")

        # 3) 페이지네이션 — 전체 건수
        print("\n" + "=" * 70)
        print("(3) 페이지네이션 — 월~금 전체 건수")
        print("=" * 70)
        total = list(first)
        last_page = 0
        for page in range(1, MAX_PAGES):
            data = await fetch_page(client, page)
            if not isinstance(data, list) or not data:
                print(f"  page={page}: 빈 응답 → 종료")
                break
            total.extend(data)
            last_page = page
            print(f"  page={page}: {len(data)}건 누적 {len(total)}건")
            if len(data) < LIMIT:
                print(f"  page={page}: limit({LIMIT}) 미만 → 마지막 페이지")
                break
        print(f"\n총 {len(total)}건 (마지막 페이지={last_page})")

        # publishedDate 범위 확인
        dates = sorted(it.get("publishedDate", "")[:10] for it in total if it.get("publishedDate"))
        if dates:
            print(f"날짜 범위: {dates[0]} ~ {dates[-1]}")
            print(f"날짜별 분포: {dict(Counter(dates))}")

        # site/publisher 분포 (출처 성격 파악)
        site_key = "site" if any(it.get("site") for it in total) else "publisher"
        site_dist = Counter(it.get(site_key) for it in total if it.get(site_key))
        if site_dist:
            print(f"\n출처({site_key}) 상위 15개:")
            for s, c in site_dist.most_common(15):
                print(f"  {s}: {c}건")

        # 4) 제목 샘플 (성격 판단)
        print("\n" + "=" * 70)
        print("(4) 제목 샘플 20개 (금융/시장 vs 일반 시사 판단)")
        print("=" * 70)
        title_key = "title" if any(it.get("title") for it in total) else "headline"
        for i, it in enumerate(total[:20], 1):
            t = it.get(title_key, "") or ""
            site = it.get(site_key, "")
            d = (it.get("publishedDate", "") or "")[:10]
            print(f"  {i:2}. [{d}] ({site}) {t[:90]}")


if __name__ == "__main__":
    asyncio.run(main())

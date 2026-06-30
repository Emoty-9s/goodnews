#!/usr/bin/env python3
# 예상 실행 시간 (4,000종목, 3개월):
#   - FMP 수집: concurrency=50 기준 약 30~60분
#   - 중복제거: 약 2~5분
#   - DB업로드: 약 5~10분
#   - 전체: 약 40~75분
"""
FMP /stable/news/stock 에서 최근 N주치 뉴스를 수집하여
data/backfill/ 에 로컬 저장하고, 중복 제거 후 Supabase articles 테이블에 INSERT.

Usage:
    python scripts/fetch_news_backfill.py                   # 12주, universe 전체
    python scripts/fetch_news_backfill.py --concurrency 80  # 동시 요청 수 조정
    python scripts/fetch_news_backfill.py --weeks 4         # 최근 4주로 범위 축소
    python scripts/fetch_news_backfill.py --tickers AAPL,NVDA,MSFT
    python scripts/fetch_news_backfill.py --retry-failed    # 실패 티커만 재수집
    python scripts/fetch_news_backfill.py --no-upload       # 로컬 저장만
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

import httpx
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.models.database import insert_articles
from app.universe.ticker_store import get_universe_tickers

settings = get_settings()

# ─── 상수 ────────────────────────────────────────────────────────

NEWS_ENDPOINT = "https://financialmodelingprep.com/stable/news/stock"
BACKFILL_DIR = ROOT / "data" / "backfill"
FAILED_FILE = BACKFILL_DIR / "failed_tickers.json"

DEFAULT_LIMIT = 750     # FMP /stable 최대 페이지 크기
MAX_PAGES = 20          # 티커당 최대 페이지 (20 × 750 = 최대 15,000건/티커/월)
PAGE_SLEEP = 0.05       # 페이지 요청 후 대기(초)
PROGRESS_EVERY = 100    # N 티커마다 진행상황 출력


# ─── 유틸 ────────────────────────────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}초"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}분 {s:02d}초"
    h, m = divmod(m, 60)
    return f"{h}시간 {m:02d}분 {s:02d}초"


# ─── 날짜 범위 계산 ──────────────────────────────────────────────

def build_month_ranges(weeks: int = 12) -> list[tuple[str, str, str]]:
    """
    오늘 기준 N주 전이 속한 달의 1일부터 오늘까지, 월별로 분할.

    반환: [(label, from_date, to_date), ...]
      label    = "2026_04"
      from_date = "2026-04-01"
      to_date   = "2026-04-30" (마지막 달은 오늘 날짜)
    """
    today = date.today()
    cutoff = today - timedelta(weeks=weeks)
    cur = cutoff.replace(day=1)

    ranges: list[tuple[str, str, str]] = []
    while cur <= today:
        label = cur.strftime("%Y_%m")
        from_str = cur.isoformat()
        last_day = date(cur.year, cur.month, monthrange(cur.year, cur.month)[1])
        to_str = min(last_day, today).isoformat()
        ranges.append((label, from_str, to_str))
        cur = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)

    return ranges


# ─── FMP 수집 ────────────────────────────────────────────────────

async def _fetch_page(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    params: dict,
) -> list[dict]:
    """단일 페이지 요청. 429 시 exponential backoff (1→2→4초, 최대 3회)."""
    for attempt in range(3):
        try:
            async with semaphore:
                resp = await client.get(NEWS_ENDPOINT, params=params, timeout=30)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"429 Too Many Requests — {wait}초 후 재시도 (시도 {attempt + 1}/3)")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            await asyncio.sleep(PAGE_SLEEP)
            data = resp.json()
            return data if isinstance(data, list) else []
        except httpx.HTTPStatusError as e:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            logger.error(
                f"HTTP 오류 ticker={params.get('symbols')} "
                f"page={params.get('page')}: {e}"
            )
            return []
        except httpx.HTTPError as e:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            logger.error(f"네트워크 오류: {e}")
            return []
    return []


async def _fetch_ticker(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    ticker: str,
    from_date: str,
    to_date: str,
    limit: int = DEFAULT_LIMIT,
) -> tuple[str, list[dict]]:
    """티커 1개의 전체 뉴스를 페이지네이션으로 수집."""
    all_items: list[dict] = []
    seen_urls: set[str] = set()

    for page in range(MAX_PAGES):
        params = {
            "symbols": ticker,
            "limit": limit,
            "page": page,
            "from": from_date,
            "to": to_date,
            "apikey": settings.fmp_api_key,
        }
        items = await _fetch_page(client, semaphore, params)
        new_items = [i for i in items if i.get("url") and i["url"] not in seen_urls]
        seen_urls.update(i["url"] for i in new_items)
        all_items.extend(new_items)

        if len(items) < limit:
            break

    return ticker, all_items


# ─── 중복 제거 ───────────────────────────────────────────────────

def _word_set(s: str) -> set[str]:
    return set(re.findall(r"\w+", s.lower())) if s else set()


def _jaccard(a: str, b: str) -> float:
    sa, sb = _word_set(a), _word_set(b)
    if not sa and not sb:
        return 1.0
    union = len(sa | sb)
    return len(sa & sb) / union if union else 0.0


def deduplicate(articles: list[dict]) -> list[dict]:
    """
    3단계 중복 제거.

    Rule 1: 동일 티커 내 같은 URL — 수집 시 seen_urls로 이미 처리.
    Rule 2: 다른 티커 간 같은 URL — tickers 배열 병합, 1건 유지.
    Rule 3: 동일 티커 내 제목 Jaccard ≥ 0.8 — 본문 더 긴 것 유지.
    """
    # Rule 2: URL 기준 통합 + tickers 병합
    by_url: dict[str, dict] = {}
    for art in articles:
        url = (art.get("url") or "").strip()
        if not url:
            continue
        art = dict(art)
        raw_tickers: list[str] = []
        if art.get("tickers"):
            raw_tickers = [str(t).upper() for t in art["tickers"] if t]
        elif art.get("symbol"):
            raw_tickers = [str(art["symbol"]).upper()]
        art["tickers"] = raw_tickers
        art["url_hash"] = _sha256(url)

        if url not in by_url:
            by_url[url] = art
        else:
            existing = by_url[url]
            existing["tickers"] = list(set(existing["tickers"] + art["tickers"]))
            if len(art.get("text") or "") > len(existing.get("text") or ""):
                existing["title"] = art.get("title") or existing.get("title")
                existing["text"] = art.get("text") or existing.get("text")

    pool = list(by_url.values())

    # Rule 3: 동일 티커 내 Jaccard ≥ 0.8
    ticker_idx: dict[str, list[int]] = {}
    for i, art in enumerate(pool):
        for t in (art.get("tickers") or []):
            ticker_idx.setdefault(t, []).append(i)

    to_remove: set[int] = set()
    for indices in ticker_idx.values():
        active = [i for i in indices if i not in to_remove]
        for j in range(len(active)):
            if active[j] in to_remove:
                continue
            title_j = pool[active[j]].get("title") or ""
            for k in range(j + 1, len(active)):
                if active[k] in to_remove:
                    continue
                title_k = pool[active[k]].get("title") or ""
                if _jaccard(title_j, title_k) >= 0.8:
                    len_j = len(pool[active[j]].get("text") or "")
                    len_k = len(pool[active[k]].get("text") or "")
                    to_remove.add(active[k] if len_j >= len_k else active[j])

    return [art for i, art in enumerate(pool) if i not in to_remove]


# ─── 월별 처리 ───────────────────────────────────────────────────

async def process_month(
    label: str,
    from_str: str,
    to_str: str,
    tickers: list[str],
    concurrency: int,
    no_upload: bool,
    failed_map: dict[str, list[str]],
) -> None:
    month_dir = BACKFILL_DIR / label
    month_dir.mkdir(parents=True, exist_ok=True)

    # 이미 완료된 티커 판별 (>= 2바이트 = 최소 "[]" 이상)
    to_fetch: list[str] = []
    skipped = 0
    for t in tickers:
        p = month_dir / f"{t}.json"
        if p.exists() and p.stat().st_size >= 2:
            skipped += 1
        else:
            to_fetch.append(t)

    total = len(to_fetch)
    print(
        f"[{label}] 시작: {len(tickers):,}개 티커 수집 예정 "
        f"(이미 완료 {skipped:,}개 스킵)"
    )

    state: dict = {"ok": 0, "collected": 0, "failed": []}
    t0 = time.monotonic()
    semaphore = asyncio.Semaphore(concurrency)

    limits = httpx.Limits(
        max_connections=concurrency + 20,
        max_keepalive_connections=concurrency,
    )

    async def fetch_and_save(ticker: str) -> None:
        ticker_path = month_dir / f"{ticker}.json"
        try:
            _, items = await _fetch_ticker(client, semaphore, ticker, from_str, to_str)
            ticker_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            state["ok"] += 1
            state["collected"] += len(items)
        except Exception as e:
            logger.error(f"[{label}] {ticker} 수집 실패: {type(e).__name__}: {e}")
            state["failed"].append(ticker)
            return  # 파일 미기록 → 다음 실행 시 자동 재시도

        done = state["ok"] + len(state["failed"])
        if done % PROGRESS_EVERY == 0 or done == total:
            elapsed = time.monotonic() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 and done < total else 0
            print(
                f"[{label}] 진행: {done:,}/{total:,} 완료 | "
                f"수집 {state['collected']:,}건 | "
                f"경과 {_fmt_time(elapsed)} | "
                f"예상 남은 시간 {_fmt_time(eta)}"
            )

    async with httpx.AsyncClient(limits=limits) as client:
        if to_fetch:
            await asyncio.gather(*[fetch_and_save(t) for t in to_fetch])

    print(
        f"[{label}] 완료: {state['ok']:,}개 수집 / "
        f"{skipped:,}개 스킵 / {len(state['failed']):,}개 실패 | "
        f"이번 수집 {state['collected']:,}건"
    )

    if state["failed"]:
        prev = failed_map.get(label, [])
        failed_map[label] = list(set(prev + state["failed"]))
        FAILED_FILE.write_text(
            json.dumps(failed_map, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ─── 중복제거 ────────────────────────────────────────────────
    print(f"[{label}] 중복제거 시작...")
    raw_articles: list[dict] = []
    for p in sorted(month_dir.glob("*.json")):
        try:
            items = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                continue
            for item in items:
                if not item.get("symbol"):
                    item["symbol"] = p.stem
            raw_articles.extend(items)
        except Exception:
            pass

    raw_count = len(raw_articles)
    deduped = deduplicate(raw_articles)
    removed = raw_count - len(deduped)
    print(
        f"[{label}] 중복제거: 원본 {raw_count:,}건 → "
        f"정제 {len(deduped):,}건 ({removed:,}건 제거)"
    )

    # ─── DB 업로드 ───────────────────────────────────────────────
    if no_upload:
        print(f"[{label}] --no-upload: DB 업로드 생략")
        return

    if not deduped:
        print(f"[{label}] DB업로드: 업로드할 기사 없음")
        return

    inserted = await insert_articles(deduped)
    skipped_db = len(deduped) - inserted
    print(
        f"[{label}] DB업로드: {len(deduped):,}건 INSERT 시도 → "
        f"{inserted:,}건 신규 / {skipped_db:,}건 중복 스킵"
    )


# ─── 진입점 ──────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="FMP 뉴스 백필 수집기 (수집 + 중복제거 + DB INSERT 원스톱)"
    )
    parser.add_argument("--weeks", type=int, default=12, help="수집 기간(주), 기본 12")
    parser.add_argument("--concurrency", type=int, default=50, help="FMP 동시 요청 수, 기본 50")
    parser.add_argument("--tickers", type=str, default="", help="특정 종목만 (콤마 구분)")
    parser.add_argument("--retry-failed", action="store_true", help="failed_tickers.json 대상만 재수집")
    parser.add_argument("--no-upload", action="store_true", help="로컬 저장만, DB 업로드 생략")
    args = parser.parse_args()

    BACKFILL_DIR.mkdir(parents=True, exist_ok=True)

    # 티커 목록
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        logger.info(f"지정 티커 {len(tickers)}개 사용")
    else:
        tickers = await get_universe_tickers()
        if not tickers:
            logger.error("Supabase universe_tickers 테이블이 비어있음. python scripts/upload_universe_to_supabase.py로 채워주세요.")
            sys.exit(1)
        logger.info(f"유니버스 {len(tickers):,}개 티커 로드")

    # 실패 티커 맵 로드
    failed_map: dict[str, list[str]] = {}
    if FAILED_FILE.exists():
        try:
            failed_map = json.loads(FAILED_FILE.read_text(encoding="utf-8"))
        except Exception:
            failed_map = {}

    # 날짜 범위
    month_ranges = build_month_ranges(args.weeks)
    logger.info(
        f"수집 기간: {month_ranges[0][1]} ~ {month_ranges[-1][2]} "
        f"({len(month_ranges)}개월)"
    )

    # 월별 처리
    for label, from_str, to_str in month_ranges:
        month_tickers = tickers

        if args.retry_failed:
            retry_set = set(failed_map.get(label, []))
            if not retry_set:
                logger.info(f"[{label}] 재시도 대상 없음, 스킵")
                continue
            # 기존 실패 파일 삭제 → 재수집
            month_dir = BACKFILL_DIR / label
            for t in retry_set:
                p = month_dir / f"{t}.json"
                if p.exists():
                    p.unlink()
            month_tickers = [t for t in tickers if t in retry_set]
            logger.info(f"[{label}] 재시도: {len(month_tickers)}개 실패 티커")
            failed_map.pop(label, None)

        await process_month(
            label=label,
            from_str=from_str,
            to_str=to_str,
            tickers=month_tickers,
            concurrency=args.concurrency,
            no_upload=args.no_upload,
            failed_map=failed_map,
        )

    # 최종 실패 요약
    total_failed = sum(len(v) for v in failed_map.values())
    if total_failed:
        logger.warning(
            f"전체 실패 티커: {total_failed}개 → {FAILED_FILE}\n"
            f"재시도: python scripts/fetch_news_backfill.py --retry-failed"
        )
    else:
        logger.info("모든 티커 수집 완료 (실패 없음)")


if __name__ == "__main__":
    asyncio.run(main())

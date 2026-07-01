#!/usr/bin/env python3
"""
scripts/backfill_full.py

주차 단위 순환 백필: 수집 → 업로드 → weekly 생성 → articles 삭제를 주차별로 반복.
articles는 항상 1주치만 DB에 존재하므로 Supabase 500MB 용량 문제가 없다.

전체 흐름:
  Step 0       : 벤치마크 + 시장뉴스 + 거시지표 소급 수집 (backfill_benchmarks_news.py 호출)
  Step 1~N     : 주차 루프 (오래된 주부터)
                   1-A. FMP 뉴스 수집 → 로컬 캐시 저장
                   1-B. articles 테이블 업로드
                   1-C. weekly final 생성 (phase1_weekly_final 재사용)
                   1-D. 해당 주 articles 즉시 삭제
  Step N+1     : midterm 생성 (phase3_midterm 재사용)
  Step N+2     : daily overnight 생성 (phase4_daily_overnight 재사용)

Usage:
    python scripts/backfill_full.py
    python scripts/backfill_full.py --weeks 4
    python scripts/backfill_full.py --tickers AAPL,NVDA,MSFT
    python scripts/backfill_full.py --force
    python scripts/backfill_full.py --skip-benchmarks
    python scripts/backfill_full.py --skip-daily
    python scripts/backfill_full.py --from-week 2026-04-07
    python scripts/backfill_full.py --concurrency 25
    python scripts/backfill_full.py --fmp-concurrency 50
    python scripts/backfill_full.py --weeks 1 --tickers AAPL,NVDA --skip-benchmarks --skip-daily
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import httpx
from loguru import logger

from app.models.database import (
    delete_articles_between,
    get_latest_macro_snapshot,
    insert_articles,
)
from app.universe.ticker_store import get_universe_tickers

# backfill_summaries.py의 phase 함수들 직접 import
from scripts.backfill_summaries import (
    _fmt_macro,
    _new_stats,
    phase1_weekly_final,
    phase3_midterm,
    phase4_daily_overnight,
)

# fetch_news_backfill.py의 FMP 수집 함수들 직접 import
from scripts.fetch_news_backfill import (
    DEFAULT_LIMIT,
    MAX_PAGES,
    PAGE_SLEEP,
    _fetch_page,
    _fetch_ticker,
    deduplicate,
)

ET = ZoneInfo("America/New_York")
WEEKLY_CACHE_DIR = ROOT / "data" / "backfill" / "weekly"


# ─── 유틸 ────────────────────────────────────────────────────────

def _p(msg: str) -> None:
    print(f"[BACKFILL] {msg}", flush=True)


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}초"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}분 {s:02d}초"
    h, m = divmod(m, 60)
    return f"{h}시간 {m:02d}분 {s:02d}초"


def build_week_mondays(weeks: int = 12) -> list[date]:
    """오늘 기준 최근 N주의 완료된 주 월요일 목록 (오래된 순)."""
    today = date.today()
    days_to_last_friday = (today.weekday() - 4) % 7
    last_friday = today - timedelta(days=days_to_last_friday)
    last_monday = last_friday - timedelta(days=4)
    return [last_monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]


# ─── Step 0: 벤치마크 수집 ──────────────────────────────────────

def run_benchmarks() -> bool:
    """backfill_benchmarks_news.py를 subprocess로 호출."""
    _p("Step 0 — 벤치마크 + 시장뉴스 + 거시지표 소급 수집 시작")
    script = ROOT / "scripts" / "backfill_benchmarks_news.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        _p(f"  Step 0 실패 (exit code {result.returncode})")
        return False
    _p("  Step 0 완료")
    return True


# ─── Step 1-A: 주차 단위 FMP 수집 ──────────────────────────────

async def fetch_week(
    week_monday: date,
    tickers: list[str],
    fmp_concurrency: int,
    force: bool,
) -> tuple[list[dict], int, int]:
    """
    주차 단위 FMP 뉴스 수집.

    Returns:
        (deduped_articles, raw_count, fetched_count)
    """
    week_friday = week_monday + timedelta(days=4)
    from_str = week_monday.isoformat()
    to_str = week_friday.isoformat()
    week_label = week_monday.isoformat()
    week_dir = WEEKLY_CACHE_DIR / week_label
    week_dir.mkdir(parents=True, exist_ok=True)

    if force and week_dir.exists():
        shutil.rmtree(week_dir)
        week_dir.mkdir(parents=True, exist_ok=True)
        _p(f"  1-A. --force: {week_label} 캐시 삭제 후 재수집")

    to_fetch: list[str] = []
    skipped = 0
    for t in tickers:
        p = week_dir / f"{t}.json"
        if p.exists() and p.stat().st_size >= 2:
            skipped += 1
        else:
            to_fetch.append(t)

    fetched_count = 0
    semaphore = asyncio.Semaphore(fmp_concurrency)
    limits = httpx.Limits(
        max_connections=fmp_concurrency + 20,
        max_keepalive_connections=fmp_concurrency,
    )

    async def fetch_and_save(ticker: str) -> None:
        nonlocal fetched_count
        ticker_path = week_dir / f"{ticker}.json"
        try:
            async with httpx.AsyncClient(limits=limits, timeout=30) as client:
                _, items = await _fetch_ticker(client, semaphore, ticker, from_str, to_str)
            ticker_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            fetched_count += len(items)
        except Exception as e:
            logger.warning(f"  [{week_label}][{ticker}] 수집 실패: {type(e).__name__}: {e}")

    if to_fetch:
        _p(f"  1-A. FMP 수집: {len(to_fetch):,}개 종목 (캐시 {skipped:,}개 스킵)...")
        # 배치로 나눠서 httpx 클라이언트 재사용
        batch_size = fmp_concurrency * 2
        for i in range(0, len(to_fetch), batch_size):
            batch = to_fetch[i:i + batch_size]
            async with httpx.AsyncClient(limits=limits, timeout=30) as client:
                async def _save(ticker: str, _client: httpx.AsyncClient = client) -> None:
                    nonlocal fetched_count
                    ticker_path = week_dir / f"{ticker}.json"
                    try:
                        _, items = await _fetch_ticker(_client, semaphore, ticker, from_str, to_str)
                        ticker_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
                        fetched_count += len(items)
                    except Exception as e:
                        logger.warning(f"  [{week_label}][{ticker}] 수집 실패: {type(e).__name__}: {e}")
                await asyncio.gather(*(_save(t) for t in batch))
    else:
        _p(f"  1-A. FMP 수집: 전체 캐시 히트 ({skipped:,}개)")

    # 캐시에서 읽어 중복제거
    raw_articles: list[dict] = []
    for p in sorted(week_dir.glob("*.json")):
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
    return deduped, raw_count, fetched_count


# ─── 주차 루프 단계 ──────────────────────────────────────────────

async def process_week(
    step_num: int,
    total_steps: int,
    week_monday: date,
    tickers: list[str],
    fmp_concurrency: int,
    concurrency: int,
    force: bool,
    stats: dict,
) -> bool:
    """1주 처리: 1-A → 1-B → 1-C → 1-D."""
    week_friday = week_monday + timedelta(days=4)
    t0 = time.monotonic()

    _p("══════════════════════════════════════════════")
    _p(f"Step {step_num}/{total_steps} — {week_monday} 주 처리 시작")

    # 1-A: FMP 수집
    try:
        deduped, raw_count, fetched_count = await fetch_week(
            week_monday, tickers, fmp_concurrency, force
        )
        _p(f"  1-A. FMP 수집 완료: 원본 {raw_count:,}건 → 중복제거 {len(deduped):,}건")
    except Exception as e:
        _p(f"  1-A. FMP 수집 실패 — 이 주 SKIP: {e}")
        return False

    # 1-B: articles 업로드
    if deduped:
        try:
            inserted = await insert_articles(deduped)
            _p(f"  1-B. articles 업로드: {inserted:,}건 INSERT ({len(deduped) - inserted:,}건 중복 스킵)")
        except Exception as e:
            _p(f"  1-B. articles 업로드 실패 — 이 주 SKIP: {e}")
            return False
    else:
        inserted = 0
        _p(f"  1-B. articles 업로드: 업로드할 기사 없음")

    # 1-C: weekly final 생성
    _p(f"  1-C. weekly final 생성 중...")
    semaphore = asyncio.Semaphore(concurrency)
    try:
        await phase1_weekly_final([week_monday], tickers, semaphore, stats)
    except Exception as e:
        _p(f"  1-C. weekly final 생성 실패: {e}")
        # 실패해도 articles는 삭제해야 하므로 계속 진행

    ok = stats["p1"]["ok"]
    fail = stats["p1"]["fail"]
    skip = stats["p1"]["skip"]
    _p(f"  1-C. weekly final 결과: ok={ok} fail={fail} skip={skip}")

    # 1-D: articles 삭제
    since_dt = datetime(week_monday.year, week_monday.month, week_monday.day, tzinfo=ET)
    until_dt = datetime(week_friday.year, week_friday.month, week_friday.day, tzinfo=ET) + timedelta(days=1)
    try:
        deleted = await delete_articles_between(since_dt, until_dt)
        _p(f"  1-D. articles 삭제: {deleted:,}건")
    except Exception as e:
        _p(f"  1-D. articles 삭제 실패 (무시): {e}")

    elapsed = time.monotonic() - t0
    _p(f"Step {step_num}/{total_steps} 완료 (소요: {_fmt_time(elapsed)})")
    _p("══════════════════════════════════════════════")
    return True


# ─── 진입점 ──────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="GoodNews 주차별 순환 백필 (수집→업로드→weekly→삭제 반복)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--weeks", type=int, default=12,
                        help="백필 주 수 (기본 12)")
    parser.add_argument("--tickers", type=str, default="",
                        help="특정 종목만 (콤마 구분). 생략 시 universe 전체")
    parser.add_argument("--force", action="store_true",
                        help="로컬 캐시 무시하고 FMP 재수집")
    parser.add_argument("--skip-benchmarks", action="store_true",
                        help="Step 0 벤치마크 수집 건너뜀")
    parser.add_argument("--skip-daily", action="store_true",
                        help="Step 마지막 daily overnight 생성 건너뜀")
    parser.add_argument("--from-week", type=str, default="",
                        help="특정 주부터 시작 (YYYY-MM-DD, 재개용)")
    parser.add_argument("--concurrency", type=int, default=25,
                        help="Gemini 동시 호출 수 (기본 25)")
    parser.add_argument("--fmp-concurrency", type=int, default=50,
                        help="FMP 동시 요청 수 (기본 50)")
    args = parser.parse_args()

    t_total = time.monotonic()
    WEEKLY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 티커 목록
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        _p(f"지정 티커 {len(tickers)}개 사용")
    else:
        tickers = await get_universe_tickers()
        if not tickers:
            _p("오류: Supabase universe_tickers 비어있음. python scripts/upload_universe_to_supabase.py 실행 필요")
            sys.exit(1)
        _p(f"유니버스 {len(tickers):,}개 티커 로드")

    # 주차 목록
    week_mondays = build_week_mondays(args.weeks)

    # --from-week 필터
    if args.from_week:
        try:
            from_dt = date.fromisoformat(args.from_week)
            week_mondays = [w for w in week_mondays if w >= from_dt]
            if not week_mondays:
                _p(f"오류: --from-week {args.from_week} 이후 주차 없음")
                sys.exit(1)
            _p(f"--from-week: {from_dt} 부터 재개 ({len(week_mondays)}주)")
        except ValueError:
            _p(f"오류: --from-week 날짜 형식 오류 (YYYY-MM-DD): {args.from_week}")
            sys.exit(1)

    total_weeks = len(week_mondays)
    _p(f"백필 범위: {week_mondays[0]} ~ {week_mondays[-1]} ({total_weeks}주)")

    # 전체 stats
    stats: dict = {
        "p1": _new_stats(), "p1_calls": 0,
        "p2": _new_stats(), "p2_calls": 0,
        "p3": _new_stats(), "p3_calls": 0,
        "p4": _new_stats(), "p4_calls": 0,
    }
    failed_weeks: list[str] = []

    # 전체 step 수 계산
    total_steps = 1 + total_weeks + 1 + (0 if args.skip_daily else 1)
    current_step = 1

    # ── Step 0: 벤치마크 수집 ────────────────────────────────────
    if not args.skip_benchmarks:
        _p("══════════════════════════════════════════════")
        ok = run_benchmarks()
        if not ok:
            _p("  Step 0 실패 — 계속 진행 (벤치마크 없이)")
    else:
        _p("Step 0 건너뜀 (--skip-benchmarks)")
    current_step += 1

    # ── Step 1~N: 주차 루프 ──────────────────────────────────────
    for week_monday in week_mondays:
        success = await process_week(
            step_num=current_step,
            total_steps=total_steps,
            week_monday=week_monday,
            tickers=tickers,
            fmp_concurrency=args.fmp_concurrency,
            concurrency=args.concurrency,
            force=args.force,
            stats=stats,
        )
        if not success:
            failed_weeks.append(week_monday.isoformat())
        current_step += 1

    # ── Step N+1: midterm 생성 ───────────────────────────────────
    _p("══════════════════════════════════════════════")
    _p(f"Step {current_step}/{total_steps} — midterm 생성 시작")
    t_mid = time.monotonic()
    try:
        macro_snapshot = await get_latest_macro_snapshot()
        macro_data = _fmt_macro(macro_snapshot)
        _p(f"  거시 데이터: {len(macro_snapshot)}개 지표 로드")
        semaphore = asyncio.Semaphore(args.concurrency)
        today = date.today()
        await phase3_midterm(tickers, today, semaphore, stats, macro_data=macro_data, all_week_mondays=week_mondays)
    except Exception as e:
        _p(f"  midterm 생성 실패: {e}")
    _p(f"Step {current_step}/{total_steps} 완료 (소요: {_fmt_time(time.monotonic() - t_mid)})")
    _p("══════════════════════════════════════════════")
    current_step += 1

    # ── Step N+2: daily overnight ────────────────────────────────
    if not args.skip_daily:
        _p("══════════════════════════════════════════════")
        _p(f"Step {current_step}/{total_steps} — daily overnight 생성 시작")
        t_daily = time.monotonic()
        try:
            semaphore = asyncio.Semaphore(args.concurrency)
            today = date.today()
            await phase4_daily_overnight(tickers, today, semaphore, stats)
        except Exception as e:
            _p(f"  daily overnight 생성 실패: {e}")
        _p(f"Step {current_step}/{total_steps} 완료 (소요: {_fmt_time(time.monotonic() - t_daily)})")
        _p("══════════════════════════════════════════════")
    else:
        _p("daily overnight 건너뜀 (--skip-daily)")

    # ── 완료 요약 ────────────────────────────────────────────────
    elapsed_total = time.monotonic() - t_total
    _p("")
    _p(f"전체 완료 (총 소요: {_fmt_time(elapsed_total)})")
    _p(f"weekly final : {stats['p1']['ok']:,}건 생성 / {stats['p1']['skip']:,}건 스킵 / {stats['p1']['fail']:,}건 실패")
    _p(f"midterm      : {stats['p3']['ok']:,}건 생성 / {stats['p3']['skip']:,}건 스킵 / {stats['p3']['fail']:,}건 실패")
    _p(f"daily ovnight: {stats['p4']['ok']:,}건 생성 / {stats['p4']['skip']:,}건 스킵 / {stats['p4']['fail']:,}건 실패")
    if failed_weeks:
        _p(f"실패한 주차: {', '.join(failed_weeks)}")
        _p(f"재개: python scripts/backfill_full.py --from-week {failed_weeks[0]} --skip-benchmarks")
    else:
        _p("실패한 주차: 없음")


if __name__ == "__main__":
    asyncio.run(main())

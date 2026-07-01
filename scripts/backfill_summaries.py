#!/usr/bin/env python3
"""
scripts/backfill_summaries.py

과거 데이터를 소급 채울 때 Gemini 호출을 최소화하면서
현재 서비스에 필요한 리포트만 생성한다.

Phase 1 — weekly final (최근 N주치, 주당 1회)
Phase 2 — sector news  (최근 N주치, 주당 1회)
Phase 3 — midterm      (종목당 1개, 최신)
Phase 4 — daily overnight (오늘 날짜 1개)

Usage:
    python scripts/backfill_summaries.py               # 전체 (Phase 1~4)
    python scripts/backfill_summaries.py --phase 1     # weekly final만
    python scripts/backfill_summaries.py --phase 2     # sector news만
    python scripts/backfill_summaries.py --phase 3     # midterm만
    python scripts/backfill_summaries.py --phase 4     # daily overnight만
    python scripts/backfill_summaries.py --tickers AAPL,NVDA
    python scripts/backfill_summaries.py --weeks 4
    python scripts/backfill_summaries.py --concurrency 5
"""

from __future__ import annotations

import argparse
import asyncio
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

from loguru import logger
from sqlalchemy import text

from app.models.database import (
    AsyncSessionLocal,
    get_articles_for_ticker_between,
    get_latest_macro_snapshot,
    get_market_news_for_week,
    get_recent_weekly_finals_for_midterm,
    get_sector_news_series,
    get_ticker_sector_exchange,
    get_weekly_benchmarks_series,
    has_weekly_final,
    insert_market_news,
    upsert_midterm,
    upsert_sector_news,
    upsert_summary,
    upsert_weekly_benchmark,
)
from app.scheduler.fmp_collector import fetch_general_news
from app.scheduler.price_collector import (
    fetch_all_weekly_price_changes,
    fetch_sector_weekly_changes,
    fetch_sp500_weekly_change,
)
from app.summarizer.llm_summarizer import (
    summarize_midterm,
    summarize_sector_news,
    summarize_ticker,
    summarize_weekly,
)
from app.universe.ticker_store import get_universe_tickers

ET = ZoneInfo("America/New_York")


def _fmt_macro(snapshot: dict) -> str:
    """get_latest_macro_snapshot() 반환값 → Part B 프롬프트용 텍스트."""
    if not snapshot:
        return "(거시 데이터 없음)"
    LABEL: dict[str, tuple[str, str]] = {
        "gdp":            ("GDP 성장률",       "분기"),
        "cpi":            ("CPI 소비자물가",    "월간"),
        "core_cpi":       ("Core CPI 근원물가", "월간"),
        "ppi":            ("PPI 생산자물가",    "월간"),
        "unemployment":   ("실업률",           "월간"),
        "nfp":            ("비농업 고용(NFP)",  "월간"),
        "fed_funds_rate": ("기준금리",         ""),
        "treasury_10y":   ("10년 국채금리",    ""),
        "ism_mfg":        ("ISM 제조업 PMI",   "월간"),
        "ism_svc":        ("ISM 서비스업 PMI", "월간"),
    }
    lines = []
    for key, (label, period) in LABEL.items():
        d = snapshot.get(key)
        if d is None:
            continue
        val = d.get("value")
        prev = d.get("previous")
        unit = d.get("unit", "%")
        date_str = d.get("date", "")
        change = ""
        if val is not None and prev is not None:
            diff = val - prev
            change = f" (전월比 {diff:+.2f}{unit})"
        period_str = f" [{period}]" if period else ""
        lines.append(f"- {label}{period_str}: {val}{unit}{change} (기준일: {date_str})")
    return "\n".join(lines)


# ─── 유틸 ────────────────────────────────────────────────────────

def _et_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ET)


def _as_date(value) -> date:
    return value.date() if hasattr(value, "date") else value


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}초"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}분 {s:02d}초"
    h, m = divmod(m, 60)
    return f"{h}시간 {m:02d}분 {s:02d}초"


def _new_stats() -> dict:
    return {"ok": 0, "skip": 0, "fail": 0}


# ─── 날짜 범위 ───────────────────────────────────────────────────

def build_week_mondays(weeks: int = 12) -> list[date]:
    """
    오늘 기준 최근 N주의 완료된 주(금요일 <= 오늘) 월요일 날짜 목록 (오래된 순).
    """
    today = date.today()
    # 가장 최근 완료된 주의 금요일
    days_to_last_friday = (today.weekday() - 4) % 7
    last_friday = today - timedelta(days=days_to_last_friday)
    last_monday = last_friday - timedelta(days=4)

    return [last_monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]


# ─── DB 존재 확인 헬퍼 ──────────────────────────────────────────

async def _sector_news_exists(week_monday: date) -> bool:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT 1 FROM sector_news_summaries WHERE week_monday = :wm LIMIT 1"),
            {"wm": week_monday},
        )
        return r.first() is not None


async def _overnight_exists(ticker: str, report_date: date) -> bool:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text(
                "SELECT 1 FROM news_summaries "
                "WHERE ticker = :t AND digest_type = 'daily' "
                "  AND report_date = :d AND version = 'overnight' LIMIT 1"
            ),
            {"t": ticker.upper(), "d": report_date},
        )
        return r.fetchone() is not None


# ─── 진행 카운터 ─────────────────────────────────────────────────

class _Counter:
    """asyncio-safe 진행 카운터 (단일 이벤트 루프 내에서만 사용)."""

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self._t0 = time.monotonic()

    def tick(self) -> tuple[int, str]:
        """done 증가 후 (done, eta_str) 반환."""
        self.done += 1
        elapsed = time.monotonic() - self._t0
        rate = self.done / elapsed if elapsed > 0 else 0
        eta = (self.total - self.done) / rate if rate > 0 and self.done < self.total else 0
        return self.done, _fmt_time(eta)


# ─── Phase 1: weekly final ───────────────────────────────────────

async def phase1_weekly_final(
    week_mondays: list[date],
    tickers: list[str],
    semaphore: asyncio.Semaphore,
    stats: dict,
) -> None:
    total_weeks = len(week_mondays)
    for week_num, week_monday in enumerate(week_mondays, 1):
        week_friday = week_monday + timedelta(days=4)
        print(f"\n[Phase 1] 주간 final 생성: {week_monday} 주 ({week_num}/{total_weeks})")

        # 벤치마크 수집 + 저장
        try:
            sp500 = await fetch_sp500_weekly_change(week_monday, week_friday)
            sector_changes = await fetch_sector_weekly_changes(week_monday, week_friday)
            await upsert_weekly_benchmark("sp500", "SP500", None, week_monday, sp500)
            for (sector, exchange), pct in sector_changes.items():
                await upsert_weekly_benchmark("sector", sector, exchange, week_monday, pct)
            logger.info(f"[Phase 1][{week_monday}] 벤치마크 저장 완료 (sp500={sp500})")
        except Exception as e:
            logger.warning(f"[Phase 1][{week_monday}] 벤치마크 수집 실패 (스킵): {e}")
            sp500 = None
            sector_changes = {}

        # 종목별 가격 변동률
        try:
            price_changes = await fetch_all_weekly_price_changes(tickers, week_monday, week_friday)
        except Exception as e:
            logger.warning(f"[Phase 1][{week_monday}] 가격 수집 실패: {e}")
            price_changes = {}

        since_dt = _et_midnight(week_monday)
        until_dt = _et_midnight(week_friday + timedelta(days=1))
        counter = _Counter(len(tickers))

        async def _one_ticker(ticker: str) -> None:
            async with semaphore:
                try:
                    if await has_weekly_final(ticker, week_monday):
                        stats["p1"]["skip"] += 1
                        done, eta = counter.tick()
                        if done % 100 == 0 or done == counter.total:
                            print(f"[Phase 1] 진행 {done:,}/{counter.total:,} | 예상 남은 시간 {eta}")
                        return

                    raw = await get_articles_for_ticker_between(ticker, since_dt, until_dt)
                    if not raw:
                        stats["p1"]["skip"] += 1
                        done, eta = counter.tick()
                        if done % 100 == 0 or done == counter.total:
                            print(f"[Phase 1] 진행 {done:,}/{counter.total:,} | 예상 남은 시간 {eta}")
                        return

                    summary = await asyncio.to_thread(summarize_weekly, ticker, raw_articles=raw)
                    if summary is None:
                        logger.warning(f"[Phase 1][{ticker}][{week_monday}] LLM 반환 None")
                        stats["p1"]["fail"] += 1
                        done, eta = counter.tick()
                        if done % 100 == 0 or done == counter.total:
                            print(f"[Phase 1] 진행 {done:,}/{counter.total:,} | 예상 남은 시간 {eta}")
                        return

                    pct = price_changes.get(ticker)
                    await upsert_summary(
                        ticker=ticker,
                        digest_type="weekly",
                        report_date=week_monday,
                        version="final",
                        summary_text=summary["summary_text"],
                        sentiment=summary["sentiment"],
                        source_urls=[],
                        price_change_pct=pct,
                    )
                    stats["p1"]["ok"] += 1
                    stats["p1_calls"] += 1

                except Exception as e:
                    logger.warning(f"[Phase 1][{ticker}][{week_monday}] 예외: {e}")
                    stats["p1"]["fail"] += 1

            done, eta = counter.tick()
            if done % 100 == 0 or done == counter.total:
                print(
                    f"[Phase 1] {ticker} 완료 | "
                    f"진행 {done:,}/{counter.total:,} | "
                    f"예상 남은 시간 {eta}"
                )

        await asyncio.gather(*(_one_ticker(t) for t in tickers))
        logger.info(
            f"[Phase 1][{week_monday}] "
            f"ok={stats['p1']['ok']} skip={stats['p1']['skip']} fail={stats['p1']['fail']}"
        )


# ─── Phase 2: sector news ────────────────────────────────────────

async def phase2_sector_news(
    week_mondays: list[date],
    stats: dict,
) -> None:
    total = len(week_mondays)
    for i, week_monday in enumerate(week_mondays, 1):
        week_friday = week_monday + timedelta(days=4)
        print(f"\n[Phase 2] 섹터뉴스 생성: {week_monday} 주 ({i}/{total})")

        if await _sector_news_exists(week_monday):
            logger.info(f"[Phase 2] {week_monday} 이미 존재, 스킵")
            stats["p2"]["skip"] += 1
            continue

        # DB에 일반 뉴스 없으면 FMP에서 수집
        existing = await get_market_news_for_week(week_monday, week_friday)
        if not existing:
            try:
                raw = await fetch_general_news(week_monday.isoformat(), week_friday.isoformat())
                if raw:
                    inserted = await insert_market_news(raw)
                    logger.info(f"[Phase 2] {week_monday} 일반뉴스 {inserted}건 INSERT")
            except Exception as e:
                logger.warning(f"[Phase 2] {week_monday} 일반뉴스 수집 실패: {e}")

        articles = await get_market_news_for_week(week_monday, week_friday)
        if not articles:
            logger.warning(f"[Phase 2] {week_monday} 시장뉴스 없음, 스킵")
            stats["p2"]["skip"] += 1
            continue

        try:
            result = await asyncio.to_thread(summarize_sector_news, articles)
        except Exception as e:
            logger.warning(f"[Phase 2] {week_monday} summarize 실패: {e}")
            result = None

        if not result:
            logger.warning(f"[Phase 2] {week_monday} 생성 실패")
            stats["p2"]["fail"] += 1
            continue

        for category, data in result.items():
            await upsert_sector_news(
                category=category,
                week_monday=week_monday,
                summary_text=data["summary_text"],
                sentiment=data["sentiment"],
            )
        stats["p2"]["ok"] += 1
        stats["p2_calls"] += 1
        print(f"[Phase 2] {week_monday} 완료 — {len(result)}개 카테고리")


# ─── Phase 3: midterm ────────────────────────────────────────────

async def phase3_midterm(
    tickers: list[str],
    today: date,
    semaphore: asyncio.Semaphore,
    stats: dict,
    macro_data: str = "",
) -> None:
    print(f"\n[Phase 3] midterm 생성 시작: {len(tickers):,}개 종목")
    counter = _Counter(len(tickers))

    async def _one_ticker(ticker: str) -> None:
        async with semaphore:
            try:
                weekly_reports = await get_recent_weekly_finals_for_midterm(ticker, before=today)
                if not weekly_reports:
                    stats["p3"]["skip"] += 1
                    done, eta = counter.tick()
                    if done % 100 == 0 or done == counter.total:
                        print(f"[Phase 3] midterm 생성: 진행 {done:,}/{counter.total:,} | 남은 시간 {eta}")
                    return

                sector_info = await get_ticker_sector_exchange(ticker)
                if sector_info is None:
                    stats["p3"]["skip"] += 1
                    done, eta = counter.tick()
                    if done % 100 == 0 or done == counter.total:
                        print(f"[Phase 3] midterm 생성: 진행 {done:,}/{counter.total:,} | 남은 시간 {eta}")
                    return

                sector_name, exchange = sector_info
                week_mondays = [_as_date(r["week_monday"]) for r in weekly_reports]
                benchmarks = await get_weekly_benchmarks_series(week_mondays, sector_name, exchange)
                sector_news = await get_sector_news_series(sector_name, week_mondays)

                result = await asyncio.to_thread(
                    summarize_midterm,
                    ticker=ticker,
                    weekly_reports=weekly_reports,
                    sp500_series=benchmarks["sp500"],
                    sector_series=benchmarks["sector"],
                    sector_name=sector_name,
                    exchange=exchange,
                    sector_news=sector_news,
                    macro_data=macro_data,
                )
                if result is None:
                    logger.warning(f"[Phase 3][{ticker}] LLM 반환 None")
                    stats["p3"]["fail"] += 1
                    done, eta = counter.tick()
                    if done % 100 == 0 or done == counter.total:
                        print(f"[Phase 3] midterm 생성: 진행 {done:,}/{counter.total:,} | 남은 시간 {eta}")
                    return

                report_date = max(week_mondays)
                await upsert_midterm(
                    ticker=ticker,
                    report_date=report_date,
                    summary_text=result["summary_text"],
                    sentiment=result["sentiment"],
                    price_change_pct=result["price_change_pct"],
                )
                stats["p3"]["ok"] += 1
                stats["p3_calls"] += 1

            except Exception as e:
                logger.warning(f"[Phase 3][{ticker}] 예외: {e}")
                stats["p3"]["fail"] += 1

        done, eta = counter.tick()
        if done % 100 == 0 or done == counter.total:
            print(f"[Phase 3] midterm 생성: 진행 {done:,}/{counter.total:,} | 남은 시간 {eta}")

    await asyncio.gather(*(_one_ticker(t) for t in tickers))
    logger.info(
        f"[Phase 3] 완료: ok={stats['p3']['ok']} "
        f"skip={stats['p3']['skip']} fail={stats['p3']['fail']}"
    )


# ─── Phase 4: daily overnight ────────────────────────────────────

async def phase4_daily_overnight(
    tickers: list[str],
    today: date,
    semaphore: asyncio.Semaphore,
    stats: dict,
) -> None:
    print(f"\n[Phase 4] daily overnight: {today}")
    since_dt = _et_midnight(today)
    until_dt = _et_midnight(today + timedelta(days=1))
    counter = _Counter(len(tickers))

    async def _one_ticker(ticker: str) -> None:
        async with semaphore:
            try:
                if await _overnight_exists(ticker, today):
                    stats["p4"]["skip"] += 1
                    done, eta = counter.tick()
                    if done % 100 == 0 or done == counter.total:
                        print(f"[Phase 4] 진행 {done:,}/{counter.total:,} | 남은 시간 {eta}")
                    return

                articles = await get_articles_for_ticker_between(ticker, since_dt, until_dt)
                if not articles:
                    stats["p4"]["skip"] += 1
                    done, eta = counter.tick()
                    if done % 100 == 0 or done == counter.total:
                        print(f"[Phase 4] 진행 {done:,}/{counter.total:,} | 남은 시간 {eta}")
                    return

                result = await asyncio.to_thread(summarize_ticker, ticker, articles, "daily")
                if result is None:
                    logger.warning(f"[Phase 4][{ticker}] LLM 반환 None")
                    stats["p4"]["fail"] += 1
                    done, eta = counter.tick()
                    if done % 100 == 0 or done == counter.total:
                        print(f"[Phase 4] 진행 {done:,}/{counter.total:,} | 남은 시간 {eta}")
                    return

                await upsert_summary(
                    ticker=ticker,
                    digest_type="daily",
                    report_date=today,
                    version="overnight",
                    summary_text=result["summary_text"],
                    sentiment=result["sentiment"],
                    source_urls=result["source_urls"],
                )
                stats["p4"]["ok"] += 1
                stats["p4_calls"] += 1

            except Exception as e:
                logger.warning(f"[Phase 4][{ticker}] 예외: {e}")
                stats["p4"]["fail"] += 1

        done, eta = counter.tick()
        if done % 100 == 0 or done == counter.total:
            print(f"[Phase 4] 진행 {done:,}/{counter.total:,} | 남은 시간 {eta}")

    await asyncio.gather(*(_one_ticker(t) for t in tickers))
    logger.info(
        f"[Phase 4] 완료: ok={stats['p4']['ok']} "
        f"skip={stats['p4']['skip']} fail={stats['p4']['fail']}"
    )


# ─── 비용 요약 출력 ──────────────────────────────────────────────

def _print_cost_summary(tickers: list[str], weeks: int, stats: dict, phases: list[int]) -> None:
    n = len(tickers)
    sep = "=" * 60
    print(f"\n{sep}")
    print("  Gemini 호출 횟수 요약")
    print(sep)
    if 1 in phases:
        max_p1 = n * weeks
        print(f"  weekly final  : {n:,}종목 × {weeks}주 = 최대 {max_p1:,}회 / 실제 {stats['p1_calls']:,}회")
    if 2 in phases:
        max_p2 = weeks  # 주당 1회 (1 LLM call for all sectors)
        print(f"  sector news   : {weeks}주 = 최대 {max_p2}회 / 실제 {stats['p2_calls']}회")
    if 3 in phases:
        print(f"  midterm       : 최대 {n:,}회 / 실제 {stats['p3_calls']:,}회")
    if 4 in phases:
        print(f"  daily         : 최대 {n:,}회 / 실제 {stats['p4_calls']:,}회")

    total_max = sum([
        n * weeks if 1 in phases else 0,
        weeks if 2 in phases else 0,
        n if 3 in phases else 0,
        n if 4 in phases else 0,
    ])
    total_actual = sum(stats[k] for k in ("p1_calls", "p2_calls", "p3_calls", "p4_calls"))
    print(f"  합계          : 최대 {total_max:,}회 / 실제 {total_actual:,}회")
    print(sep)

    print("\n  Phase별 결과")
    print(sep)
    labels = {1: "weekly final", 2: "sector news ", 3: "midterm     ", 4: "daily ovnt  "}
    keys = {1: "p1", 2: "p2", 3: "p3", 4: "p4"}
    for p in phases:
        s = stats[keys[p]]
        print(f"  Phase {p} ({labels[p]}):  ok={s['ok']:>5,}  skip={s['skip']:>5,}  fail={s['fail']:>5,}")
    print(sep)


# ─── 진입점 ──────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="GoodNews 백필 요약 생성기 (Phase 1~4)"
    )
    parser.add_argument(
        "--phase", type=int, choices=[1, 2, 3, 4], default=None,
        help="실행할 Phase 번호 (생략 시 1~4 전체 실행)",
    )
    parser.add_argument(
        "--tickers", type=str, default="",
        help="특정 종목만 (콤마 구분). 생략 시 universe 전체",
    )
    parser.add_argument(
        "--weeks", type=int, default=12,
        help="대상 주 수 (Phase 1, 2 적용). 기본 12",
    )
    parser.add_argument(
        "--concurrency", type=int, default=25,
        help="종목 단위 동시 LLM 호출 수. Gemini Tier 1이면 20~30 권장. 기본 25",
    )
    args = parser.parse_args()

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

    today = date.today()
    week_mondays = build_week_mondays(args.weeks)
    phases = [args.phase] if args.phase else [1, 2, 3, 4]

    logger.info(f"실행 Phase: {phases}")
    logger.info(f"대상 종목: {len(tickers):,}개")
    logger.info(f"날짜 범위: {week_mondays[0]} ~ {week_mondays[-1]} ({len(week_mondays)}주)")

    semaphore = asyncio.Semaphore(args.concurrency)

    # 거시 데이터: Phase 3 Part B에서 공통 사용 — 한 번만 조회
    macro_snapshot = await get_latest_macro_snapshot()
    macro_data = _fmt_macro(macro_snapshot)
    logger.info(f"거시 데이터 로드: {len(macro_snapshot)}개 지표")

    stats: dict = {
        "p1": _new_stats(), "p1_calls": 0,
        "p2": _new_stats(), "p2_calls": 0,
        "p3": _new_stats(), "p3_calls": 0,
        "p4": _new_stats(), "p4_calls": 0,
    }

    t_total = time.monotonic()

    if 1 in phases:
        await phase1_weekly_final(week_mondays, tickers, semaphore, stats)

    if 2 in phases:
        await phase2_sector_news(week_mondays, stats)

    if 3 in phases:
        await phase3_midterm(tickers, today, semaphore, stats, macro_data=macro_data)

    if 4 in phases:
        await phase4_daily_overnight(tickers, today, semaphore, stats)

    elapsed = time.monotonic() - t_total
    logger.info(f"전체 소요 시간: {_fmt_time(elapsed)}")

    _print_cost_summary(tickers, args.weeks, stats, phases)


if __name__ == "__main__":
    asyncio.run(main())

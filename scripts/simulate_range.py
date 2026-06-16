#!/usr/bin/env python3
"""
GoodNews AI - 기간 시뮬레이션 스크립트
========================================
지정한 날짜 구간(start ~ end)을 하루씩 순회하며,
이미 DB(articles 테이블)에 적재된 뉴스 데이터를 사용해
daily(closing) -> weekly(draft/final) 리포트를
실제 배치와 동일한 로직/순서로 생성합니다.

용도
----
1. 시뮬레이션      : 전체 파이프라인이 순서대로 잘 도는지 검증
2. 데이터 복구      : 백필은 됐지만 요약이 비어있는 기간을 채울 때
3. 초기 부트스트랩  : 서비스 시작 시 과거 N개월치 리포트를 미리 생성
4. 향후 실험        : 프롬프트/로직 변경 후 특정 기간을 재실행해 결과 비교

중요
----
- 라이브 FMP 뉴스 수집은 하지 않습니다. articles 테이블에 이미 있는
  데이터(백필 결과)를 그대로 사용합니다.
- 주간 가격 변동률(S&P500/섹터/종목)과 주간 일반 시장뉴스는 FMP에
  실시간으로 요청합니다 (과거 날짜의 시세/뉴스는 FMP가 지원).
- 같은 (ticker, digest_type, report_date)에 대해 upsert 이므로,
  같은 구간을 여러 번 실행해도 마지막 실행 결과로 덮어씁니다
  (재실행/복구에 안전).

--dry-run 옵션
--------------
운영 DB에 절대 쓰지 않고, 결과를 로컬 파일로만 저장합니다.
    저장 위치: sim_results/dry_run_{start}_{end}/
    - {ticker}/daily_{report_date}_{version}.json
    - {ticker}/weekly_{week_monday}_{draft|final}.json
    - sector_news/{week_monday}.json
    - benchmarks/{week_monday}.json
    - usage_summary.json  (모델별·digest_type별 호출/토큰 집계)

    DB 읽기/FMP 외부 API는 그대로 수행됩니다 (쓰기만 차단).

사용법
------
    # dry-run으로 3종목 1주일
    python scripts/simulate_range.py --start 2026-06-09 --end 2026-06-13 \\
        --tickers AAPL,NVDA,NKE --dry-run

    # 운영 DB에 저장 (기존 방식)
    python scripts/simulate_range.py --start 2026-05-01 --end 2026-05-31 \\
        --tickers AAPL,NVDA,MSFT,NKE,BAC

    # 섹터뉴스 생략 (빠른 daily/weekly 검증용)
    python scripts/simulate_range.py --start 2026-05-01 --end 2026-05-07 \\
        --tickers AAPL --no-sector-news --dry-run
"""

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

# Windows 콘솔(cp949) 출력 깨짐 방지
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from loguru import logger

from app.models.database import (
    get_articles_for_ticker_between,
    get_daily_reports,
    get_last_midterm_date,
    get_market_news_for_week,
    get_recent_weekly_finals_for_midterm,
    get_sector_news_series,
    get_ticker_sector_exchange,
    get_weekly_benchmarks_series,
    get_weekly_draft,
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
from app.scheduler.tasks import should_generate_midterm
from app.summarizer.llm_summarizer import (
    summarize_midterm,
    summarize_sector_news,
    summarize_ticker,
    summarize_weekly,
    summarize_weekly_update,
)

ET = ZoneInfo("America/New_York")
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

def _et_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ET)

def _as_date(value) -> date:
    return value.date() if hasattr(value, "date") else value

def _bump(stats: dict, key: str, result: str) -> None:
    stats[key][result] += 1

def _new_stats() -> dict:
    return {"ok": 0, "skip": 0, "fail": 0}

# ──────────────────────────────────────────
# DryRunContext: 로컬 저장 + 사용량 추적
# ──────────────────────────────────────────

class DryRunContext:
    """
    --dry-run 모드에서 DB 쓰기 대신 로컬 파일에 결과를 저장하고
    LLM 호출 사용량을 집계한다.
    """

    def __init__(self, out_dir: Path, tickers: list[str], start: date, end: date):
        self.out_dir = out_dir
        self.tickers = tickers
        self.start = start
        self.end = end
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # usage tracking: {model: {calls, input_tokens, output_tokens}}
        self._by_model: dict[str, dict] = {}
        # usage tracking: {digest_type: {calls, input_tokens, output_tokens}}
        self._by_digest: dict[str, dict] = {}
        # current active digest_type (set just before each summarize call)
        self.active_digest_type: str = "unknown"


    # ── LLM 사용량 기록 ──

    def record_llm(
        self,
        model: str,
        input_chars: int,
        output_chars: int,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        inp = input_tokens if input_tokens is not None else max(1, input_chars // 4)
        out = output_tokens if output_tokens is not None else max(1, output_chars // 4)
        digest = self.active_digest_type

        for bucket, key in [(self._by_model, model), (self._by_digest, digest)]:
            if key not in bucket:
                bucket[key] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
            bucket[key]["calls"] += 1
            bucket[key]["input_tokens"] += inp
            bucket[key]["output_tokens"] += out

    # ── 파일 저장 헬퍼 ──

    def _write(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_daily(
        self,
        ticker: str,
        report_date: date,
        version: str,
        summary_text: str,
        sentiment: str,
        source_urls: list,
    ) -> None:
        path = self.out_dir / ticker / f"daily_{report_date}_{version}.json"
        self._write(path, {
            "ticker": ticker,
            "report_date": str(report_date),
            "version": version,
            "summary_text": summary_text,
            "sentiment": sentiment,
            "source_urls": source_urls,
        })

    def save_weekly(
        self,
        ticker: str,
        week_monday: date,
        version: str,
        summary_text: str,
        sentiment: str,
        price_change_pct: float | None = None,
    ) -> None:
        path = self.out_dir / ticker / f"weekly_{week_monday}_{version}.json"
        data: dict = {
            "ticker": ticker,
            "week_monday": str(week_monday),
            "version": version,
            "summary_text": summary_text,
            "sentiment": sentiment,
        }
        if price_change_pct is not None:
            data["price_change_pct"] = price_change_pct
        self._write(path, data)

    def save_midterm(
        self,
        ticker: str,
        week_monday: date,
        summary_text: str,
        sentiment: str | None,
        price_change_pct: float | None,
    ) -> None:
        path = self.out_dir / ticker / f"midterm_{week_monday}.json"
        self._write(path, {
            "ticker": ticker,
            "week_monday": str(week_monday),
            "summary_text": summary_text,
            "sentiment": sentiment,
            "price_change_pct": price_change_pct,
        })

    def save_sector_news(self, week_monday: date, sector_summaries: dict) -> None:
        path = self.out_dir / "sector_news" / f"{week_monday}.json"
        self._write(path, {
            "week_monday": str(week_monday),
            "categories": sector_summaries,
        })

    def save_benchmark(
        self,
        week_monday: date,
        sp500_change: float | None,
        sector_changes: dict,
    ) -> None:
        path = self.out_dir / "benchmarks" / f"{week_monday}.json"
        self._write(path, {
            "week_monday": str(week_monday),
            "sp500": sp500_change,
            "sectors": {
                f"{sec}|{exch}": pct for (sec, exch), pct in sector_changes.items()
            },
        })

    # ── 최종 집계 출력 & 저장 ──

    def finalize(self, stats: dict) -> None:
        summary = {
            "tickers": self.tickers,
            "period": {"start": str(self.start), "end": str(self.end)},
            "by_model": self._by_model,
            "by_digest_type": self._by_digest,
            "stats": stats,
        }
        path = self.out_dir / "usage_summary.json"
        self._write(path, summary)

        total_calls = sum(v["calls"] for v in self._by_model.values())
        total_inp   = sum(v["input_tokens"] for v in self._by_model.values())
        total_out   = sum(v["output_tokens"] for v in self._by_model.values())

        sep = "=" * 62
        print(f"\n{sep}")
        print("  DRY-RUN LLM 사용량 요약 (토큰 추정: 글자수/4)")
        print(sep)
        print(f"  총 호출:  {total_calls:>6}회")
        print(f"  입력 토큰:{total_inp:>10,}")
        print(f"  출력 토큰:{total_out:>10,}")
        print()
        print("  [모델별]")
        for model, d in self._by_model.items():
            print(f"    {model}")
            print(f"      호출 {d['calls']}회 / 입력 {d['input_tokens']:,} / 출력 {d['output_tokens']:,} 토큰")
        print()
        print("  [digest_type별]")
        for dt, d in self._by_digest.items():
            print(f"    {dt:<18}  호출 {d['calls']:>4}회 / 입력 {d['input_tokens']:>8,} / 출력 {d['output_tokens']:>8,} 토큰")
        print()
        print("  [파이프라인 통계]")
        for stage, s in stats.items():
            tmpl = f"  template={s.get('template', 0):>3}" if "template" in s else ""
            print(
                f"    {stage:<18}  ok={s.get('ok',0):>4}{tmpl}"
                f"  skip={s.get('skip',0):>4}  fail={s.get('fail',0):>4}"
            )
        print(f"\n  저장: {path}")
        print(f"  출력 폴더: {self.out_dir}")
        print(sep)

# ──────────────────────────────────────────
# LLM 사용량 인터셉트 (monkey-patch)
# ──────────────────────────────────────────

def _install_llm_hook(ctx: DryRunContext) -> None:
    """
    app.summarizer.llm_summarizer._generate_content 를 래핑하여
    모든 Gemini 호출의 입출력 글자 수를 ctx 에 기록한다.
    llm_summarizer 내부 함수들은 모듈 globals 에서 _generate_content 를
    조회하므로, 모듈 dict를 교체하면 모든 호출이 자동으로 경유된다.
    """
    import app.summarizer.llm_summarizer as _llm_mod
    from app.core.config import get_settings

    _orig = _llm_mod._generate_content

    def _wrapped(prompt: str, ticker: str = "", model: str | None = None) -> str | None:
        settings = get_settings()
        actual_model = model or settings.gemini_model
        result = _orig(prompt, ticker, model)
        ctx.record_llm(
            model=actual_model,
            input_chars=len(prompt),
            output_chars=len(result) if result else 0,
        )
        return result

    _llm_mod._generate_content = _wrapped
    logger.info("[DRY-RUN] LLM 사용량 인터셉터 설치됨")

# ──────────────────────────────────────────
# Daily: closing
# ──────────────────────────────────────────

async def sim_daily_closing(
    ticker: str, day: date, ctx: DryRunContext | None = None
) -> str:
    """day(ET 00:00~24:00) 뉴스로 daily/closing 리포트 생성. 반환: ok/skip/fail"""
    since = _et_midnight(day)
    until = _et_midnight(day + timedelta(days=1))
    articles = await get_articles_for_ticker_between(ticker, since, until)
    if not articles:
        return "skip"

    if ctx:
        ctx.active_digest_type = "daily"
    result = summarize_ticker(ticker, articles, "daily")
    if result is None:
        return "fail"

    if ctx:
        ctx.save_daily(
            ticker, day, "closing",
            result["summary_text"], result["sentiment"], result["source_urls"],
        )
    else:
        await upsert_summary(
            ticker=ticker,
            digest_type="daily",
            report_date=day,
            version="closing",
            summary_text=result["summary_text"],
            sentiment=result["sentiment"],
            source_urls=result["source_urls"],
        )
    return "ok"

# ──────────────────────────────────────────
# Weekly: draft (월요일) / final (금요일)
# ──────────────────────────────────────────

async def sim_weekly_draft(
    ticker: str, week_monday: date, ctx: DryRunContext | None = None
) -> str:
    since_date = week_monday - timedelta(days=7)
    until_date = week_monday
    dailies = await get_daily_reports(ticker, since_date, until_date)

    raw = []
    if len(dailies) < 3:
        raw = await get_articles_for_ticker_between(
            ticker, _et_midnight(since_date), _et_midnight(until_date)
        )

    if not dailies and not raw:
        return "skip"

    if ctx:
        ctx.active_digest_type = "weekly_draft"
    summary = summarize_weekly(ticker, daily_reports=dailies, raw_articles=raw)
    if summary is None:
        return "fail"

    if ctx:
        ctx.save_weekly(
            ticker, week_monday, "draft",
            summary["summary_text"], summary["sentiment"],
        )
    else:
        await upsert_summary(
            ticker=ticker,
            digest_type="weekly",
            report_date=week_monday,
            version="draft",
            summary_text=summary["summary_text"],
            sentiment=summary["sentiment"],
            source_urls=[],
        )
    return "ok"

async def sim_weekly_final(
    ticker: str,
    week_monday: date,
    week_friday: date,
    price_changes: dict[str, float],
    ctx: DryRunContext | None = None,
) -> str:
    since_dt = _et_midnight(week_monday)
    until_dt = _et_midnight(week_friday + timedelta(days=1))

    draft = await get_weekly_draft(ticker, week_monday)
    this_week_dailies = await get_daily_reports(ticker, week_monday, week_friday)

    if ctx:
        ctx.active_digest_type = "weekly_final"

    if draft:
        summary = summarize_weekly_update(
            ticker=ticker,
            draft_report=draft["summary_text"],
            daily_reports=this_week_dailies,
        )
    elif this_week_dailies:
        summary = summarize_weekly(ticker, daily_reports=this_week_dailies)
    else:
        raw = await get_articles_for_ticker_between(ticker, since_dt, until_dt)
        if not raw:
            return "skip"
        summary = summarize_weekly(ticker, raw_articles=raw)

    if summary is None:
        return "fail"

    pct = price_changes.get(ticker)
    if ctx:
        ctx.save_weekly(
            ticker, week_monday, "final",
            summary["summary_text"], summary["sentiment"],
            price_change_pct=pct,
        )
    else:
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
    return "ok"

# ──────────────────────────────────────────
# Weekly: 섹터별 시장 뉴스 (best-effort)
# ──────────────────────────────────────────

async def sim_weekly_sector_news(
    week_monday: date,
    week_friday: date,
    ctx: DryRunContext | None = None,
) -> int:
    try:
        raw = await fetch_general_news(week_monday.isoformat(), week_friday.isoformat())
        if raw:
            if ctx:
                logger.info(
                    f"[DRY-RUN][SECTOR-NEWS] insert_market_news 스킵 ({len(raw)}건 수집됨)"
                )
            else:
                inserted = await insert_market_news(raw)
                logger.info(
                    f"[SECTOR-NEWS] market_news INSERT {inserted}건 (수집 {len(raw)}건)"
                )

        articles = await get_market_news_for_week(week_monday, week_friday)
        if not articles:
            logger.warning(f"[SECTOR-NEWS] {week_monday} 주 시장 뉴스 없음 -> 스킵")
            return 0

        if ctx:
            ctx.active_digest_type = "sector_news"
        sector_summaries = summarize_sector_news(articles)
        if not sector_summaries:
            logger.warning(f"[SECTOR-NEWS] {week_monday} 요약 생성 실패")
            return 0

        if ctx:
            ctx.save_sector_news(week_monday, sector_summaries)
        else:
            for category, data in sector_summaries.items():
                await upsert_sector_news(
                    category=category,
                    week_monday=week_monday,
                    summary_text=data["summary_text"],
                    sentiment=data["sentiment"],
                )
        return len(sector_summaries)
    except Exception as e:
        logger.warning(f"[SECTOR-NEWS] {week_monday} 실패 (스킵): {e}")
        return 0

# ──────────────────────────────────────────
# Midterm: 중장기 리포트
# ──────────────────────────────────────────

async def sim_midterm(
    ticker: str,
    week_monday: date,
    dry_run: bool = False,
    ctx: "DryRunContext | None" = None,
) -> str:
    """
    중장기 리포트 생성. 반환: "ok" / "skip" / "template" / "fail"

    dry_run=True 이면 DB upsert 대신 로컬 파일에 저장.
    ctx 가 있으면 LLM 사용량 추적을 위해 active_digest_type 을 설정한다.
    """
    prev_monday = week_monday - timedelta(days=7)

    this_has_final = await has_weekly_final(ticker, week_monday)
    prev_has_final = await has_weekly_final(ticker, prev_monday)
    last_mid = await get_last_midterm_date(ticker)

    if not should_generate_midterm(ticker, week_monday, this_has_final, prev_has_final, last_mid):
        return "skip"

    weekly_reports = await get_recent_weekly_finals_for_midterm(ticker, before=week_monday)
    if not weekly_reports:
        return "skip"

    sector_info = await get_ticker_sector_exchange(ticker)
    if sector_info is None:
        return "skip"
    sector_name, exchange = sector_info

    week_mondays = [
        _as_date(r["week_monday"]) for r in weekly_reports
    ]
    benchmarks = await get_weekly_benchmarks_series(week_mondays, sector_name, exchange)
    sector_news = await get_sector_news_series(sector_name, week_mondays)

    if ctx:
        ctx.active_digest_type = "midterm"

    result = summarize_midterm(
        ticker=ticker,
        weekly_reports=weekly_reports,
        sp500_series=benchmarks["sp500"],
        sector_series=benchmarks["sector"],
        sector_name=sector_name,
        exchange=exchange,
        sector_news=sector_news,
    )
    if result is None:
        return "skip"

    if dry_run and ctx:
        ctx.save_midterm(
            ticker, week_monday,
            result["summary_text"], result["sentiment"], result["price_change_pct"],
        )
    elif not dry_run:
        await upsert_midterm(
            ticker=ticker,
            report_date=week_monday,
            summary_text=result["summary_text"],
            sentiment=result["sentiment"],
            price_change_pct=result["price_change_pct"],
        )

    return "template" if result["sentiment"] is None else "ok"


# ──────────────────────────────────────────
# 메인 루프
# ──────────────────────────────────────────

async def run(
    start: date,
    end: date,
    tickers: list[str],
    do_sector_news: bool = True,
    do_midterm: bool = True,
    ctx: "DryRunContext | None" = None,
):
    stats: dict = {
        "daily": _new_stats(),
        "weekly_draft": _new_stats(),
        "weekly_final": _new_stats(),
        "midterm": {**_new_stats(), "template": 0},
    }

    if ctx:
        logger.info(f"[DRY-RUN] 출력 폴더: {ctx.out_dir}")

    cur = start
    while cur <= end:
        weekday = cur.weekday()  # 0=월 ... 6=일
        logger.info(f"\n===== {cur.isoformat()} ({WEEKDAY_KR[weekday]}) =====")

        # 1) Daily closing - 매일
        for ticker in tickers:
            r = await sim_daily_closing(ticker, cur, ctx)
            _bump(stats, "daily", r)
        logger.info(f"[DAILY] {cur} 누적: {stats['daily']}")

        # 2) Weekly draft - 월요일
        if weekday == 0:
            week_monday = cur
            for ticker in tickers:
                r = await sim_weekly_draft(ticker, week_monday, ctx)
                _bump(stats, "weekly_draft", r)
            logger.info(f"[WEEKLY-DRAFT] {week_monday} 누적: {stats['weekly_draft']}")

        # 3) Weekly final - 금요일 (+가격 벤치마크, +섹터뉴스)
        if weekday == 4:
            week_monday = cur - timedelta(days=4)
            week_friday = cur

            sp500_change = await fetch_sp500_weekly_change(week_monday, week_friday)
            sector_changes = await fetch_sector_weekly_changes(week_monday, week_friday)

            if ctx:
                ctx.save_benchmark(week_monday, sp500_change, sector_changes)
            else:
                await upsert_weekly_benchmark("sp500", "SP500", None, week_monday, sp500_change)
                for (sector, exchange), pct in sector_changes.items():
                    await upsert_weekly_benchmark("sector", sector, exchange, week_monday, pct)

            price_changes = await fetch_all_weekly_price_changes(
                tickers, week_monday, week_friday
            )

            for ticker in tickers:
                r = await sim_weekly_final(ticker, week_monday, week_friday, price_changes, ctx)
                _bump(stats, "weekly_final", r)
            logger.info(f"[WEEKLY-FINAL] {week_monday} 누적: {stats['weekly_final']}")

            if do_midterm:
                dry_run = ctx is not None
                for ticker in tickers:
                    try:
                        r = await sim_midterm(ticker, week_monday, dry_run=dry_run, ctx=ctx)
                    except Exception as e:
                        logger.warning(f"[MIDTERM][{ticker}] 실패 (스킵): {e}")
                        r = "fail"
                    if r == "template":
                        stats["midterm"]["template"] += 1
                        stats["midterm"]["ok"] += 1
                    else:
                        _bump(stats, "midterm", r)
                logger.info(
                    f"[MIDTERM] {week_monday} 누적: "
                    f"ok={stats['midterm']['ok']} "
                    f"(template={stats['midterm']['template']}) "
                    f"skip={stats['midterm']['skip']} "
                    f"fail={stats['midterm']['fail']}"
                )

            if do_sector_news:
                n = await sim_weekly_sector_news(week_monday, week_friday, ctx)
                logger.info(f"[SECTOR-NEWS] {week_monday}: {n}개 카테고리 생성")

        cur += timedelta(days=1)

    logger.info("\n===== 시뮬레이션 완료 =====")
    logger.info(f"daily        : {stats['daily']}")
    logger.info(f"weekly_draft : {stats['weekly_draft']}")
    logger.info(f"weekly_final : {stats['weekly_final']}")
    logger.info(
        f"midterm      : ok={stats['midterm']['ok']} "
        f"(template={stats['midterm']['template']}) "
        f"skip={stats['midterm']['skip']} "
        f"fail={stats['midterm']['fail']}"
    )

    if ctx:
        ctx.finalize(stats)

def main():
    parser = argparse.ArgumentParser(description="GoodNews AI 기간 시뮬레이션")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--tickers",
        default="AAPL,NVDA,MSFT,NKE,BAC",
        help="쉼표로 구분된 티커 목록 (기본: AAPL,NVDA,MSFT,NKE,BAC)",
    )
    parser.add_argument(
        "--universe", action="store_true",
        help="universe_current.csv 의 전체 종목 사용 (시간/비용 매우 큼)",
    )
    parser.add_argument("--no-sector-news", action="store_true", help="섹터 뉴스 단계 생략")
    parser.add_argument("--no-midterm", action="store_true", help="midterm 리포트 생성 생략")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="운영 DB에 쓰지 않고 sim_results/dry_run_*/에 로컬 파일로 저장",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if start > end:
        raise SystemExit("--start 가 --end 보다 늦을 수 없습니다.")

    if args.universe:
        from app.universe.ticker_store import get_universe_tickers
        tickers = get_universe_tickers()
        logger.warning(f"전체 유니버스 {len(tickers)}개 종목으로 실행합니다 (시간/비용 주의).")
    else:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    logger.info(f"시뮬레이션 기간: {start} ~ {end} ({(end-start).days + 1}일)")
    logger.info(f"대상 종목 ({len(tickers)}개): {', '.join(tickers[:20])}"
                + (" ..." if len(tickers) > 20 else ""))

    ctx: DryRunContext | None = None
    if args.dry_run:
        out_dir = ROOT / "sim_results" / f"dry_run_{start}_{end}"
        ctx = DryRunContext(out_dir, tickers, start, end)
        _install_llm_hook(ctx)
        logger.info(f"[DRY-RUN] 활성화 — 운영 DB 쓰기 차단, 출력: {out_dir}")

    asyncio.run(
        run(
            start,
            end,
            tickers,
            do_sector_news=not args.no_sector_news,
            do_midterm=not args.no_midterm,
            ctx=ctx,
        )
    )

if __name__ == "__main__":
    main()

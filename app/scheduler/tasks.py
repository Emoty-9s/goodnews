"""
Celery 배치 스케줄러
====================
모든 스케줄 기준: 미국 동부시간 (America/New_York)

- daily  : closing(21:00) / overnight(08:00)
- weekly : draft(월 08:00) / final(금 21:00) / sector-news(금 21:30)
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from celery import Celery
from celery.schedules import crontab
from loguru import logger

from app.core.config import get_settings
from app.core.alerting import send_alert
from app.scheduler.fmp_collector import (
    FMPNewsCollector,
    fetch_general_news,
    get_since_datetime,
)
from app.scheduler.price_collector import (
    fetch_all_weekly_price_changes,
    fetch_sector_weekly_changes,
    fetch_sp500_weekly_change,
)
from app.summarizer.llm_summarizer import (
    generate_midterm_part_b,
    summarize_midterm,
    summarize_sector_news,
    summarize_ticker,
    summarize_update,
    summarize_weekly,
    summarize_weekly_update,
)
from app.models.database import (
    delete_closing_for_overnight,
    delete_draft_for_final,
    delete_old_daily_reports,
    delete_old_news_articles,
    delete_old_weekly_data,
    get_articles_for_ticker_between,
    get_closing_report,
    get_daily_reports,
    get_last_midterm_date,
    get_market_news_for_week,
    get_recent_weekly_finals_for_midterm,
    get_sector_news_series,
    get_ticker_sector_exchange,
    get_tickers_with_news_between,
    get_weekly_benchmarks_series,
    get_weekly_draft,
    has_weekly_final,
    insert_articles,
    insert_market_news,
    upsert_midterm,
    upsert_sector_news,
    upsert_summary,
    upsert_weekly_benchmark,
)

ET = ZoneInfo("America/New_York")

# ── 파일 로그 설정 (로컬에 logger.add()가 없으면 여기서 초기화) ──
_LOG_DIR = Path(__file__).parent.parent.parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logger.add(
    str(_LOG_DIR / "tasks_{time:YYYY-MM-DD}.log"),
    level="INFO",
    encoding="utf-8",
    rotation="1 day",
    retention="30 days",
)

def _week_monday(d: date) -> date:
    """주어진 날짜가 속한 주의 월요일."""
    return d - timedelta(days=d.weekday())


# ──────────────────────────────────────────
# Midterm 트리거 판단
# ──────────────────────────────────────────

MIDTERM_FORCE_INTERVAL_DAYS: int = 42  # 6주 = 42일


def should_generate_midterm(
    ticker: str,
    this_week_monday: date,
    this_week_has_final: bool,
    prev_week_has_final: bool,
    last_midterm_date: date | None,
) -> bool:
    """
    이번 주에 midterm 리포트를 생성해야 하는지 판단한다.

    규칙 (우선순위 순):
    1. this_week_has_final == False → 무조건 False
       (weekly final이 없으면 midterm 소재 없음)
    2. prev_week_has_final == True → True
       (직전 주에도 final이 있었다 = 연속 2주 → 기본 트리거)
    3. last_midterm_date is None
       or (this_week_monday - last_midterm_date).days >= MIDTERM_FORCE_INTERVAL_DAYS
       → True (6주 이상 발행 없음 → 강제 트리거)
    4. 그 외 → False
    """
    if not this_week_has_final:
        return False
    if prev_week_has_final:
        return True
    if last_midterm_date is None:
        return True
    if (this_week_monday - last_midterm_date).days >= MIDTERM_FORCE_INTERVAL_DAYS:
        return True
    return False

def _et_midnight(d: date) -> datetime:
    """ET 자정(00:00) tz-aware datetime."""
    return datetime(d.year, d.month, d.day, tzinfo=ET)

def _check_abort(
    task_name: str, success: int, fail: int, total: int, last_ticker: str
) -> bool:
    """
    실패 10건 이상 누적 + 실패율 50% 초과 시 알림 전송 후 True(중단 신호) 반환.
    """
    processed = success + fail
    if fail >= 10 and processed and fail / processed > 0.5:
        send_alert(
            title=f"🚨 {task_name} 중단",
            message=(
                f"실패율 {fail / processed * 100:.0f}% 초과로 태스크 중단\n"
                f"처리: {processed}/{total}\n"
                f"성공: {success} / 실패: {fail}\n"
                f"마지막 실패 종목: {last_ticker}"
            ),
        )
        logger.error(f"{task_name} 중단: 실패율 초과 ({fail}/{processed})")
        return True
    return False

def _alert_summary(task_name: str, success: int, fail: int, total: int):
    """완료 후 실패가 있었을 때만 요약 알림 전송."""
    if fail > 0:
        send_alert(
            title=f"⚠️ {task_name} 완료 (일부 실패)",
            message=(
                f"전체: {total} / 성공: {success} / 실패: {fail}\n"
                f"실패 종목은 다음 실행에서 재시도됩니다."
            ),
        )
    logger.info(f"{task_name} 완료: {success}/{total} (실패 {fail})")

settings = get_settings()

# ──────────────────────────────────────────
# Celery 앱 초기화
# ──────────────────────────────────────────
celery_app = Celery(
    "goodnews",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="America/New_York",
    enable_utc=True,
)

# ──────────────────────────────────────────
# 티커 목록 — universe_current.csv 에서 로드
# ──────────────────────────────────────────

def load_all_tickers() -> list[str]:
    """
    universe_current.csv (build_universe 실행 결과) 에서
    is_actively_trading=True, universe_status=included 종목만 반환.

    유니버스가 아직 빌드되지 않았으면 빈 리스트 → 배치가 조용히 스킵됨.
    유니버스 빌드: python -m app.universe.build_universe
    """
    from app.universe.ticker_store import get_universe_tickers
    return get_universe_tickers()

# ──────────────────────────────────────────
# 핵심 배치 실행 함수 (공통)
# ──────────────────────────────────────────

async def run_digest_batch(digest_type: str):
    """
    특정 digest_type에 대해:
    1. FMP API로 뉴스 수집
    2. LLM Map-Reduce 요약
    3. DB Upsert
    """
    logger.info(f"===== [{digest_type.upper()}] 배치 시작 =====")

    tickers = load_all_tickers()
    since = get_since_datetime(digest_type)

    # 1. 뉴스 수집
    collector = FMPNewsCollector()
    news_by_ticker = await collector.fetch_all(
        all_tickers=tickers,
        since=since,
        limit_per_batch=50,
        concurrency=25,
    )

    # 2. LLM 요약 + DB Upsert (비-daily 는 report_date=NULL, version=None)
    upserted = 0
    for ticker, news_list in news_by_ticker.items():
        if not news_list:
            continue

        result = summarize_ticker(ticker, news_list, digest_type)
        if result is None:
            continue
        await upsert_summary(
            ticker=result["ticker"],
            digest_type=result["digest_type"],
            report_date=None,
            version=None,
            summary_text=result["summary_text"],
            sentiment=result["sentiment"],
            source_urls=result["source_urls"],
        )
        upserted += 1

    logger.info(f"===== [{digest_type.upper()}] 완료: {upserted}개 종목 Upsert =====")

# ──────────────────────────────────────────
# Daily Phase1: Closing (밤 9PM ET)
# ──────────────────────────────────────────

async def run_daily_closing(test_tickers: list[str] | None = None):
    """
    매일 밤 9PM ET 실행 (장 마감 종합 리포트).
    1) 7일 초과 daily 리포트 정리
    2) 최근 24시간 뉴스 수집 → articles INSERT
    3) 뉴스 있는 종목만 Gemini 요약 → closing 버전 Upsert

    test_tickers 가 주어지면 해당 종목만 처리 (None 이면 전체 유니버스).
    """
    try:
        et_now = datetime.now(ET)
        today_et = et_now.date()
        logger.info(f"===== [DAILY-CLOSING] 시작 (ET {et_now:%Y-%m-%d %H:%M}) =====")

        deleted = await delete_old_daily_reports()
        logger.info(f"[DAILY-CLOSING] 오래된 daily 리포트 {deleted}건 삭제")

        deleted_articles = await delete_old_news_articles(days=7)
        logger.info(
            f"[DAILY-CLOSING] 오래된 원문 삭제 — "
            f"articles={deleted_articles['articles']}건, "
            f"market_news={deleted_articles['market_news_articles']}건"
        )

        since = et_now - timedelta(hours=24)
        tickers = test_tickers if test_tickers else load_all_tickers()
        collector = FMPNewsCollector()
        news_by_ticker = await collector.fetch_all(
            all_tickers=tickers, since=since, limit_per_batch=50, concurrency=25,
        )

        all_articles = [a for articles in news_by_ticker.values() for a in articles]
        inserted = await insert_articles(all_articles)
        logger.info(
            f"[DAILY-CLOSING] articles INSERT {inserted}건 (수집 {len(all_articles)}건)"
        )

        active = [(t, a) for t, a in news_by_ticker.items() if a]
        total = len(active)
        success = 0
        fail = 0
        for ticker, articles in active:
            result = summarize_ticker(ticker, articles, "daily")
            if result is None:
                fail += 1
                logger.warning(f"[FAIL][daily_closing][{ticker}][{today_et}] LLM 반환 None")
                if _check_abort("daily_closing", success, fail, total, ticker):
                    return
                continue

            await upsert_summary(
                ticker=ticker,
                digest_type="daily",
                report_date=today_et,
                version="closing",
                summary_text=result["summary_text"],
                sentiment=result["sentiment"],
                source_urls=result["source_urls"],
            )
            success += 1

        _alert_summary("daily_closing", success, fail, total)
    except Exception as e:
        send_alert(
            title="🔥 daily_closing 오류",
            message=f"태스크 실행 중 예외 발생:\n{str(e)[:500]}",
        )
        raise

# ──────────────────────────────────────────
# Daily Phase2: Premarket (아침 8AM ET)
# ──────────────────────────────────────────

async def run_daily_premarket(
    test_tickers: list[str] | None = None,
    since_override: datetime | None = None,
):
    """
    매일 아침 8AM ET 실행 (장 시작 전 업데이트).
    1) 전날 9PM ~ 오늘 새벽 뉴스 수집 → articles INSERT
    2) 새 뉴스 있는 종목만: 전날 closing 리포트 + 새 뉴스로 premarket 갱신

    test_tickers 가 주어지면 해당 종목만 처리 (None 이면 전체 유니버스).
    since_override 는 테스트 전용 — 수집 시간창을 강제 지정한다.
    """
    try:
        et_now = datetime.now(ET)
        today_et = et_now.date()
        yesterday_et = today_et - timedelta(days=1)
        logger.info(f"===== [DAILY-PREMARKET] 시작 (ET {et_now:%Y-%m-%d %H:%M}) =====")

        # 오늘 00:00 ET 에서 3시간 전 = 전날 21:00(9PM) ET
        since = since_override or (
            et_now.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(hours=3)
        )
        tickers = test_tickers if test_tickers else load_all_tickers()
        collector = FMPNewsCollector()
        news_by_ticker = await collector.fetch_all(
            all_tickers=tickers, since=since, limit_per_batch=50, concurrency=25,
        )

        all_articles = [a for articles in news_by_ticker.values() for a in articles]
        inserted = await insert_articles(all_articles)
        logger.info(
            f"[DAILY-PREMARKET] articles INSERT {inserted}건 (수집 {len(all_articles)}건)"
        )

        active = [(t, a) for t, a in news_by_ticker.items() if a]
        total = len(active)
        success = 0
        fail = 0
        for ticker, new_articles in active:
            closing = await get_closing_report(ticker, yesterday_et)
            summary = summarize_update(
                ticker=ticker,
                existing_report=closing["summary_text"] if closing else None,
                new_articles=new_articles,
            )
            if summary is None:
                fail += 1
                logger.warning(f"[FAIL][daily_premarket][{ticker}][{yesterday_et}] LLM 반환 None")
                if _check_abort("daily_premarket", success, fail, total, ticker):
                    return
                continue

            await upsert_summary(
                ticker=ticker,
                digest_type="daily",
                report_date=yesterday_et,
                version="overnight",
                summary_text=summary["summary_text"],
                sentiment=summary["sentiment"],
                source_urls=summary["source_urls"],
            )
            # overnight은 closing의 최종본 → 같은 날짜 closing 삭제
            await delete_closing_for_overnight(ticker, yesterday_et)
            success += 1

        _alert_summary("daily_premarket", success, fail, total)
    except Exception as e:
        send_alert(
            title="🔥 daily_premarket 오류",
            message=f"태스크 실행 중 예외 발생:\n{str(e)[:500]}",
        )
        raise

# ──────────────────────────────────────────
# Weekly Phase1: Draft (월요일 8AM ET)
# ──────────────────────────────────────────

async def run_weekly_draft(test_tickers: list[str] | None = None):
    """
    월요일 8AM ET 실행 — 주간 초안 생성.
    - 이번 주 월요일을 report_date 로 사용
    - 지난 7일 일간 closing 리포트 조회 (부족하면 원본 뉴스 보완)
    - summarize_weekly() → weekly/draft Upsert
    """
    try:
        et_now = datetime.now(ET)
        week_monday = _week_monday(et_now.date())
        since_date = week_monday - timedelta(days=7)
        until_date = week_monday
        since_dt = _et_midnight(since_date)
        until_dt = _et_midnight(until_date)
        logger.info(
            f"===== [WEEKLY-DRAFT] 시작 (월요일={week_monday}, "
            f"기간 {since_date}~{until_date}) ====="
        )

        deleted = await delete_old_weekly_data()
        logger.info(f"[WEEKLY-DRAFT] 52주 초과 데이터 삭제: {deleted}")

        if test_tickers:
            tickers = test_tickers
        else:
            tickers = await get_tickers_with_news_between(since_dt, until_dt)
        logger.info(f"[WEEKLY-DRAFT] 대상 종목 {len(tickers)}개")

        total = len(tickers)
        success = 0
        fail = 0
        for ticker in tickers:
            dailies = await get_daily_reports(ticker, since_date, until_date)
            raw = []
            if len(dailies) < 3:
                raw = await get_articles_for_ticker_between(ticker, since_dt, until_dt)

            # 요약할 데이터 자체가 없으면 실패가 아니라 스킵
            if not dailies and not raw:
                continue

            summary = summarize_weekly(ticker, daily_reports=dailies, raw_articles=raw)
            if summary is None:
                fail += 1
                logger.warning(
                    f"[FAIL][weekly_draft][{ticker}][{week_monday}] "
                    f"LLM 반환 None (daily {len(dailies)}건 / articles {len(raw)}건)"
                )
                if _check_abort("weekly_draft", success, fail, total, ticker):
                    return
                continue

            await upsert_summary(
                ticker=ticker,
                digest_type="weekly",
                report_date=week_monday,
                version="draft",
                summary_text=summary["summary_text"],
                sentiment=summary["sentiment"],
                source_urls=[],
            )
            success += 1

        _alert_summary("weekly_draft", success, fail, total)
    except Exception as e:
        send_alert(
            title="🔥 weekly_draft 오류",
            message=f"태스크 실행 중 예외 발생:\n{str(e)[:500]}",
        )
        raise

# ──────────────────────────────────────────
# Weekly Phase2: Final (금요일 9PM ET)
# ──────────────────────────────────────────

async def run_weekly_final(test_tickers: list[str] | None = None):
    """
    금요일 9PM ET 실행 — 주간 최종본 생성.
    - 이번 주 월요일을 report_date 로 사용
    - 초안이 있으면 이번 주 일간으로 업데이트(summarize_weekly_update)
    - 초안이 없으면 이번 주 일간(또는 원본 뉴스)으로 신규 생성(summarize_weekly)
    - weekly/final Upsert (초안 행을 final 로 갱신)
    """
    try:
        et_now = datetime.now(ET)
        today = et_now.date()
        week_monday = _week_monday(today)
        since_dt = _et_midnight(week_monday)
        until_dt = _et_midnight(today + timedelta(days=1))
        logger.info(
            f"===== [WEEKLY-FINAL] 시작 (월요일={week_monday}, "
            f"기간 {week_monday}~{today}) ====="
        )

        if test_tickers:
            tickers = test_tickers
        else:
            tickers = await get_tickers_with_news_between(since_dt, until_dt)
        logger.info(f"[WEEKLY-FINAL] 대상 종목 {len(tickers)}개")

        # ── 가격 벤치마크 수집 (종목 루프 전 1회) ──
        # 뉴스 분석과 분리: 아래 가격 데이터는 Gemini 프롬프트에 넣지 않는다.
        week_friday = week_monday + timedelta(days=4)
        sp500_change = await fetch_sp500_weekly_change(week_monday, week_friday)
        sector_changes = await fetch_sector_weekly_changes(week_monday, week_friday)
        await upsert_weekly_benchmark(
            "sp500", "SP500", None, week_monday, sp500_change
        )
        for (sector, exchange), pct in sector_changes.items():
            await upsert_weekly_benchmark(
                "sector", sector, exchange, week_monday, pct
            )
        price_changes = await fetch_all_weekly_price_changes(
            tickers, week_monday, week_friday
        )

        total = len(tickers)
        success = 0
        fail = 0
        for ticker in tickers:
            draft = await get_weekly_draft(ticker, week_monday)
            this_week_dailies = await get_daily_reports(ticker, week_monday, today)

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
                # 요약할 데이터 자체가 없으면 실패가 아니라 스킵
                if not raw:
                    continue
                summary = summarize_weekly(ticker, raw_articles=raw)

            if summary is None:
                fail += 1
                path_used = (
                    "draft_update" if draft else
                    "daily" if this_week_dailies else "raw"
                )
                logger.warning(
                    f"[FAIL][weekly_final][{ticker}][{week_monday}] "
                    f"LLM 반환 None (경로: {path_used})"
                )
                if _check_abort("weekly_final", success, fail, total, ticker):
                    return
                continue

            await upsert_summary(
                ticker=ticker,
                digest_type="weekly",
                report_date=week_monday,
                version="final",
                summary_text=summary["summary_text"],
                sentiment=summary["sentiment"],
                source_urls=[],
                price_change_pct=price_changes.get(ticker),
            )
            # final은 draft의 최종본 → 같은 주 draft 삭제
            await delete_draft_for_final(ticker, week_monday)
            success += 1

        _alert_summary("weekly_final", success, fail, total)
    except Exception as e:
        send_alert(
            title="🔥 weekly_final 오류",
            message=f"태스크 실행 중 예외 발생:\n{str(e)[:500]}",
        )
        raise

# ──────────────────────────────────────────
# Weekly: 섹터별 시장 뉴스 리포트 (금요일 9:30PM ET)
# ──────────────────────────────────────────

async def run_weekly_sector_news(test: bool = False):
    """
    금요일 9:30PM ET 실행 (weekly-final 30분 후), 종목 리포트와 별개.

    1) 이번 주(월~금) 일반 시장 뉴스 수집 → market_news_articles INSERT
    2) 주간 뉴스 조회
    3) summarize_sector_news() 1회 호출 (12개 카테고리 분류)
    4) 카테고리별 sector_news_summaries Upsert

    NOTE: 단일 LLM 호출이라 _check_abort(종목 루프 실패율) 는 해당 없음.
          503 재시도(_generate_content) + 실패/예외 시 ntfy 알림 적용.
    """
    try:
        et_now = datetime.now(ET)
        week_monday = _week_monday(et_now.date())
        week_friday = week_monday + timedelta(days=4)
        logger.info(
            f"===== [SECTOR-NEWS] 시작 (월~금 {week_monday}~{week_friday}, "
            f"test={test}) ====="
        )

        raw = await fetch_general_news(
            week_monday.isoformat(), week_friday.isoformat()
        )
        inserted = await insert_market_news(raw)
        logger.info(
            f"[SECTOR-NEWS] market_news INSERT {inserted}건 (수집 {len(raw)}건)"
        )

        articles = await get_market_news_for_week(week_monday, week_friday)
        if not articles:
            logger.warning("[SECTOR-NEWS] 이번 주 시장 뉴스 없음 → 스킵")
            return

        sector_summaries = summarize_sector_news(articles)
        if sector_summaries is None:
            send_alert(
                title="🚨 weekly_sector_news 실패",
                message=(
                    f"섹터 뉴스 요약 생성 실패 (기사 {len(articles)}건)\n"
                    f"Gemini 호출 실패 또는 파싱된 카테고리 0건"
                ),
            )
            logger.error("[SECTOR-NEWS] 요약 생성 실패 (None)")
            return

        for category, data in sector_summaries.items():
            await upsert_sector_news(
                category=category,
                week_monday=week_monday,
                summary_text=data["summary_text"],
                sentiment=data["sentiment"],
            )

        count = len(sector_summaries)
        logger.info(f"===== [SECTOR-NEWS] 완료: {count}개 카테고리 Upsert =====")
    except Exception as e:
        send_alert(
            title="🔥 weekly_sector_news 오류",
            message=f"태스크 실행 중 예외 발생:\n{str(e)[:500]}",
        )
        raise

# ──────────────────────────────────────────
# Midterm: 중장기 리포트 (금요일 22:00 ET)
# ──────────────────────────────────────────

async def run_midterm(
    tickers: list[str],
    week_monday: date,
) -> dict:
    """
    금요일 22:00 ET 실행 — 최근 12주 weekly final을 집계한 중장기 리포트 생성.
    weekly_final(21:00) → sector_news(21:30) 완료 후 실행.

    반환: {"ok": int, "skip": int, "fail": int, "template": int}
    """
    stats = {"ok": 0, "skip": 0, "fail": 0, "template": 0}
    prev_monday = week_monday - timedelta(days=7)

    logger.info(
        f"===== [MIDTERM] 시작 (week_monday={week_monday}, "
        f"종목 {len(tickers)}개) ====="
    )

    for ticker in tickers:
        try:
            this_has_final = await has_weekly_final(ticker, week_monday)
            prev_has_final = await has_weekly_final(ticker, prev_monday)
            last_mid = await get_last_midterm_date(ticker)

            if not should_generate_midterm(
                ticker, week_monday, this_has_final, prev_has_final, last_mid
            ):
                stats["skip"] += 1
                continue

            weekly_reports = await get_recent_weekly_finals_for_midterm(
                ticker, before=week_monday
            )
            if not weekly_reports:
                stats["skip"] += 1
                continue

            sector_info = await get_ticker_sector_exchange(ticker)
            if sector_info is None:
                logger.warning(f"[MIDTERM][{ticker}] sector/exchange 정보 없음 → 스킵")
                stats["skip"] += 1
                continue
            sector_name, exchange = sector_info

            week_mondays = [
                r["week_monday"]
                if isinstance(r["week_monday"], date)
                else r["week_monday"].date()
                for r in weekly_reports
            ]
            benchmarks = await get_weekly_benchmarks_series(
                week_mondays, sector_name, exchange
            )
            sector_news = await get_sector_news_series(sector_name, week_mondays)

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
                logger.warning(
                    f"[FAIL][midterm][{ticker}][{week_monday}] "
                    f"LLM 반환 None ({len(weekly_reports)}주 기반)"
                )
                stats["skip"] += 1
                continue

            await upsert_midterm(
                ticker=ticker,
                report_date=week_monday,
                summary_text=result["summary_text"],
                sentiment=result["sentiment"],
                price_change_pct=result["price_change_pct"],
            )

            if result["sentiment"] is None:
                stats["template"] += 1
            else:
                stats["ok"] += 1

        except Exception as e:
            logger.error(f"[MIDTERM][{ticker}] 처리 실패: {e}")
            stats["fail"] += 1

    logger.info(
        f"===== [MIDTERM] 완료: ok={stats['ok']} template={stats['template']} "
        f"skip={stats['skip']} fail={stats['fail']} ====="
    )
    return stats

# ──────────────────────────────────────────
# Midterm Part B 갱신 (금요일 22:30 ET)
# ──────────────────────────────────────────

_PART_B_MARKER = "[누적 성과 vs 벤치마크]"


def _replace_part_b(summary_text: str, new_part_b: str) -> str:
    """summary_text에서 '[누적 성과 vs 벤치마크]' 이후를 new_part_b로 교체."""
    idx = summary_text.find(_PART_B_MARKER)
    if idx == -1:
        # 파트 A 없는 종목 → 파트 B 전체로 덮어씀
        return new_part_b
    part_a = summary_text[:idx].rstrip()
    return (part_a + "\n\n" + new_part_b) if part_a else new_part_b


def _cumulative_return(series: list) -> float:
    """None 제외한 주간 수익률 시퀀스 복리 합산. % 단위 반환."""
    factor = 1.0
    for x in series:
        if x is not None:
            factor *= (1 + x / 100)
    return (factor - 1) * 100


async def _get_current_midterm(ticker: str) -> dict | None:
    """현재 midterm 리포트(가장 최근 1건) 조회. 없으면 None."""
    from app.models.database import AsyncSessionLocal
    from sqlalchemy import text as sa_text
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            sa_text(
                "SELECT report_date, summary_text, sentiment, price_change_pct "
                "FROM news_summaries "
                "WHERE ticker = :t AND digest_type = 'midterm' "
                "ORDER BY report_date DESC LIMIT 1"
            ),
            {"t": ticker.upper()},
        )
        row = r.fetchone()
        if row is None:
            return None
        return {
            "report_date": row[0],
            "summary_text": row[1] or "",
            "sentiment": row[2],
            "price_change_pct": row[3],
        }


async def run_refresh_midterm_part_b(test_tickers: list[str] | None = None):
    """
    금요일 22:30 ET 실행 — 전체 종목의 midterm 파트 B를
    이번 주 최신 수치로 갱신. 파트 A(뉴스 기반)는 건드리지 않음.

    weekly_midterm(22:00) 완료 후 실행.
    """
    CONCURRENCY = 10
    et_now = datetime.now(ET)
    week_monday = _week_monday(et_now.date())
    tickers = test_tickers if test_tickers else load_all_tickers()
    total = len(tickers)
    logger.info(
        f"===== [REFRESH-MIDTERM-PART-B] 시작 "
        f"(week_monday={week_monday}, 종목 {total:,}개) ====="
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)
    counter = [0]
    stats = {"ok": 0, "skip": 0, "fail": 0}

    async def _one(ticker: str) -> None:
        async with semaphore:
            try:
                current = await _get_current_midterm(ticker)
                if current is None:
                    stats["skip"] += 1
                    return

                weekly_reports = await get_recent_weekly_finals_for_midterm(
                    ticker, before=week_monday + timedelta(days=1)
                )
                if not weekly_reports:
                    stats["skip"] += 1
                    return

                sector_info = await get_ticker_sector_exchange(ticker)
                if sector_info is None:
                    stats["skip"] += 1
                    return
                sector_name, exchange = sector_info

                week_mondays_list = [
                    r["week_monday"] if isinstance(r["week_monday"], date)
                    else r["week_monday"].date()
                    for r in weekly_reports
                ]
                benchmarks = await get_weekly_benchmarks_series(
                    week_mondays_list, sector_name, exchange
                )
                sector_news = await get_sector_news_series(sector_name, week_mondays_list)

                stock_cumulative = _cumulative_return(
                    [r.get("price_change_pct") for r in weekly_reports]
                )
                sp500_cumulative = _cumulative_return(benchmarks["sp500"])
                sector_cumulative = _cumulative_return(benchmarks["sector"])
                alpha_vs_market = stock_cumulative - sp500_cumulative
                alpha_vs_sector = stock_cumulative - sector_cumulative

                new_part_b = await asyncio.to_thread(
                    generate_midterm_part_b,
                    ticker,
                    stock_cumulative,
                    sp500_cumulative,
                    sector_cumulative,
                    alpha_vs_market,
                    alpha_vs_sector,
                    sector_name,
                    exchange,
                    sector_news,
                )

                new_summary = _replace_part_b(current["summary_text"], new_part_b)
                await upsert_midterm(
                    ticker=ticker,
                    report_date=current["report_date"],
                    summary_text=new_summary,
                    sentiment=current["sentiment"],   # 파트 A의 sentiment 유지
                    price_change_pct=stock_cumulative,
                )
                stats["ok"] += 1

            except Exception as e:
                logger.error(f"[REFRESH-MIDTERM-PART-B][{ticker}] 처리 실패: {e}")
                stats["fail"] += 1

        counter[0] += 1
        if counter[0] % 500 == 0 or counter[0] == total:
            logger.info(
                f"[refresh_midterm_part_b] 진행 {counter[0]:,}/{total:,}"
            )

    await asyncio.gather(*(_one(t) for t in tickers))

    logger.info(
        f"===== [REFRESH-MIDTERM-PART-B] 완료: "
        f"ok={stats['ok']} skip={stats['skip']} fail={stats['fail']} ====="
    )
    if stats["fail"] > 0:
        send_alert(
            title="⚠️ refresh_midterm_part_b 완료 (일부 실패)",
            message=(
                f"성공: {stats['ok']} / 실패: {stats['fail']} / 스킵: {stats['skip']}\n"
                f"실패 종목은 다음 주 실행에서 재시도됩니다."
            ),
        )


# ──────────────────────────────────────────
# Celery 태스크 정의
# ──────────────────────────────────────────

@celery_app.task(name="tasks.daily_closing")
def task_daily_closing():
    asyncio.run(run_daily_closing())

@celery_app.task(name="tasks.daily_premarket")
def task_daily_premarket():
    asyncio.run(run_daily_premarket())

@celery_app.task(name="tasks.weekly_draft")
def task_weekly_draft():
    asyncio.run(run_weekly_draft())

@celery_app.task(name="tasks.weekly_final")
def task_weekly_final():
    asyncio.run(run_weekly_final())

@celery_app.task(name="tasks.weekly_sector_news")
def task_weekly_sector_news():
    asyncio.run(run_weekly_sector_news())

@celery_app.task(name="tasks.weekly_midterm")
def task_weekly_midterm():
    et_now = datetime.now(ET)
    week_monday = _week_monday(et_now.date())
    tickers = load_all_tickers()
    asyncio.run(run_midterm(tickers, week_monday))

@celery_app.task(name="tasks.refresh_midterm_part_b")
def task_refresh_midterm_part_b():
    asyncio.run(run_refresh_midterm_part_b())

@celery_app.task(name="tasks.daily_digest")
def task_daily_digest():
    asyncio.run(run_digest_batch("daily"))

# ──────────────────────────────────────────
# Celery Beat 스케줄 설정
# ──────────────────────────────────────────
celery_app.conf.beat_schedule = {
    # Daily Phase1 — Closing: 매일 21:00 ET (장 마감 후)
    # celery timezone=America/New_York 이므로 ET 기준 시각, DST 자동 처리됨
    "daily-closing": {
        "task": "tasks.daily_closing",
        "schedule": crontab(hour=21, minute=0),
    },
    # Daily Phase2 — Premarket: 매일 08:00 ET (장 시작 전)
    "daily-premarket": {
        "task": "tasks.daily_premarket",
        "schedule": crontab(hour=8, minute=0),
    },

    # Weekly Phase1 — Draft: 매주 월요일 08:00 ET
    "weekly-draft": {
        "task": "tasks.weekly_draft",
        "schedule": crontab(day_of_week="monday", hour=8, minute=0),
    },
    # Weekly Phase2 — Final: 매주 금요일 21:00 ET
    "weekly-final": {
        "task": "tasks.weekly_final",
        "schedule": crontab(day_of_week="friday", hour=21, minute=0),
    },
    # Weekly 섹터 시장 뉴스: 매주 금요일 21:30 ET (weekly-final 30분 후)
    "weekly-sector-news": {
        "task": "tasks.weekly_sector_news",
        "schedule": crontab(day_of_week="friday", hour=21, minute=30),
    },
    # Weekly Midterm: 매주 금요일 22:00 ET (sector-news 30분 후)
    # weekly_final(21:00) + sector_news(21:30) 완료 후 실행
    "weekly-midterm": {
        "task": "tasks.weekly_midterm",
        "schedule": crontab(day_of_week="friday", hour=22, minute=0),
    },
    # Midterm Part B 갱신: 매주 금요일 22:30 ET (weekly_midterm 완료 후)
    # 전체 종목의 파트 B(수치/판단)를 이번 주 최신 벤치마크로 교체.
    # 파트 A(뉴스 기반)는 건드리지 않음.
    "weekly-refresh-midterm-part-b": {
        "task": "tasks.refresh_midterm_part_b",
        "schedule": crontab(day_of_week="friday", hour=22, minute=30),
    },

}

# ──────────────────────────────────────────
# 유니버스 빌드 태스크
# ──────────────────────────────────────────

@celery_app.task(name="tasks.build_universe")
def task_build_universe():
    """
    FMP company-screener 기반으로 뉴스 수집 대상 유니버스를 빌드하고
    data/universe/universe_current.csv 를 갱신한다.

    스케줄: 매주 일요일 새벽 2시 (장 마감 후 조용한 시간대)
    수동 실행: celery -A app.scheduler.tasks call tasks.build_universe
    """
    from app.universe.universe_runner import run_universe_build, UniverseBuildConfig
    config = UniverseBuildConfig(
        min_market_cap=settings.universe_min_market_cap,
        exchanges=settings.universe_exchanges,
        save_raw=False,
        profile_enrich_new_only=True,
    )
    result = run_universe_build(config)
    if result.success:
        logger.info(
            f"[UNIVERSE] 빌드 완료: {result.included_count}개 종목 | "
            f"snapshot={result.snapshot_date}"
        )
    else:
        logger.error(f"[UNIVERSE] 빌드 실패: exit_code={result.exit_code}")

# beat_schedule 에 universe 빌드 추가
celery_app.conf.beat_schedule["universe-weekly"] = {
    "task": "tasks.build_universe",
    "schedule": crontab(hour=2, minute=0, day_of_week=0),  # 매주 일요일 02:00
}

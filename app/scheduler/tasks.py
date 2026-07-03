"""
Celery 배치 스케줄러
====================
모든 스케줄 기준: 미국 동부시간 (America/New_York)

- daily  : closing(21:00) / overnight(08:00)
- weekly : draft(월 08:00) / final(금 21:00) / sector-news(금 21:30)
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from celery import Celery
from celery.schedules import crontab
from loguru import logger

# httpx가 매 요청마다 INFO로 "HTTP Request: GET ..." 로그를 찍어
# 수천 개 티커를 순회하는 배치 중 Railway 로그 rate limit에 걸려
# 정작 필요한 에러 로그가 유실되는 것을 방지.
logging.getLogger("httpx").setLevel(logging.WARNING)

from app.core.config import get_settings
from app.core.alerting import send_alert
from app.scheduler.macro_collector import fetch_macro_indicators
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
    delete_old_macro_indicators,
    delete_old_news_articles,
    delete_old_weekly_data,
    get_articles_for_ticker_between,
    get_closing_report,
    get_daily_reports,
    get_last_midterm_date,
    get_latest_macro_snapshot,
    get_market_news_for_week,
    get_recent_weekly_finals_for_midterm,
    get_sector_news_series,
    get_ticker_sector_exchange,
    get_tickers_with_news_between,
    dispose_engine,
    get_unresolved_failures,
    get_weekly_benchmarks_series,
    get_weekly_draft,
    has_weekly_final,
    insert_articles,
    insert_market_news,
    mark_failure_resolved,
    record_fetch_failure,
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

async def load_all_tickers() -> list[str]:
    """
    Supabase universe_tickers 테이블에서
    is_actively_trading=True, universe_status=included 종목만 반환.

    DB가 비어있으면 빈 리스트 → 배치가 조용히 스킵됨.
    DB 업로드: python scripts/upload_universe_to_supabase.py
    """
    from app.universe.ticker_store import get_universe_tickers
    return await get_universe_tickers()

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

    tickers = await load_all_tickers()
    since = get_since_datetime(digest_type)

    # 1. 뉴스 수집
    collector = FMPNewsCollector()
    news_by_ticker, _failed_tickers = await collector.fetch_all(
        all_tickers=tickers,
        since=since,
        limit_per_batch=50,
        concurrency=15,
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
        tickers = test_tickers if test_tickers else await load_all_tickers()
        collector = FMPNewsCollector()
        news_by_ticker, failed_tickers = await collector.fetch_all(
            all_tickers=tickers, since=since, limit_per_batch=50, concurrency=15,
        )

        if failed_tickers:
            for ticker, err in failed_tickers.items():
                await record_fetch_failure(ticker, "daily", today_et, err)
            logger.warning(
                f"[DAILY-CLOSING] FMP 수집 실패 {len(failed_tickers)}개 종목 "
                f"→ fetch_failures 기록: {', '.join(sorted(failed_tickers))}"
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
                logger.warning(f"[FAIL][daily_closing][{ticker}][{today_et}] LLM 반환 None — 재시도 대상 등록")
                await record_fetch_failure(
                    ticker, "daily", today_et,
                    "LLM 요약 실패 (Gemini 503 재시도 소진) — fetch는 성공함",
                )
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

        # 뉴스 수집 실패(429 등) 안전망 — 즉시 1차 재시도, 필요시 지연 재시도 예약
        await retry_failed_daily(today_et, since=since, pass_num=1)
    except Exception as e:
        send_alert(
            title="🔥 daily_closing 오류",
            message=f"태스크 실행 중 예외 발생:\n{str(e)[:500]}",
        )
        raise

# ──────────────────────────────────────────
# Daily Phase1b: 뉴스 수집 실패(429 등) 재시도 안전망
# ──────────────────────────────────────────

RETRY_MAX_PASSES = 5
# pass_num(진행한 회차) → 다음 pass 예약까지 대기 시간(초). Pass1은 즉시(0) 실행.
RETRY_DELAY_SECONDS = {1: 300, 2: 600, 3: 1200, 4: 2400}


async def schedule_retry_or_alert(
    task_type: str,
    report_date: date,
    pass_num: int,
) -> None:
    """
    한 pass 실행 후 호출하는 공용 재시도 스케줄러.

    미해결이 없으면 종료. pass_num < RETRY_MAX_PASSES 면 다음 pass를
    tasks.retry_failed_report 로 +5/10/20/40분 예약. RETRY_MAX_PASSES(5)회
    소진 시 ntfy 알림. daily/daily_premarket/향후 weekly/midterm 재시도
    함수들이 공통으로 호출하는 진입점.
    """
    unresolved = await get_unresolved_failures(task_type, report_date)
    if not unresolved:
        return

    if pass_num >= RETRY_MAX_PASSES:
        lines = [f"- {u['ticker']}: {u['last_error']}" for u in unresolved]
        send_alert(
            title=f"⚠️ {task_type} 최종 실패 {len(unresolved)}개",
            message=(
                f"{report_date} — {RETRY_MAX_PASSES}회 재시도 후에도 실패\n"
                + "\n".join(lines[:50])
            ),
        )
        logger.error(
            f"[RETRY][{task_type}] {report_date} 최종 실패 확정: "
            f"{len(unresolved)}개 ({RETRY_MAX_PASSES}회 재시도 소진)"
        )
        return

    countdown = RETRY_DELAY_SECONDS[pass_num]
    next_pass = pass_num + 1
    celery_app.send_task(
        "tasks.retry_failed_report",
        args=[task_type, report_date.isoformat(), next_pass],
        countdown=countdown,
    )
    logger.info(
        f"[RETRY][{task_type}] pass {next_pass} 예약: +{countdown // 60}분 후 "
        f"(미해결 {len(unresolved)}개)"
    )


async def retry_failed_daily(
    report_date: date,
    since: datetime | None = None,
    pass_num: int = 1,
) -> None:
    """
    daily-closing 뉴스 수집 실패(429 등, 재시도 소진) 티커만 다시 수집한다.

    - Pass 1: run_daily_closing 종료 직후 즉시 호출.
    - 여전히 미해결이면 Celery countdown으로 다음 pass 를 예약(+5/10/20/40분).
    - 최대 RETRY_MAX_PASSES(5)회. 그 이후에도 미해결이면 최종 실패로 확정하고 ntfy 알림.

    daily 전용 — weekly/midterm 은 대상 아님.
    """
    unresolved = await get_unresolved_failures("daily", report_date)
    if not unresolved:
        return

    tickers = [u["ticker"] for u in unresolved]
    logger.info(
        f"[RETRY-DAILY][pass {pass_num}] {report_date} 미해결 {len(tickers)}개 재시도: "
        f"{', '.join(tickers)}"
    )

    if since is None:
        since = datetime.now(ET) - timedelta(hours=24)

    collector = FMPNewsCollector()
    news_by_ticker, failed_tickers = await collector.fetch_all(
        all_tickers=tickers, since=since, limit_per_batch=50, concurrency=15,
    )

    resolved_count = 0
    for ticker in tickers:
        if ticker in failed_tickers:
            await record_fetch_failure(ticker, "daily", report_date, failed_tickers[ticker])
            continue

        articles = news_by_ticker.get(ticker, [])
        if not articles:
            # 재수집 성공, 뉴스 없음까지 확인됨 → fetch_failures 상 해결 처리
            await mark_failure_resolved(ticker, "daily", report_date)
            resolved_count += 1
            continue

        await insert_articles(articles)
        result = summarize_ticker(ticker, articles, "daily")
        if result is not None:
            await upsert_summary(
                ticker=ticker,
                digest_type="daily",
                report_date=report_date,
                version="closing",
                summary_text=result["summary_text"],
                sentiment=result["sentiment"],
                source_urls=result["source_urls"],
            )
            await mark_failure_resolved(ticker, "daily", report_date)
            resolved_count += 1
        else:
            # fetch는 성공했지만 요약 실패 — "해결"로 잘못 표시하면 리포트 없이
            # 영구 누락되므로 미해결로 유지해 다음 pass에서 재시도되게 한다.
            logger.warning(
                f"[RETRY-DAILY][pass {pass_num}][{ticker}] "
                f"fetch 성공, LLM 요약 실패 — 재시도 대상 유지"
            )
            await record_fetch_failure(
                ticker, "daily", report_date,
                "LLM 요약 실패 (Gemini 503 재시도 소진) — fetch는 성공함",
            )

    logger.info(
        f"[RETRY-DAILY][pass {pass_num}] 완료: 해결 {resolved_count}개 / "
        f"재실패 {len(failed_tickers)}개"
    )

    await schedule_retry_or_alert("daily", report_date, pass_num)

# ──────────────────────────────────────────
# Daily Phase2: Premarket (아침 8AM ET)
# ──────────────────────────────────────────

DAILY_PREMARKET_CONCURRENCY = 3


async def run_daily_premarket(
    test_tickers: list[str] | None = None,
    since_override: datetime | None = None,
):
    """
    매일 아침 8AM ET 실행 (장 시작 전 업데이트).
    1) 전날 9PM ~ 오늘 새벽 뉴스 수집 → articles INSERT
    2) 새 뉴스 있는 종목만: 전날 closing 리포트 + 새 뉴스로 premarket 갱신
       (Semaphore + asyncio.to_thread 로 병렬 처리)

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
        tickers = test_tickers if test_tickers else await load_all_tickers()
        collector = FMPNewsCollector()
        news_by_ticker, failed_tickers = await collector.fetch_all(
            all_tickers=tickers, since=since, limit_per_batch=50, concurrency=15,
        )

        if failed_tickers:
            for ticker, err in failed_tickers.items():
                await record_fetch_failure(ticker, "daily_premarket", today_et, err)
            logger.warning(
                f"[DAILY-PREMARKET] FMP 수집 실패 {len(failed_tickers)}개 종목 "
                f"→ fetch_failures 기록: {', '.join(sorted(failed_tickers))}"
            )

        all_articles = [a for articles in news_by_ticker.values() for a in articles]
        inserted = await insert_articles(all_articles)
        logger.info(
            f"[DAILY-PREMARKET] articles INSERT {inserted}건 (수집 {len(all_articles)}건)"
        )

        active = [(t, a) for t, a in news_by_ticker.items() if a]
        total = len(active)
        semaphore = asyncio.Semaphore(DAILY_PREMARKET_CONCURRENCY)
        stats = {"success": 0, "fail": 0}
        abort_flag = [False]

        async def _one(ticker: str, new_articles: list[dict]) -> None:
            async with semaphore:
                if abort_flag[0]:
                    return
                closing = await get_closing_report(ticker, yesterday_et)
                summary = await asyncio.to_thread(
                    summarize_update,
                    ticker=ticker,
                    existing_report=closing["summary_text"] if closing else None,
                    new_articles=new_articles,
                )
                if summary is None:
                    stats["fail"] += 1
                    logger.warning(
                        f"[FAIL][daily_premarket][{ticker}][{yesterday_et}] "
                        f"LLM 반환 None — 재시도 대상 등록"
                    )
                    await record_fetch_failure(
                        ticker, "daily_premarket", today_et,
                        "LLM 요약 실패 (Gemini 503 재시도 소진) — fetch는 성공함",
                    )
                    if _check_abort(
                        "daily_premarket", stats["success"], stats["fail"], total, ticker
                    ):
                        abort_flag[0] = True
                    return

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
                stats["success"] += 1

        await asyncio.gather(*(_one(t, a) for t, a in active))

        if abort_flag[0]:
            return

        _alert_summary("daily_premarket", stats["success"], stats["fail"], total)

        # 뉴스 수집(429 등) + LLM 요약 실패 안전망 — 즉시 1차 재시도, 필요시 지연 재시도 예약
        await retry_failed_daily_premarket(today_et, since=since, pass_num=1)
    except Exception as e:
        send_alert(
            title="🔥 daily_premarket 오류",
            message=f"태스크 실행 중 예외 발생:\n{str(e)[:500]}",
        )
        raise

# ──────────────────────────────────────────
# Daily Phase2b: Premarket 실패(429/LLM) 재시도 안전망
# ──────────────────────────────────────────


async def retry_failed_daily_premarket(
    report_date: date,
    since: datetime | None = None,
    pass_num: int = 1,
) -> None:
    """
    daily-premarket 뉴스 수집(429 등) + LLM 요약 실패(재시도 소진) 티커만
    다시 수집+요약한다. retry_failed_daily(closing)와 동일한 구조 —
    digest_type='daily_premarket' 전용.

    - Pass 1: run_daily_premarket 종료 직후 즉시 호출.
    - 여전히 미해결이면 Celery countdown으로 다음 pass 를 예약(+5/10/20/40분).
    - 최대 RETRY_MAX_PASSES(5)회. 그 이후에도 미해결이면 최종 실패로 확정하고 ntfy 알림.
    """
    unresolved = await get_unresolved_failures("daily_premarket", report_date)
    if not unresolved:
        return

    tickers = [u["ticker"] for u in unresolved]
    logger.info(
        f"[RETRY-PREMARKET][pass {pass_num}] {report_date} 미해결 {len(tickers)}개 재시도: "
        f"{', '.join(tickers)}"
    )

    yesterday = report_date - timedelta(days=1)
    if since is None:
        since = _et_midnight(report_date) - timedelta(hours=3)

    collector = FMPNewsCollector()
    news_by_ticker, failed_tickers = await collector.fetch_all(
        all_tickers=tickers, since=since, limit_per_batch=50, concurrency=15,
    )

    resolved_count = 0
    for ticker in tickers:
        if ticker in failed_tickers:
            await record_fetch_failure(
                ticker, "daily_premarket", report_date, failed_tickers[ticker]
            )
            continue

        articles = news_by_ticker.get(ticker, [])
        if not articles:
            # 재수집 성공, 새 뉴스 없음까지 확인됨 → fetch_failures 상 해결 처리
            await mark_failure_resolved(ticker, "daily_premarket", report_date)
            resolved_count += 1
            continue

        await insert_articles(articles)
        closing = await get_closing_report(ticker, yesterday)
        summary = summarize_update(
            ticker=ticker,
            existing_report=closing["summary_text"] if closing else None,
            new_articles=articles,
        )
        if summary is not None:
            await upsert_summary(
                ticker=ticker,
                digest_type="daily",
                report_date=yesterday,
                version="overnight",
                summary_text=summary["summary_text"],
                sentiment=summary["sentiment"],
                source_urls=summary["source_urls"],
            )
            await delete_closing_for_overnight(ticker, yesterday)
            await mark_failure_resolved(ticker, "daily_premarket", report_date)
            resolved_count += 1
        else:
            logger.warning(
                f"[RETRY-PREMARKET][pass {pass_num}][{ticker}] "
                f"fetch 성공, LLM 요약 실패 — 재시도 대상 유지"
            )
            await record_fetch_failure(
                ticker, "daily_premarket", report_date,
                "LLM 요약 실패 (Gemini 503 재시도 소진) — fetch는 성공함",
            )

    logger.info(
        f"[RETRY-PREMARKET][pass {pass_num}] 완료: 해결 {resolved_count}개 / "
        f"재실패 {len(failed_tickers)}개"
    )

    await schedule_retry_or_alert("daily_premarket", report_date, pass_num)

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


def _format_macro_data(snapshot: dict[str, dict]) -> str:
    """거시 지표 snapshot → 프롬프트용 텍스트 블록."""
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


async def run_refresh_midterm_part_b(test_tickers: list[str] | None = None):
    """
    금요일 22:30 ET 실행 — 전체 종목의 midterm 파트 B를
    이번 주 최신 수치로 갱신. 파트 A(뉴스 기반)는 건드리지 않음.

    weekly_midterm(22:00) 완료 후 실행.
    """
    CONCURRENCY = 10
    et_now = datetime.now(ET)
    week_monday = _week_monday(et_now.date())
    tickers = test_tickers if test_tickers else await load_all_tickers()
    total = len(tickers)
    logger.info(
        f"===== [REFRESH-MIDTERM-PART-B] 시작 "
        f"(week_monday={week_monday}, 종목 {total:,}개) ====="
    )

    # 거시 데이터는 전 종목 공통 — 한 번만 조회
    macro_snapshot = await get_latest_macro_snapshot()
    macro_data = _format_macro_data(macro_snapshot)

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
                    macro_data,
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

async def _run_and_dispose(coro):
    """
    코루틴 실행 후 (같은 이벤트 루프 안에서) DB 엔진 커넥션 풀을 비운다.
    코루틴의 반환값은 그대로 전달한다.

    asyncio.run()은 호출마다 새 이벤트 루프를 만들고 끝나면 닫지만, DB 엔진은
    워커 프로세스 생애주기 동안 재사용된다. 풀에 남은 커넥션이 "닫힌 루프"에
    귀속된 채로 다음 asyncio.run() 호출에 재사용되면 RuntimeError가 발생하므로,
    모든 Celery 태스크는 asyncio.run(...) 대신 asyncio.run(_run_and_dispose(...))
    으로 감싸 실행 직후 풀을 비운다.
    """
    try:
        return await coro
    finally:
        await dispose_engine()


@celery_app.task(name="tasks.daily_closing")
def task_daily_closing():
    asyncio.run(_run_and_dispose(run_daily_closing()))

@celery_app.task(name="tasks.retry_failed_daily")
def task_retry_failed_daily(
    report_date_str: str = None,
    since_iso: str = None,
    pass_num: int = 1,
):
    """
    daily-closing 뉴스 수집 실패 안전망 — 지연 재시도(pass 2~5) 및 수동 실행용.

    수동 실행: celery -A app.scheduler.tasks call tasks.retry_failed_daily --args='["2026-07-01"]'
    """
    report_date = date.fromisoformat(report_date_str) if report_date_str else date.today()
    since = datetime.fromisoformat(since_iso) if since_iso else None
    asyncio.run(_run_and_dispose(
        retry_failed_daily(report_date, since=since, pass_num=pass_num)
    ))

@celery_app.task(name="tasks.daily_premarket")
def task_daily_premarket():
    asyncio.run(_run_and_dispose(run_daily_premarket()))

@celery_app.task(name="tasks.retry_failed_daily_premarket")
def task_retry_failed_daily_premarket(
    report_date_str: str = None,
    since_iso: str = None,
    pass_num: int = 1,
):
    """
    daily-premarket 실패 안전망 — 지연 재시도(pass 2~5) 및 수동 실행용.

    수동 실행: celery -A app.scheduler.tasks call tasks.retry_failed_daily_premarket --args='["2026-07-03"]'
    """
    report_date = date.fromisoformat(report_date_str) if report_date_str else date.today()
    since = datetime.fromisoformat(since_iso) if since_iso else None
    asyncio.run(_run_and_dispose(
        retry_failed_daily_premarket(report_date, since=since, pass_num=pass_num)
    ))

# ──────────────────────────────────────────
# 공용 재시도 레지스트리 — Stage 3 이후(weekly/midterm) 여기에 핸들러만 추가
# ──────────────────────────────────────────

RETRY_HANDLERS: dict[str, Callable[[date, int], Awaitable[None]]] = {
    "daily": lambda report_date, pass_num: retry_failed_daily(report_date, pass_num=pass_num),
    "daily_premarket": lambda report_date, pass_num: retry_failed_daily_premarket(
        report_date, pass_num=pass_num
    ),
    # weekly/midterm은 각 Stage에서 여기 추가
}


@celery_app.task(name="tasks.retry_failed_report")
def task_retry_failed_report(task_type: str, report_date_str: str, pass_num: int = 1):
    """
    schedule_retry_or_alert가 pass 2 이후를 예약할 때 쓰는 공용 진입점.

    수동 실행: celery -A app.scheduler.tasks call tasks.retry_failed_report \
        --args='["daily_premarket", "2026-07-03", 1]'
    """
    handler = RETRY_HANDLERS.get(task_type)
    if handler is None:
        logger.error(f"[RETRY] 알 수 없는 task_type: {task_type}")
        return
    report_date = date.fromisoformat(report_date_str)
    asyncio.run(_run_and_dispose(handler(report_date, pass_num)))


@celery_app.task(name="tasks.weekly_draft")
def task_weekly_draft():
    asyncio.run(_run_and_dispose(run_weekly_draft()))

@celery_app.task(name="tasks.weekly_final")
def task_weekly_final():
    asyncio.run(_run_and_dispose(run_weekly_final()))

@celery_app.task(name="tasks.weekly_sector_news")
def task_weekly_sector_news():
    asyncio.run(_run_and_dispose(run_weekly_sector_news()))

@celery_app.task(name="tasks.weekly_midterm")
def task_weekly_midterm():
    async def _run():
        et_now = datetime.now(ET)
        week_monday = _week_monday(et_now.date())
        tickers = await load_all_tickers()
        await run_midterm(tickers, week_monday)
    asyncio.run(_run_and_dispose(_run()))

@celery_app.task(name="tasks.refresh_midterm_part_b")
def task_refresh_midterm_part_b():
    asyncio.run(_run_and_dispose(run_refresh_midterm_part_b()))


@celery_app.task(name="tasks.macro_collect")
def task_macro_collect():
    """
    매주 금요일 21:15 ET — 거시경제 지표 수집 + 오래된 데이터 삭제.
    weekly-final(21:00) 완료 후, weekly-sector-news(21:30) 이전 실행.
    """
    async def _run():
        count = await fetch_macro_indicators()
        deleted = await delete_old_macro_indicators(months=6)
        logger.info(f"[MACRO] 수집 {count}건, 삭제 {deleted}건")
    asyncio.run(_run_and_dispose(_run()))


@celery_app.task(name="tasks.daily_digest")
def task_daily_digest():
    asyncio.run(_run_and_dispose(run_digest_batch("daily")))

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
    # 거시 지표 수집: 매주 금요일 21:15 ET (weekly-final 완료 후, sector-news 이전)
    "weekly-macro-collect": {
        "task": "tasks.macro_collect",
        "schedule": crontab(day_of_week="friday", hour=21, minute=15),
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
    Supabase universe_tickers 테이블에 upsert한다.

    스케줄: 매년 1월 1일 새벽 3시 ET (거래소 휴장일, 안전한 시간대)
    수동 실행: celery -A app.scheduler.tasks call tasks.build_universe
    """
    import pandas as pd
    from pathlib import Path as _Path
    from app.universe.universe_runner import run_universe_build, UniverseBuildConfig
    from app.universe.universe_save import save_to_supabase

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
        try:
            csv_path = _Path(result.data_dir) / "universe_current.csv"
            if csv_path.exists():
                df = pd.read_csv(csv_path)
                count = asyncio.run(_run_and_dispose(save_to_supabase(df)))
                logger.info(f"[UNIVERSE] Supabase upsert 완료: {count}개 종목")
            else:
                logger.warning("[UNIVERSE] universe_current.csv 없음 — Supabase 업로드 스킵")
        except Exception as e:
            logger.error(f"[UNIVERSE] Supabase 업로드 실패: {e}")
    else:
        logger.error(f"[UNIVERSE] 빌드 실패: exit_code={result.exit_code}")

# beat_schedule — 매년 1월 1일 03:00 ET (거래소 휴장일, 안전)
celery_app.conf.beat_schedule["universe-yearly"] = {
    "task": "tasks.build_universe",
    "schedule": crontab(month_of_year=1, day_of_month=1, hour=3, minute=0),
}

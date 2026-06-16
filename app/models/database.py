import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Column, String, Text, TIMESTAMP, Date, JSON, Float, Index, func, text
from sqlalchemy.dialects.postgresql import ARRAY, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class NewsSummary(Base):
    """
    주식 종목별 AI 요약 테이블
    키: (ticker, digest_type, report_date)

    NOTE: report_date 는 daily 전용(NULL 허용). 비-daily(weekly/monthly/yearly)는 NULL.
    Supabase 실테이블은 UNIQUE (ticker, digest_type, report_date) NULLS NOT DISTINCT 로
    관리되어 NULL 도 충돌 키로 동작한다(마이그레이션으로 적용). ORM 매핑상으로는
    세 컬럼을 primary_key 로 선언한다.
    """
    __tablename__ = "news_summaries"

    ticker = Column(String(10), primary_key=True, comment="주식 티커 (예: AAPL)")
    digest_type = Column(
        String(10), primary_key=True,
        comment="요약 주기: daily | weekly | monthly | yearly"
    )
    report_date = Column(
        Date, primary_key=True, nullable=True,
        comment="ET 기준 날짜 (daily 전용, 비-daily 는 NULL)"
    )
    version = Column(
        String(10), nullable=True,
        comment="closing | premarket | None (daily 전용)"
    )
    summary_text = Column(Text, nullable=True, comment="AI 요약 텍스트 (마크다운)")
    sentiment = Column(String(10), nullable=True, comment="bullish | bearish | neutral")
    source_urls = Column(JSON, nullable=True, comment="원문 뉴스 URL 리스트")
    price_change_pct = Column(
        Float, nullable=True,
        comment="주간 가격 변동률(%) — weekly 전용 (월요일 open→금요일 close)"
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        comment="마지막 AI 업데이트 시각 (UTC)"
    )

    def __repr__(self):
        return (
            f"<NewsSummary {self.ticker} [{self.digest_type}] "
            f"{self.report_date} {self.version} {self.sentiment}>"
        )


class Article(Base):
    """
    중복 제거된 원본 뉴스 기사 테이블
    PK: url_hash = SHA256(url)
    """
    __tablename__ = "articles"

    url_hash = Column(String(64), primary_key=True, comment="SHA256(url)")
    title = Column(Text, nullable=True, comment="뉴스 제목")
    text = Column(Text, nullable=True, comment="뉴스 본문")
    published_at = Column(
        TIMESTAMP(timezone=True), nullable=True, comment="기사 발행 시각 (TZ)"
    )
    source = Column(String(100), nullable=True, comment="출처 도메인 (예: reuters.com)")
    url = Column(Text, nullable=True, comment="원문 URL")
    tickers = Column(
        ARRAY(String), nullable=True, comment="연관 티커 목록 (크로스 티커 태그)"
    )
    created_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        comment="레코드 생성 시각 (UTC)"
    )

    __table_args__ = (
        Index("ix_articles_published_at", "published_at"),
        Index("ix_articles_tickers_gin", "tickers", postgresql_using="gin"),
    )

    def __repr__(self):
        return f"<Article {self.url_hash[:8]} {self.tickers}>"


class MarketNewsArticle(Base):
    """
    일반 시장 뉴스 기사 테이블 (종목 태그 없음).
    PK: url_hash = SHA256(url)
    """
    __tablename__ = "market_news_articles"

    url_hash = Column(String(64), primary_key=True, comment="SHA256(url)")
    title = Column(Text, nullable=True, comment="뉴스 제목")
    text = Column(Text, nullable=True, comment="뉴스 본문")
    url = Column(Text, nullable=True, comment="원문 URL")
    source = Column(String(100), nullable=True, comment="출처 도메인 (예: cnbc.com)")
    published_at = Column(
        TIMESTAMP(timezone=True), nullable=True, comment="기사 발행 시각 (TZ)"
    )

    def __repr__(self):
        return f"<MarketNewsArticle {self.url_hash[:8]} {self.source}>"


async def get_db():
    """FastAPI dependency - DB 세션 제공"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def create_tables():
    """테이블 생성 (최초 실행 시)"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ──────────────────────────────────────────
# 내부 유틸
# ──────────────────────────────────────────

def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_dt(value):
    """ISO/Z/공백 포맷 문자열을 datetime 으로 파싱. 실패 시 None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(str(value), fmt)
            except ValueError:
                continue
    return None


def _article_to_row(item: dict) -> dict | None:
    """
    수집/정제 기사 dict → articles 테이블 row dict.
    url 이 없으면 None (PK 생성 불가).
    """
    url = item.get("url") or ""
    if not url:
        return None

    # tickers: 정제본은 tickers 배열, 수집본은 단일 symbol
    if item.get("tickers"):
        tickers = list(item["tickers"])
    elif item.get("symbol"):
        tickers = [str(item["symbol"]).upper()]
    else:
        tickers = []

    published = item.get("published_at") or item.get("publishedDate")
    source = item.get("source") or item.get("site") or ""

    return {
        "url_hash": item.get("url_hash") or _sha256_hex(url),
        "title": item.get("title", "") or "",
        "text": item.get("text", "") or "",
        "published_at": _parse_dt(published),
        "source": source,
        "url": url,
        "tickers": tickers,
    }


# ──────────────────────────────────────────
# Articles
# ──────────────────────────────────────────

async def insert_articles(articles: list[dict]) -> int:
    """
    articles 를 ON CONFLICT (url_hash) DO NOTHING 으로 bulk INSERT.
    삽입된 행 수를 반환한다 (이미 있는 건 스킵).
    """
    # url_hash 기준 입력 내 중복 제거
    rows_by_hash: dict[str, dict] = {}
    for item in articles:
        row = _article_to_row(item)
        if row is None:
            continue
        rows_by_hash[row["url_hash"]] = row

    rows = list(rows_by_hash.values())
    if not rows:
        return 0

    inserted = 0
    async with AsyncSessionLocal() as session:
        for i in range(0, len(rows), 500):
            batch = rows[i:i + 500]
            stmt = pg_insert(Article).values(batch).on_conflict_do_nothing(
                index_elements=["url_hash"]
            )
            result = await session.execute(stmt)
            inserted += result.rowcount or 0
        await session.commit()

    return inserted


def _market_news_to_row(item: dict) -> dict | None:
    """일반 시장 뉴스 dict → market_news_articles row dict. url 없으면 None."""
    url = item.get("url") or ""
    if not url:
        return None
    published = item.get("published_at") or item.get("publishedDate")
    source = item.get("source") or item.get("site") or ""
    return {
        "url_hash": item.get("url_hash") or _sha256_hex(url),
        "title": item.get("title", "") or "",
        "text": item.get("text", "") or "",
        "url": url,
        "source": source,
        "published_at": _parse_dt(published),
    }


async def insert_market_news(articles: list[dict]) -> int:
    """
    market_news_articles 를 ON CONFLICT (url_hash) DO NOTHING 으로 bulk INSERT.
    삽입된 행 수 반환 (이미 있는 건 스킵).
    """
    rows_by_hash: dict[str, dict] = {}
    for item in articles:
        row = _market_news_to_row(item)
        if row is None:
            continue
        rows_by_hash[row["url_hash"]] = row

    rows = list(rows_by_hash.values())
    if not rows:
        return 0

    inserted = 0
    async with AsyncSessionLocal() as session:
        for i in range(0, len(rows), 500):
            batch = rows[i:i + 500]
            stmt = pg_insert(MarketNewsArticle).values(batch).on_conflict_do_nothing(
                index_elements=["url_hash"]
            )
            result = await session.execute(stmt)
            inserted += result.rowcount or 0
        await session.commit()

    return inserted


async def get_market_news_for_week(
    week_monday: date, week_friday: date
) -> list[dict]:
    """해당 주(월 00:00 ~ 금+1 00:00) 일반 시장 뉴스 목록 (오래된 순)."""
    since = datetime(week_monday.year, week_monday.month, week_monday.day,
                     tzinfo=ZoneInfo("America/New_York"))
    end_day = week_friday + timedelta(days=1)
    until = datetime(end_day.year, end_day.month, end_day.day,
                     tzinfo=ZoneInfo("America/New_York"))
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT url_hash, title, text, url, source, published_at "
                "FROM market_news_articles "
                "WHERE published_at >= :since AND published_at < :until "
                "ORDER BY published_at ASC"
            ),
            {"since": since, "until": until},
        )
        rows = result.mappings().all()

    out = []
    for row in rows:
        pub = row["published_at"]
        out.append({
            "url_hash": row["url_hash"],
            "title": row["title"],
            "text": row["text"],
            "url": row["url"],
            "source": row["source"],
            "published_at": pub.isoformat() if pub else "",
            "publishedDate": pub.isoformat() if pub else "",
        })
    return out


async def upsert_sector_news(
    category: str, week_monday: date, summary_text: str, sentiment: str
) -> None:
    """sector_news_summaries Upsert. 충돌 키: (category, week_monday)."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO sector_news_summaries
                    (category, week_monday, summary_text, sentiment)
                VALUES
                    (:category, :week_monday, :summary_text, :sentiment)
                ON CONFLICT (category, week_monday)
                DO UPDATE SET
                    summary_text = EXCLUDED.summary_text,
                    sentiment    = EXCLUDED.sentiment
                """
            ),
            {
                "category": category,
                "week_monday": week_monday,
                "summary_text": summary_text,
                "sentiment": sentiment,
            },
        )
        await session.commit()


async def get_tickers_with_news(since: datetime) -> list[str]:
    """published_at >= since 인 기사들의 연관 티커 목록(중복 제거)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT DISTINCT unnest(tickers) AS t "
                "FROM articles WHERE published_at >= :since"
            ),
            {"since": since},
        )
        return [r[0] for r in result.all() if r[0]]


async def get_articles_for_ticker(ticker: str, since: datetime) -> list[dict]:
    """특정 ticker 의 published_at >= since 기사 목록 (최신순)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT url_hash, title, text, published_at, source, url, tickers "
                "FROM articles "
                "WHERE :ticker = ANY(tickers) AND published_at >= :since "
                "ORDER BY published_at DESC"
            ),
            {"ticker": ticker.upper(), "since": since},
        )
        rows = result.mappings().all()

    articles = []
    for row in rows:
        published_at = row["published_at"]
        articles.append(
            {
                "url_hash": row["url_hash"],
                "title": row["title"],
                "text": row["text"],
                "published_at": published_at.isoformat() if published_at else "",
                # summarizer 호환용 별칭
                "publishedDate": published_at.isoformat() if published_at else "",
                "source": row["source"],
                "url": row["url"],
                "tickers": row["tickers"],
            }
        )
    return articles


async def get_articles_for_ticker_between(
    ticker: str, since: datetime, until: datetime
) -> list[dict]:
    """특정 ticker 의 since ~ until 기간 기사 목록 (오래된 순)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT url_hash, title, text, published_at, source, url, tickers "
                "FROM articles "
                "WHERE :ticker = ANY(tickers) "
                "  AND published_at >= :since AND published_at < :until "
                "ORDER BY published_at ASC"
            ),
            {"ticker": ticker.upper(), "since": since, "until": until},
        )
        rows = result.mappings().all()

    articles = []
    for row in rows:
        published_at = row["published_at"]
        articles.append(
            {
                "url_hash": row["url_hash"],
                "title": row["title"],
                "text": row["text"],
                "published_at": published_at.isoformat() if published_at else "",
                "publishedDate": published_at.isoformat() if published_at else "",
                "source": row["source"],
                "url": row["url"],
                "tickers": row["tickers"],
            }
        )
    return articles


async def get_tickers_with_news_between(
    since: datetime, until: datetime
) -> list[str]:
    """since ~ until 기간 기사들의 연관 티커 목록(중복 제거)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT DISTINCT unnest(tickers) AS t FROM articles "
                "WHERE published_at >= :since AND published_at < :until"
            ),
            {"since": since, "until": until},
        )
        return [r[0] for r in result.all() if r[0]]


# ──────────────────────────────────────────
# News summaries
# ──────────────────────────────────────────

async def get_closing_report(ticker: str, report_date: date) -> dict | None:
    """특정 ticker 의 특정 날짜 closing 리포트 조회 (Phase2 용). 없으면 None."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT ticker, digest_type, report_date, version, "
                "       summary_text, sentiment, source_urls "
                "FROM news_summaries "
                "WHERE ticker = :ticker AND digest_type = 'daily' "
                "  AND report_date = :report_date AND version = 'closing' "
                "LIMIT 1"
            ),
            {"ticker": ticker.upper(), "report_date": report_date},
        )
        row = result.mappings().first()
    return dict(row) if row else None


async def get_daily_reports(ticker: str, since: date, until: date) -> list[dict]:
    """
    특정 기간(since ~ until)의 daily closing 리포트 목록 (report_date 오름차순).
    주간 요약 입력으로 사용.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT ticker, digest_type, report_date, version, "
                "       summary_text, sentiment, source_urls "
                "FROM news_summaries "
                "WHERE ticker = :ticker AND digest_type = 'daily' "
                "  AND version = 'closing' "
                "  AND report_date BETWEEN :since AND :until "
                "ORDER BY report_date ASC"
            ),
            {"ticker": ticker.upper(), "since": since, "until": until},
        )
        rows = result.mappings().all()
    return [dict(r) for r in rows]


async def has_weekly_final(ticker: str, week_monday: date) -> bool:
    """해당 주(월요일 기준) weekly/final 존재 여부."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT 1 FROM news_summaries "
                "WHERE ticker = :ticker AND digest_type = 'weekly' "
                "  AND version = 'final' AND report_date = :week_monday "
                "LIMIT 1"
            ),
            {"ticker": ticker.upper(), "week_monday": week_monday},
        )
        return result.fetchone() is not None


async def get_weekly_draft(ticker: str, week_monday: date) -> dict | None:
    """해당 주(월요일 기준) 주간 초안(draft) 조회. 없으면 None."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT ticker, digest_type, report_date, version, "
                "       summary_text, sentiment, source_urls "
                "FROM news_summaries "
                "WHERE ticker = :ticker AND digest_type = 'weekly' "
                "  AND report_date = :week_monday AND version = 'draft' "
                "LIMIT 1"
            ),
            {"ticker": ticker.upper(), "week_monday": week_monday},
        )
        row = result.mappings().first()
    return dict(row) if row else None


async def upsert_summary(
    ticker: str,
    digest_type: str,
    report_date,
    version,
    summary_text: str,
    sentiment: str,
    source_urls,
    price_change_pct: float | None = None,
) -> None:
    """
    news_summaries Upsert.
    충돌 키: (ticker, digest_type, report_date) — NULLS NOT DISTINCT.

    price_change_pct: 주간 리포트 전용 가격 변동률(%). 그 외는 None.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO news_summaries
                    (ticker, digest_type, report_date, version,
                     summary_text, sentiment, source_urls,
                     price_change_pct, updated_at)
                VALUES
                    (:ticker, :digest_type, :report_date, :version,
                     :summary_text, :sentiment, CAST(:source_urls AS JSONB),
                     :price_change_pct, NOW())
                ON CONFLICT (ticker, digest_type, report_date)
                DO UPDATE SET
                    version          = EXCLUDED.version,
                    summary_text     = EXCLUDED.summary_text,
                    sentiment        = EXCLUDED.sentiment,
                    source_urls      = EXCLUDED.source_urls,
                    price_change_pct = EXCLUDED.price_change_pct,
                    updated_at       = NOW()
                """
            ),
            {
                "ticker": ticker,
                "digest_type": digest_type,
                "report_date": report_date,
                "version": version,
                "summary_text": summary_text,
                "sentiment": sentiment,
                "source_urls": json.dumps(source_urls or []),
                "price_change_pct": price_change_pct,
            },
        )
        await session.commit()


async def upsert_weekly_benchmark(
    benchmark_type: str,
    benchmark_name: str,
    exchange: str | None,
    week_monday: date,
    change_pct: float | None,
) -> None:
    """
    weekly_benchmarks Upsert.
    충돌 키: (benchmark_type, benchmark_name, exchange, week_monday) — NULLS NOT DISTINCT.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO weekly_benchmarks
                    (benchmark_type, benchmark_name, exchange, week_monday, change_pct)
                VALUES
                    (:benchmark_type, :benchmark_name, :exchange,
                     :week_monday, :change_pct)
                ON CONFLICT (benchmark_type, benchmark_name, exchange, week_monday)
                DO UPDATE SET change_pct = EXCLUDED.change_pct
                """
            ),
            {
                "benchmark_type": benchmark_type,
                "benchmark_name": benchmark_name,
                "exchange": exchange,
                "week_monday": week_monday,
                "change_pct": change_pct,
            },
        )
        await session.commit()


async def get_weekly_benchmarks(week_monday: date) -> dict:
    """
    해당 주(월요일 기준) 벤치마크 변동률 조회.

    반환:
    {
        "sp500": float | None,
        "sectors": {(benchmark_name, exchange): change_pct, ...},
    }
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT benchmark_type, benchmark_name, exchange, change_pct "
                "FROM weekly_benchmarks WHERE week_monday = :week_monday"
            ),
            {"week_monday": week_monday},
        )
        rows = result.mappings().all()

    out: dict = {"sp500": None, "sectors": {}}
    for row in rows:
        if row["benchmark_type"] == "sp500":
            out["sp500"] = row["change_pct"]
        else:
            out["sectors"][(row["benchmark_name"], row["exchange"])] = row["change_pct"]
    return out


async def get_ticker_sector_exchange(ticker: str) -> tuple[str, str] | None:
    """
    종목의 (sector, exchange_short_name) 반환 — weekly_benchmarks 매칭용.

    데이터 소스: universe_current.csv (별도 DB 테이블 없음).
    """
    from app.universe.ticker_store import get_ticker_sector_exchange as _lookup

    return _lookup(ticker)



async def get_weekly_benchmarks_series(
    week_mondays: list[date], sector: str, exchange: str
) -> dict:
    """주어진 주차들의 S&P500 + 해당 섹터/거래소 변동률 시퀀스.
    반환: {"sp500": [...], "sector": [...]}  (week_mondays 순서와 매칭, 없으면 None)"""
    if not week_mondays:
        return {"sp500": [], "sector": []}

    params: dict = {"sector": sector, "exchange": exchange}
    for i, wm in enumerate(week_mondays):
        params[f"w{i}"] = wm
    placeholders = ", ".join(f":w{i}" for i in range(len(week_mondays)))

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                f"SELECT benchmark_type, benchmark_name, exchange, week_monday, change_pct "
                f"FROM weekly_benchmarks "
                f"WHERE week_monday IN ({placeholders})"
            ),
            params,
        )
        rows = result.mappings().all()

    sp500_map: dict = {}
    sector_map: dict = {}
    for row in rows:
        wm = row["week_monday"]
        if isinstance(wm, datetime):
            wm = wm.date()
        if row["benchmark_type"] == "sp500":
            sp500_map[wm] = row["change_pct"]
        elif (
            row["benchmark_type"] == "sector"
            and row["benchmark_name"] == sector
            and row["exchange"] == exchange
        ):
            sector_map[wm] = row["change_pct"]

    return {
        "sp500": [sp500_map.get(wm) for wm in week_mondays],
        "sector": [sector_map.get(wm) for wm in week_mondays],
    }


async def get_sector_news_series(sector: str, week_mondays: list[date]) -> list[dict]:
    """주어진 주차들의 sector_news_summaries (category=sector).
    반환: [{"week_monday":..., "summary_text":..., "sentiment":...}, ...]"""
    if not week_mondays:
        return []

    params: dict = {"sector": sector}
    for i, wm in enumerate(week_mondays):
        params[f"w{i}"] = wm
    placeholders = ", ".join(f":w{i}" for i in range(len(week_mondays)))

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                f"SELECT week_monday, summary_text, sentiment "
                f"FROM sector_news_summaries "
                f"WHERE category = :sector AND week_monday IN ({placeholders}) "
                f"ORDER BY week_monday ASC"
            ),
            params,
        )
        rows = result.mappings().all()
    return [dict(r) for r in rows]


async def delete_old_weekly_data() -> dict:
    """52주(364일) 초과된 주간 데이터 삭제. 테이블별 삭제 건수 반환."""
    cutoff = datetime.now(ZoneInfo("America/New_York")).date() - timedelta(weeks=52)

    async with AsyncSessionLocal() as session:
        r1 = await session.execute(
            text(
                "DELETE FROM news_summaries "
                "WHERE digest_type = 'weekly' AND report_date < :cutoff"
            ),
            {"cutoff": cutoff},
        )
        r2 = await session.execute(
            text("DELETE FROM weekly_benchmarks WHERE week_monday < :cutoff"),
            {"cutoff": cutoff},
        )
        r3 = await session.execute(
            text("DELETE FROM sector_news_summaries WHERE week_monday < :cutoff"),
            {"cutoff": cutoff},
        )
        await session.commit()

    return {
        "news_summaries": r1.rowcount or 0,
        "weekly_benchmarks": r2.rowcount or 0,
        "sector_news_summaries": r3.rowcount or 0,
    }


async def delete_old_daily_reports() -> int:
    """7일 초과된 daily 리포트 삭제 (ET 기준 today - 7). 삭제 건수 반환."""
    cutoff = datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=7)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "DELETE FROM news_summaries "
                "WHERE digest_type = 'daily' AND report_date < :cutoff"
            ),
            {"cutoff": cutoff},
        )
        await session.commit()
    return result.rowcount or 0


# ──────────────────────────────────────────
# Midterm DB 함수
# ──────────────────────────────────────────

async def get_recent_weekly_finals_for_midterm(
    ticker: str, before: date
) -> list[dict]:
    """
    report_date 가 (before - 84일) ~ before 범위인 weekly/final rows 반환.
    즉 최근 12주치 weekly final.
    반환 필드: week_monday, summary_text, sentiment, price_change_pct
    과거→최근 오름차순 정렬.
    """
    since = before - timedelta(days=84)  # 12주 = 84일
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT report_date AS week_monday, summary_text, sentiment, price_change_pct "
                "FROM news_summaries "
                "WHERE ticker = :ticker "
                "  AND digest_type = 'weekly' AND version = 'final' "
                "  AND report_date >= :since AND report_date <= :before "
                "ORDER BY report_date ASC"
            ),
            {"ticker": ticker.upper(), "since": since, "before": before},
        )
        rows = result.mappings().all()
    return [dict(r) for r in rows]


async def get_last_midterm_date(ticker: str) -> date | None:
    """
    해당 ticker 의 가장 최근 midterm report_date 반환.
    없으면 None.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT report_date FROM news_summaries "
                "WHERE ticker = :ticker AND digest_type = 'midterm' "
                "ORDER BY report_date DESC LIMIT 1"
            ),
            {"ticker": ticker.upper()},
        )
        row = result.fetchone()
    if row is None:
        return None
    val = row[0]
    return val.date() if hasattr(val, "date") else val


async def upsert_midterm(
    ticker: str,
    report_date: date,
    summary_text: str,
    sentiment: str | None,
    price_change_pct: float | None,
) -> None:
    """
    news_summaries 에 digest_type='midterm', version='final' 로 Upsert.
    충돌 키: (ticker, digest_type, report_date).
    sentiment / price_change_pct 는 nullable.
    """
    import json as _json

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO news_summaries
                    (ticker, digest_type, report_date, version,
                     summary_text, sentiment, source_urls,
                     price_change_pct, updated_at)
                VALUES
                    (:ticker, 'midterm', :report_date, 'final',
                     :summary_text, :sentiment, CAST(:source_urls AS JSONB),
                     :price_change_pct, NOW())
                ON CONFLICT (ticker, digest_type, report_date)
                DO UPDATE SET
                    summary_text     = EXCLUDED.summary_text,
                    sentiment        = EXCLUDED.sentiment,
                    price_change_pct = EXCLUDED.price_change_pct,
                    updated_at       = NOW()
                """
            ),
            {
                "ticker": ticker.upper(),
                "report_date": report_date,
                "summary_text": summary_text,
                "sentiment": sentiment,
                "source_urls": _json.dumps([]),
                "price_change_pct": price_change_pct,
            },
        )
        await session.commit()

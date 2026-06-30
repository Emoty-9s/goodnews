# -*- coding: utf-8 -*-
"""
유니버스 티커 저장소 — GoodNews 뉴스 수집 파이프라인의 종목 목록 공급원.

역할
----
* ``universe_current.csv`` (universe 빌드 결과) 에서 티커를 읽어 스케줄러에 공급한다.
* DB 의존 없이 CSV 파일 하나로 동작하므로, 유니버스 빌드와 뉴스 수집이 독립적으로 실행된다.

주요 함수
---------
load_tickers_from_csv(path)   → list[str]   : CSV 직접 로드
get_universe_tickers()        → list[str]   : 기본 경로에서 로드 (스케줄러용)
get_universe_stats()          → dict        : 종목 수·섹터·거래소 분포 요약
get_ticker_sector_exchange()  → tuple|None  : 종목별 (sector, exchange) 조회
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

from app.core.config import get_settings

log = logging.getLogger(__name__)

settings = get_settings()

# universe_current.csv 기본 저장 경로
DEFAULT_UNIVERSE_CSV: Path = Path(settings.universe_data_dir) / "universe_current.csv"

# CSV에서 읽을 필수 컬럼
_REQUIRED_COL = "symbol"
_OPTIONAL_FILTER_COLS = ("universe_status", "is_actively_trading")


def load_tickers_from_csv(
    path: Path | str = DEFAULT_UNIVERSE_CSV,
    status_filter: str = "included",
) -> list[str]:
    """
    ``universe_current.csv`` 에서 티커 목록을 로드한다.

    Parameters
    ----------
    path          : CSV 파일 경로 (기본: settings.universe_data_dir/universe_current.csv)
    status_filter : "included" 행만 필터링. None 이면 전체 반환.

    Returns
    -------
    대문자 티커 리스트. 파일이 없거나 빈 경우 빈 리스트.
    """
    p = Path(path)
    if not p.exists():
        log.warning(
            "universe_current.csv not found at %s — "
            "run 'python -m app.universe.build_universe' to build the universe first.",
            p.resolve(),
        )
        return []

    try:
        df = pd.read_csv(p, low_memory=False)
    except Exception as e:
        log.error("ticker_store: csv read failed path=%s err=%s", p, type(e).__name__)
        return []

    if df.empty or _REQUIRED_COL not in df.columns:
        log.warning("ticker_store: csv empty or missing 'symbol' column path=%s", p)
        return []

    # universe_status 컬럼이 있으면 필터 적용
    if status_filter and "universe_status" in df.columns:
        df = df[df["universe_status"] == status_filter]

    # is_actively_trading 컬럼이 있으면 True만 남김
    if "is_actively_trading" in df.columns:
        df = df[df["is_actively_trading"].fillna(False).astype(bool)]

    tickers = (
        df[_REQUIRED_COL]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
        .tolist()
    )
    tickers = [t for t in tickers if t and t != "NAN"]

    log.info("ticker_store: loaded %d tickers from %s", len(tickers), p.name)
    return tickers


@lru_cache(maxsize=1)
def _load_universe_metadata_df(path: str) -> pd.DataFrame:
    """
    universe_current.csv 에서 symbol / sector / exchange_short_name 만 로드 (캐시).
    included + is_actively_trading 필터 적용.
    """
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=["symbol", "sector", "exchange_short_name"])

    try:
        df = pd.read_csv(p, low_memory=False)
    except Exception as e:
        log.error("ticker_store: metadata read failed path=%s err=%s", p, type(e).__name__)
        return pd.DataFrame(columns=["symbol", "sector", "exchange_short_name"])

    if df.empty or "symbol" not in df.columns:
        return pd.DataFrame(columns=["symbol", "sector", "exchange_short_name"])

    if "universe_status" in df.columns:
        df = df[df["universe_status"] == "included"]
    if "is_actively_trading" in df.columns:
        df = df[df["is_actively_trading"].fillna(False).astype(bool)]

    cols = ["symbol"]
    for c in ("sector", "exchange_short_name"):
        if c in df.columns:
            cols.append(c)
        else:
            df[c] = pd.NA
            cols.append(c)

    out = df[cols].copy()
    out["symbol"] = out["symbol"].astype(str).str.strip().str.upper()
    return out.drop_duplicates(subset=["symbol"], keep="first")


async def get_ticker_sector_exchange(
    ticker: str,
    path: Optional[Path | str] = None,
) -> tuple[str, str] | None:
    """
    종목의 (sector, exchange_short_name) 반환 — weekly_benchmarks 매칭용.

    데이터 소스: Supabase universe_tickers 테이블.
    로컬 디버깅용 CSV 조회: _load_universe_metadata_df() 직접 사용.

    Returns
    -------
    ("Technology", "NASDAQ") 또는 None
    """
    from app.models.database import get_ticker_sector_exchange_from_db
    try:
        return await get_ticker_sector_exchange_from_db(ticker)
    except Exception as e:
        log.error("ticker_store: DB get_ticker_sector_exchange failed for %s: %s", ticker, e)
        return None


async def get_universe_tickers(path: Optional[Path | str] = None) -> list[str]:
    """
    스케줄러(tasks.py)의 load_all_tickers() 에서 호출하는 메인 인터페이스.

    데이터 소스: Supabase universe_tickers 테이블.
    DB가 비어있거나 연결 실패 시 빈 리스트 + 경고 로그.
    로컬 CSV 조회: load_tickers_from_csv() 직접 사용.
    """
    from app.models.database import get_universe_tickers_from_db
    try:
        tickers = await get_universe_tickers_from_db()
    except Exception as e:
        log.error("ticker_store: DB load failed: %s", e)
        return []

    if not tickers:
        log.warning(
            "ticker_store: universe_tickers DB is empty. "
            "News collection will be skipped. "
            "Run: python scripts/upload_universe_to_supabase.py to populate."
        )
    else:
        log.info("ticker_store: loaded %d tickers from DB", len(tickers))
    return tickers


async def get_universe_stats(path: Optional[Path | str] = None) -> dict:
    """
    유니버스 요약 통계 반환 (API 상태 엔드포인트·운영 모니터링용).

    데이터 소스: Supabase universe_tickers 테이블.

    Returns
    -------
    {
        "total": int,
        "by_exchange": {"NASDAQ": n, "NYSE": n, ...},
        "by_sector": {"Technology": n, ...},
        "snapshot_date": "2025-01-15" | None,
        "source_file": "supabase:universe_tickers",
    }
    """
    from app.models.database import get_universe_stats_from_db
    try:
        return await get_universe_stats_from_db()
    except Exception as e:
        log.error("ticker_store: DB get_universe_stats failed: %s", e)
        return {
            "total": 0,
            "by_exchange": {},
            "by_sector": {},
            "snapshot_date": None,
            "source_file": "error",
        }

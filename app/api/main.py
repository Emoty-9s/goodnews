"""
GoodNews AI - FastAPI 서버
============================
엔드포인트:
  GET /summary/{ticker}?digest_type=daily
  GET /feed?tickers=AAPL,NVDA,MSFT&digest_type=daily
  GET /health
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from typing import Optional
from datetime import datetime
import json

from app.models.database import NewsSummary, get_db, create_tables
from app.core.config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # production에서는 마이그레이션으로 스키마를 관리하므로 자동 생성 생략
    if settings.app_env == "production":
        logger.info("[startup] production 환경 — create_tables() 스킵 (마이그레이션 관리)")
    else:
        try:
            await create_tables()
            logger.info("[startup] create_tables() 완료")
        except Exception as e:
            logger.warning(f"[startup] create_tables() 실패 (서버 계속 기동): {e}")
    yield


app = FastAPI(
    title="GoodNews AI",
    description="미국 주식 뉴스 AI 요약 서비스",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 운영 시 도메인 제한 필요
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────
# 응답 스키마 (Pydantic)
# ──────────────────────────────────────────

from pydantic import BaseModel

class SummaryResponse(BaseModel):
    ticker: str
    digest_type: str
    summary_text: Optional[str]
    sentiment: Optional[str]
    source_urls: Optional[list]
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


# ──────────────────────────────────────────
# API 엔드포인트
# ──────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/summary/{ticker}", response_model=SummaryResponse)
async def get_summary(
    ticker: str,
    digest_type: str = Query(
        default="daily",
        pattern="^(daily|weekly|midterm)$",
        description="요약 주기: daily | weekly | midterm"
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    특정 종목의 요약 조회.
    예: GET /summary/AAPL?digest_type=weekly
    """
    ticker = ticker.upper()
    result = await db.execute(
        select(NewsSummary).where(
            NewsSummary.ticker == ticker,
            NewsSummary.digest_type == digest_type,
        )
    )
    summary = result.scalar_one_or_none()

    if not summary:
        raise HTTPException(
            status_code=404,
            detail=f"{ticker} [{digest_type}] 요약이 아직 생성되지 않았습니다."
        )
    return summary


@app.get("/feed", response_model=list[SummaryResponse])
async def get_feed(
    tickers: str = Query(
        description="쉼표로 구분된 티커 목록 (예: AAPL,NVDA,MSFT)",
        example="AAPL,NVDA,MSFT"
    ),
    digest_type: str = Query(
        default="daily",
        pattern="^(daily|weekly|midterm)$"
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    관심 종목 목록의 요약 피드 조회.
    예: GET /feed?tickers=AAPL,NVDA,MSFT&digest_type=daily
    """
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not ticker_list:
        raise HTTPException(status_code=400, detail="tickers 파라미터가 필요합니다.")
    if len(ticker_list) > 50:
        raise HTTPException(status_code=400, detail="한 번에 최대 50개 종목만 조회 가능합니다.")

    result = await db.execute(
        select(NewsSummary).where(
            NewsSummary.ticker.in_(ticker_list),
            NewsSummary.digest_type == digest_type,
        ).order_by(NewsSummary.updated_at.desc())
    )
    summaries = result.scalars().all()
    return summaries


@app.get("/summary/{ticker}/all", response_model=list[SummaryResponse])
async def get_all_digests(
    ticker: str,
    db: AsyncSession = Depends(get_db),
):
    """
    단일 종목의 모든 주기(daily/weekly/midterm) 요약 한번에 조회.
    """
    ticker = ticker.upper()
    result = await db.execute(
        select(NewsSummary).where(
            NewsSummary.ticker == ticker
        )
    )
    summaries = result.scalars().all()
    if not summaries:
        raise HTTPException(
            status_code=404,
            detail=f"{ticker}의 요약 데이터가 없습니다."
        )
    return summaries


# ──────────────────────────────────────────
# 개발 서버 실행
# ──────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
    )


@app.get("/universe/stats")
async def get_universe_stats():
    """
    현재 유니버스 요약 통계.
    예: GET /universe/stats
    """
    from app.universe.ticker_store import get_universe_stats
    return get_universe_stats()


@app.post("/universe/build")
async def trigger_universe_build(background_tasks: __import__("fastapi").BackgroundTasks):
    """
    유니버스 빌드를 백그라운드에서 즉시 실행.
    Celery 없이 테스트할 때 유용.
    예: POST /universe/build
    """
    from app.universe.universe_runner import run_universe_build
    import asyncio

    def _build():
        run_universe_build()

    background_tasks.add_task(_build)
    return {"status": "universe build triggered", "message": "백그라운드에서 실행 중입니다."}

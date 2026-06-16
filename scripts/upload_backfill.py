#!/usr/bin/env python3
"""
data/clean/ 의 월별 정제 뉴스를 Supabase(PostgreSQL) articles 테이블에 업로드.

- url_hash = SHA256(url) 를 PK 로 사용
- INSERT ... ON CONFLICT (url_hash) DO NOTHING (이미 있으면 스킵)
- 500건씩 bulk insert
- 실행: python scripts/upload_backfill.py
"""
import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from sqlalchemy.dialects.postgresql import insert

from app.models.database import Article, AsyncSessionLocal, create_tables

CLEAN_DIR = ROOT / "data" / "clean"
BATCH_SIZE = 500


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_published_at(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def to_row(item: dict) -> dict | None:
    url = item.get("url", "") or ""
    url_hash = item.get("url_hash") or (sha256_hex(url) if url else None)
    if not url_hash:
        return None
    return {
        "url_hash": url_hash,
        "title": item.get("title", "") or "",
        "text": item.get("text", "") or "",
        "published_at": parse_published_at(item.get("published_at", "")),
        "source": item.get("source", "") or "",
        "url": url,
        "tickers": item.get("tickers", []) or [],
    }


def chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


async def upload_month(session, month_key: str, rows: list[dict]) -> tuple[int, int]:
    total = len(rows)
    inserted_total = 0
    done = 0

    for batch in chunk(rows, BATCH_SIZE):
        stmt = insert(Article).values(batch)
        stmt = stmt.on_conflict_do_nothing(index_elements=["url_hash"])
        result = await session.execute(stmt)
        await session.commit()

        inserted = result.rowcount if result.rowcount is not None else len(batch)
        inserted_total += inserted
        done += len(batch)
        logger.info(f"[{month_key}] {total:,}건 업로드 중... {done}/{total}")

    skipped_total = total - inserted_total
    logger.info(
        f"[{month_key}] 완료: {inserted_total:,}건 INSERT, {skipped_total:,}건 스킵"
    )
    return inserted_total, skipped_total


async def run():
    if not CLEAN_DIR.exists():
        logger.error(
            f"clean 폴더가 없습니다: {CLEAN_DIR}\n"
            f"먼저 python scripts/deduplicate_backfill.py 를 실행하세요"
        )
        sys.exit(1)

    clean_files = sorted(CLEAN_DIR.glob("*.json"))
    if not clean_files:
        logger.error(f"업로드할 JSON 파일이 없습니다: {CLEAN_DIR}")
        sys.exit(1)

    # 테이블이 없으면 생성 (이미 있으면 무시)
    await create_tables()

    grand_inserted = 0
    grand_skipped = 0

    async with AsyncSessionLocal() as session:
        for clean_file in clean_files:
            month_key = clean_file.stem

            with open(clean_file, "r", encoding="utf-8") as f:
                items = json.load(f)

            rows = [r for r in (to_row(i) for i in items) if r is not None]
            if not rows:
                logger.info(f"[{month_key}] 업로드할 행 없음 (건너뜀)")
                continue

            inserted, skipped = await upload_month(session, month_key, rows)
            grand_inserted += inserted
            grand_skipped += skipped

    logger.info(
        f"전체 완료: {grand_inserted:,}건 INSERT, {grand_skipped:,}건 스킵"
    )


if __name__ == "__main__":
    asyncio.run(run())

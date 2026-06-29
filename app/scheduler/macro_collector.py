from datetime import date, timedelta

import httpx
from loguru import logger
from sqlalchemy import text

from app.core.config import get_settings
from app.models.database import AsyncSessionLocal

settings = get_settings()

_BASE = "https://financialmodelingprep.com/stable"

# FMP name → (db_name, unit)
_INDICATORS: list[tuple[str, str, str]] = [
    ("GDP",               "gdp",           "%"),
    ("CPI",               "cpi",           "%"),
    ("coreCPI",           "core_cpi",      "%"),
    ("PPI",               "ppi",           "%"),
    ("unemploymentRate",  "unemployment",  "%"),
    ("nonFarmPayrolls",   "nfp",           "K"),
    ("federalFundsRate",  "fed_funds_rate","%"),
    ("ISMManufacturing",  "ism_mfg",       "index"),
    ("ISMServices",       "ism_svc",       "index"),
]

_UPSERT_SQL = text("""
    INSERT INTO macro_indicators (name, date, value, previous, estimate, unit)
    VALUES (:name, :date, :value, :previous, :estimate, :unit)
    ON CONFLICT (name, date) DO UPDATE SET
        value    = EXCLUDED.value,
        previous = EXCLUDED.previous,
        estimate = EXCLUDED.estimate,
        unit     = EXCLUDED.unit
""")


async def _upsert_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    async with AsyncSessionLocal() as session:
        await session.execute(_UPSERT_SQL, rows)
        await session.commit()
    return len(rows)


async def _fetch_indicator(
    client: httpx.AsyncClient,
    fmp_name: str,
    db_name: str,
    unit: str,
    cutoff: date,
) -> list[dict]:
    try:
        resp = await client.get(
            f"{_BASE}/economic-indicators",
            params={"name": fmp_name, "apikey": settings.fmp_api_key},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"[macro] {db_name} 수집 실패: {e}")
        return []

    items = data if isinstance(data, list) else []
    rows: list[dict] = []
    for item in items:
        try:
            d = date.fromisoformat(str(item.get("date", ""))[:10])
        except ValueError:
            continue
        if d < cutoff:
            continue
        rows.append({
            "name":     db_name,
            "date":     d,
            "value":    item.get("value"),
            "previous": item.get("previous"),
            "estimate": item.get("estimate"),
            "unit":     unit,
        })
    return rows


async def _fetch_treasury_10y(
    client: httpx.AsyncClient,
    cutoff: date,
) -> list[dict]:
    try:
        resp = await client.get(
            f"{_BASE}/treasury-rates",
            params={"apikey": settings.fmp_api_key},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"[macro] treasury_10y 수집 실패: {e}")
        return []

    items = data if isinstance(data, list) else []
    rows: list[dict] = []
    seen: set[date] = set()
    for item in items:
        try:
            d = date.fromisoformat(str(item.get("date", ""))[:10])
        except ValueError:
            continue
        if d < cutoff or d in seen:
            continue
        seen.add(d)
        rows.append({
            "name":     "treasury_10y",
            "date":     d,
            "value":    item.get("year10"),
            "previous": None,
            "estimate": None,
            "unit":     "%",
        })
    return rows


async def fetch_macro_indicators() -> int:
    """FMP에서 거시경제 지표 9종 + 국채 10년물을 수집해 DB에 upsert. 저장 건수 반환."""
    cutoff = date.today() - timedelta(days=90)
    total = 0

    async with httpx.AsyncClient() as client:
        for fmp_name, db_name, unit in _INDICATORS:
            rows = await _fetch_indicator(client, fmp_name, db_name, unit, cutoff)
            saved = await _upsert_rows(rows)
            logger.info(f"[macro] {db_name}: {saved}건 저장")
            total += saved

        rows = await _fetch_treasury_10y(client, cutoff)
        saved = await _upsert_rows(rows)
        logger.info(f"[macro] treasury_10y: {saved}건 저장")
        total += saved

    logger.info(f"[macro] 전체 저장 완료: {total}건")
    return total


async def get_latest_macro_snapshot() -> dict[str, dict]:
    """DB에서 지표별 최신값 1건씩 조회.

    반환: {'cpi': {'value': 3.2, 'date': '2026-05-10', 'previous': 3.0}, ...}
    """
    all_names = [db_name for _, db_name, _ in _INDICATORS] + ["treasury_10y"]
    result: dict[str, dict] = {}

    async with AsyncSessionLocal() as session:
        for name in all_names:
            row = (await session.execute(
                text("""
                    SELECT value, date, previous
                    FROM macro_indicators
                    WHERE name = :name
                    ORDER BY date DESC
                    LIMIT 1
                """),
                {"name": name},
            )).fetchone()
            if row is not None:
                result[name] = {
                    "value":    row[0],
                    "date":     str(row[1]),
                    "previous": row[2],
                }

    return result

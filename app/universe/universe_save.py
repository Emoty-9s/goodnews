# -*- coding: utf-8 -*-
"""
유니버스 최종 저장 스키마 및 CSV / Parquet / JSON 출력.

* 포함·제외·요약 DataFrame 을 고정 컬럼 순으로 정렬해 기록한다.
* Parquet 저장이 실패(pyarrow 미설치 등)해도 **CSV·JSON 저장은 계속** 시도한다.
* API 키는 본 모듈에서 로그하지 않는다.

``build_log`` JSON 에는 통상 다음 필드가 포함된다:
``snapshot_date``, ``started_at_utc``, ``finished_at_utc``, ``fmp_endpoints_used``,
``exchanges_requested``, ``country_filter``, ``min_market_cap``,
``raw_screener_rows``, ``normalized_screener_rows``, ``included_count``, ``excluded_count``,
``output_files``, ``warnings`` — 일부는 ``save_outputs`` 호출 시 보강된다.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 스키마 — 컬럼 순서 고정
# ---------------------------------------------------------------------------

INCLUDED_UNIVERSE_COLUMNS: list[str] = [
    "symbol",
    "company_name",
    "exchange",
    "exchange_short_name",
    "country",
    "currency",
    "sector",
    "industry",
    "market_cap",
    "price",
    "beta",
    "volume",
    "is_actively_trading",
    "is_etf",
    "is_fund",
    "is_common_stock_like",
    "universe_status",
    "exclusion_reason",
    "exclusion_detail",
    "data_source",
    "snapshot_date",
    "created_at_utc",
]

EXCLUDED_UNIVERSE_COLUMNS: list[str] = [
    "symbol",
    "company_name",
    "exchange",
    "exchange_short_name",
    "country",
    "sector",
    "industry",
    "market_cap",
    "price",
    "is_actively_trading",
    "is_etf",
    "is_fund",
    "universe_status",
    "exclusion_reason",
    "exclusion_detail",
    "data_source",
    "snapshot_date",
    "created_at_utc",
]

SUMMARY_COLUMNS: list[str] = ["reason_code", "count", "share_pct"]


def _ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in columns:
        if c not in out.columns:
            out[c] = pd.NA
    return out[columns]


def prepare_included_universe_df(
    df: pd.DataFrame,
    *,
    data_source: str,
    snapshot_date: str,
    created_at_utc: str,
) -> pd.DataFrame:
    """포함 유니버스 최종 스키마 정렬 및 메타 컬럼 보강."""
    out = df.copy()
    out["universe_status"] = "included"
    out["is_common_stock_like"] = True
    if "exclusion_reason" not in out.columns:
        out["exclusion_reason"] = "PASS"
    else:
        out["exclusion_reason"] = out["exclusion_reason"].fillna("PASS")
    if "exclusion_detail" not in out.columns:
        out["exclusion_detail"] = ""
    else:
        out["exclusion_detail"] = out["exclusion_detail"].fillna("")
    out["data_source"] = data_source
    out["snapshot_date"] = snapshot_date
    out["created_at_utc"] = created_at_utc
    return _ensure_columns(out, INCLUDED_UNIVERSE_COLUMNS)


def prepare_excluded_universe_df(
    df: pd.DataFrame,
    *,
    data_source: str,
    snapshot_date: str,
    created_at_utc: str,
) -> pd.DataFrame:
    """제외 유니버스 최종 스키마 정렬 및 메타 컬럼 보강."""
    out = df.copy()
    out["universe_status"] = "excluded"
    if "exclusion_reason" not in out.columns:
        out["exclusion_reason"] = pd.NA
    if "exclusion_detail" not in out.columns:
        out["exclusion_detail"] = ""
    out["data_source"] = data_source
    out["snapshot_date"] = snapshot_date
    out["created_at_utc"] = created_at_utc
    return _ensure_columns(out, EXCLUDED_UNIVERSE_COLUMNS)


def prepare_summary_df(df: pd.DataFrame) -> pd.DataFrame:
    return _ensure_columns(df, SUMMARY_COLUMNS)


def save_csv(df: pd.DataFrame, path: Path | str) -> None:
    """CSV 저장. 실패 시 경로와 예외를 로그하고 재전파."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(p, index=False)
        log.info("saved csv path=%s rows=%d", p.resolve(), len(df))
    except Exception as e:
        log.error("csv save failed path=%s error=%s: %s", p.resolve(), type(e).__name__, e)
        raise


def save_parquet(df: pd.DataFrame, path: Path | str) -> bool:
    """
    Parquet 저장. 성공 시 True.

    pyarrow 미설치 등으로 실패하면 False를 반환하고, **예외는 삼킨다**(CSV 경로는 별도).
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p, index=False)
        log.info("saved parquet path=%s rows=%d", p.resolve(), len(df))
        return True
    except Exception as e:
        log.error(
            "parquet save failed path=%s error=%s: %s",
            p.resolve(),
            type(e).__name__,
            e,
        )
        return False


def save_json(obj: Any, path: Path | str) -> None:
    """JSON 직렬화 저장. 실패 시 로그 후 재전파."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        log.info("saved json path=%s", p.resolve())
    except Exception as e:
        log.error("json save failed path=%s error=%s: %s", p.resolve(), type(e).__name__, e)
        raise


def save_outputs(
    included_df: pd.DataFrame,
    excluded_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    build_log: Mapping[str, Any],
    data_dir: Path | str,
    *,
    data_source: str = "FMP",
    snapshot_date: Optional[str] = None,
    created_at_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ``data_dir`` 에 표준 파일명으로 유니버스 산출물을 저장한다.

    * ``universe_current`` — 포함( CSV + Parquet 시도 )
    * ``universe_excluded`` — 제외( CSV + Parquet 시도 )
    * ``universe_summary`` — 요약( CSV 전용 )
    * ``universe_build_log`` — JSON

    Parquet 단계 실패는 삼키고 CSV·JSON 은 진행한다.

    Returns
    -------
    ``paths``, ``parquet_ok``, ``snapshot_date``, ``created_at_utc`` 등 메타 dict.
    """
    base = Path(data_dir)
    snap = snapshot_date or date.today().isoformat()
    created = created_at_utc or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    inc = prepare_included_universe_df(
        included_df,
        data_source=data_source,
        snapshot_date=snap,
        created_at_utc=created,
    )
    exc = prepare_excluded_universe_df(
        excluded_df,
        data_source=data_source,
        snapshot_date=snap,
        created_at_utc=created,
    )
    summ = prepare_summary_df(summary_df)

    paths = {
        "universe_current_csv": base / "universe_current.csv",
        "universe_current_parquet": base / "universe_current.parquet",
        "universe_excluded_csv": base / "universe_excluded.csv",
        "universe_excluded_parquet": base / "universe_excluded.parquet",
        "universe_summary_csv": base / "universe_summary.csv",
        "universe_build_log_json": base / "universe_build_log.json",
    }

    save_csv(inc, paths["universe_current_csv"])
    pq_inc_ok = save_parquet(inc, paths["universe_current_parquet"])

    save_csv(exc, paths["universe_excluded_csv"])
    pq_exc_ok = save_parquet(exc, paths["universe_excluded_parquet"])

    save_csv(summ, paths["universe_summary_csv"])

    blog = dict(build_log)
    blog.setdefault("snapshot_date", snap)
    blog.setdefault("included_count", int(len(inc)))
    blog.setdefault("excluded_count", int(len(exc)))
    blog["output_files"] = {k: str(v.resolve()) for k, v in paths.items()}
    wlist: list[Any] = []
    raw_w = blog.get("warnings")
    if raw_w is not None:
        wlist = list(raw_w) if isinstance(raw_w, (list, tuple)) else [raw_w]
    if not pq_inc_ok:
        wlist.append(f"parquet_failed:{paths['universe_current_parquet'].name}")
    if not pq_exc_ok:
        wlist.append(f"parquet_failed:{paths['universe_excluded_parquet'].name}")
    blog["warnings"] = wlist

    save_json(blog, paths["universe_build_log_json"])

    return {
        "paths": {k: str(v.resolve()) for k, v in paths.items()},
        "parquet_ok": {"included": pq_inc_ok, "excluded": pq_exc_ok},
        "snapshot_date": snap,
        "created_at_utc": created,
    }


async def save_to_supabase(included_df: pd.DataFrame) -> int:
    """
    included_df(포함 유니버스 DataFrame)를 Supabase universe_tickers 테이블에 upsert.
    database.upsert_universe_tickers() 호출 — TRUNCATE + INSERT 방식.
    반환값: upsert된 행 수.
    """
    from app.models.database import upsert_universe_tickers

    rows = included_df.to_dict(orient="records")
    count = await upsert_universe_tickers(rows)
    log.info("save_to_supabase: %d rows upserted to universe_tickers", count)
    return count

# -*- coding: utf-8 -*-
"""
FMP 기반 유니버스 빌드 파이프라인(수집 → 정규화 → 필터 → 선택적 profile → 저장).

* API 호출: ``fmp_company_screener``, ``fmp_etf_stock_reference``, ``fmp_client``, ``fmp_profile_enrich``
* 정규화: 스크리너·stock-list 모듈
* 필터: ``finviz_like_equity_filter``
* 저장: ``universe_save``

CLI는 ``build_universe`` 에서만 구성한다.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from finviz_like_equity_filter import apply_filters
from fmp_client import API_KEY_ENV, ensure_fmp_session, fmp_get, get_api_key
from fmp_company_screener import (
    dedupe_by_symbol_max_mcap,
    fetch_screener,
    normalize_screener_row,
    should_use_market_cap_buckets,
)
from fmp_etf_stock_reference import (
    PATH_ETF_LIST,
    PATH_STOCK_LIST,
    build_stock_type_map,
    fetch_etf_symbols,
    fetch_stock_list_rows,
    normalize_stock_list,
)
from fmp_profile_enrich import enrich_missing_profile_fields, find_new_symbols, load_previous_universe
from universe_reason_codes import REASON_PASS
from universe_save import save_outputs

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 비즈니스·실행 상수
# ---------------------------------------------------------------------------

EXIT_OK: int = 0
EXIT_NO_API_KEY: int = 1
EXIT_SCREENER_EMPTY: int = 2
EXIT_INCLUDED_EMPTY: int = 3
EXIT_FAILURE: int = 4

WARNING_SCREENER_NEAR_LIMIT: str = "screener_rows_near_limit_10000_consider_bucketed_fetch"
WARNING_PROFILE_SKIPPED_NO_BASELINE: str = "profile_enrich_skipped_no_previous_universe_current_csv"

FMP_ENDPOINT_SCREENER: str = "/stable/company-screener"
FMP_ENDPOINT_ETF_LIST: str = "/stable/etf-list"
FMP_ENDPOINT_STOCK_LIST: str = "/stable/stock-list"
FMP_ENDPOINT_PROFILE: str = "/stable/profile"

DATA_SOURCE_FMP: str = "FMP"
RAW_FMP_SUBDIR: str = "raw_fmp"

PRIOR_UNIVERSE_CSV: str = "universe_current.csv"


@dataclass(frozen=True)
class ScreenerCollectionResult:
    """스크리너 수집·정규화 결과."""

    raw_row_count: int
    normalized_row_count: int
    screener_df: pd.DataFrame
    warnings: Tuple[str, ...]


# --- 파싱·보조 ----------------------------------------------------------------


def parse_exchanges_csv(value: str) -> Tuple[str, ...]:
    """쉼표 구분 거래소 문자열을 대문자 튜플로 변환한다."""
    return tuple(x.strip().upper() for x in str(value).split(",") if x.strip())


def normalize_country_optional(country: Optional[str]) -> Optional[str]:
    """공백-only country 는 None 으로 치환한다."""
    if country is None:
        return None
    s = str(country).strip()
    return s if s else None


def min_market_cap_api_param(value: float) -> Optional[int]:
    """0 이하이면 API 파라미터를 보내지 않기 위해 None 을 반환한다."""
    if value is None or float(value) <= 0:
        return None
    return int(float(value))


def etf_symbols_from_api_payload(payload: Any) -> Set[str]:
    """etf-list JSON 리스트에서 심볼 집합을 추출한다."""
    out: Set[str] = set()
    if not isinstance(payload, list):
        return out
    for item in payload:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
        if sym:
            out.add(sym)
    return out


def raw_fmp_dir(data_dir: Path) -> Path:
    return data_dir / RAW_FMP_SUBDIR


def write_raw_json(data_dir: Path, filename: str, obj: Any) -> None:
    """``raw_fmp`` 아래 JSON 원본을 기록한다(실패 시 로그만)."""
    try:
        d = raw_fmp_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        log.info("raw saved path=%s", path.resolve())
    except Exception as e:
        log.error("raw save failed file=%s err=%s", filename, type(e).__name__)


# --- 수집 단계 ----------------------------------------------------------------


def collect_screener_dataframe(
    exchanges: Tuple[str, ...],
    min_mcap: Optional[int],
    country: Optional[str],
    data_dir: Path,
    save_raw: bool,
) -> ScreenerCollectionResult:
    """
    거래소별 company-screener 호출 → 정규화 → 시총 기준 심볼 dedupe.

    ``save_raw`` 이면 거래소별 JSON을 즉시 기록하여 이후 단계 실패 시에도 디버깅 가능하게 한다.
    """
    warnings: List[str] = []
    raw_all: List[Dict[str, Any]] = []

    for ex in exchanges:
        rows = fetch_screener(str(ex), min_mcap, None, country)
        raw_all.extend(rows)
        if save_raw:
            write_raw_json(data_dir, f"company_screener_{ex}.json", rows)

    raw_count = len(raw_all)
    log.info("raw screener rows (all exchanges): %d", raw_count)

    if should_use_market_cap_buckets(raw_all):
        warnings.append(WARNING_SCREENER_NEAR_LIMIT)
        log.warning(
            "screener row count %d is near API limit; consider fmp_company_screener.fetch_screener_universe_bucketed",
            raw_count,
        )

    normalized = [normalize_screener_row(r) for r in raw_all]
    deduped = dedupe_by_symbol_max_mcap(normalized)
    screener_df = pd.DataFrame(deduped)
    norm_count = len(screener_df)
    log.info("normalized screener rows (deduped by symbol): %d", norm_count)

    return ScreenerCollectionResult(
        raw_row_count=raw_count,
        normalized_row_count=norm_count,
        screener_df=screener_df.copy(),
        warnings=tuple(warnings),
    )


def collect_etf_blacklist_and_stock_type_map(
    data_dir: Path,
    save_raw: bool,
) -> Tuple[Set[str], Dict[str, Set[str]]]:
    """
    etf-list·stock-list 를 가져와 ETF 집합과 symbol→type 토큰 맵을 만든다.

    ``save_raw`` 이면 목 응답 전체를 JSON 으로 남긴다.
    """
    if save_raw:
        etf_payload = fmp_get(PATH_ETF_LIST, {})
        write_raw_json(data_dir, "etf_list.json", etf_payload)
        etf_syms = etf_symbols_from_api_payload(etf_payload)
        stock_payload = fmp_get(PATH_STOCK_LIST, {})
        write_raw_json(data_dir, "stock_list.json", stock_payload)
        stock_rows = [x for x in stock_payload if isinstance(x, dict)]
    else:
        etf_syms = fetch_etf_symbols()
        stock_rows = fetch_stock_list_rows()

    stock_df = normalize_stock_list(stock_rows)
    type_map = build_stock_type_map(stock_df)
    log.info("etf blacklist size: %d | stock-list typed symbols: %d", len(etf_syms), len(type_map))
    return etf_syms, type_map


# --- 필터·보강·로그 --------------------------------------------------------------


def log_top_exclusion_reasons(summary_df: pd.DataFrame, limit: int = 15) -> None:
    """요약 테이블에서 PASS 가 아닌 상위 제외 사유를 로그한다."""
    if summary_df.empty or "reason_code" not in summary_df.columns:
        return
    sub = summary_df.loc[summary_df["reason_code"] != REASON_PASS].copy()
    if sub.empty:
        return
    sub = sub.sort_values("count", ascending=False).head(limit)
    log.info("top exclusion reasons:")
    for _, r in sub.iterrows():
        log.info("  %s: %s (%.4f%%)", r["reason_code"], r["count"], r.get("share_pct", 0))


def maybe_enrich_profile_for_new_symbols(
    included_df: pd.DataFrame,
    data_dir: Path,
    enabled: bool,
    warnings_list: List[str],
) -> pd.DataFrame:
    """
    ``--profile-enrich-new-only`` 가 켜져 있고 이전 ``universe_current.csv`` 가 있을 때만
    신규 포함 종목에 대해 profile 단건 보강을 시도한다.
    """
    if not enabled:
        return included_df.copy()

    baseline = data_dir / PRIOR_UNIVERSE_CSV
    if not baseline.exists():
        warnings_list.append(WARNING_PROFILE_SKIPPED_NO_BASELINE)
        log.info("profile enrich skipped: no previous %s", baseline)
        return included_df.copy()

    prev = load_previous_universe(data_dir)
    new_syms = find_new_symbols(included_df["symbol"].copy(), prev)
    out = enrich_missing_profile_fields(included_df.copy(), new_syms)
    log.info("profile enrich: new_symbol_candidates=%d", len(new_syms))
    return out


def build_endpoints_used(profile_enrich_requested: bool, baseline_exists: bool) -> List[str]:
    """빌드 로그용 엔드포인트 목록."""
    base = [FMP_ENDPOINT_SCREENER, FMP_ENDPOINT_ETF_LIST, FMP_ENDPOINT_STOCK_LIST]
    if profile_enrich_requested and baseline_exists:
        base = base + [FMP_ENDPOINT_PROFILE]
    return base


def build_build_log_dict(
    *,
    started: str,
    finished: str,
    exchanges: Tuple[str, ...],
    country: Optional[str],
    min_mcap_cli: float,
    screener: ScreenerCollectionResult,
    included_n: int,
    excluded_n: int,
    endpoints: List[str],
    extra_warnings: List[str],
) -> Dict[str, Any]:
    """``universe_build_log.json`` 초기 본문."""
    w = list(screener.warnings) + list(extra_warnings)
    return {
        "snapshot_date": datetime.now(timezone.utc).date().isoformat(),
        "started_at_utc": started,
        "finished_at_utc": finished,
        "fmp_endpoints_used": endpoints,
        "exchanges_requested": list(exchanges),
        "country_filter": country,
        "min_market_cap": float(min_mcap_cli),
        "raw_screener_rows": screener.raw_row_count,
        "normalized_screener_rows": screener.normalized_row_count,
        "included_count": included_n,
        "excluded_count": excluded_n,
        "warnings": w,
    }


# --- 진입점 ------------------------------------------------------------------


def run_universe_pipeline(args: argparse.Namespace) -> int:
    """
    유니버스 빌드 파이프라인.

    Returns
    -------
    종료 코드: 성공 0, API 키 없음 1, 스크리너 공백 2, 포함 0건 3, 저장/기타 실패 4.
    """
    try:
        get_api_key()
    except RuntimeError:
        log.error("%s is not set or empty.", API_KEY_ENV)
        return EXIT_NO_API_KEY

    ensure_fmp_session()

    data_dir = Path(args.data_dir)
    exchanges = parse_exchanges_csv(args.exchanges)
    country = normalize_country_optional(args.country)
    min_mcap = min_market_cap_api_param(float(args.min_market_cap))
    started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    warn_extra: List[str] = []

    screener_result = collect_screener_dataframe(
        exchanges,
        min_mcap,
        country,
        data_dir,
        bool(args.save_raw),
    )
    warn_extra.extend(screener_result.warnings)

    if screener_result.screener_df.empty:
        log.error("screener result empty after normalize/dedupe")
        return EXIT_SCREENER_EMPTY

    etf_syms, stock_type_map = collect_etf_blacklist_and_stock_type_map(
        data_dir,
        bool(args.save_raw),
    )

    screener_df = screener_result.screener_df.copy()
    included_df, excluded_df, summary_df = apply_filters(screener_df, etf_syms, stock_type_map)
    log.info("included count: %d | excluded count: %d", len(included_df), len(excluded_df))

    log_top_exclusion_reasons(summary_df.copy())

    if included_df.empty:
        log.error("included universe is empty")
        return EXIT_INCLUDED_EMPTY

    baseline_exists = (data_dir / PRIOR_UNIVERSE_CSV).exists()
    included_df = maybe_enrich_profile_for_new_symbols(
        included_df,
        data_dir,
        bool(args.profile_enrich_new_only),
        warn_extra,
    )

    finished = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    endpoints = build_endpoints_used(bool(args.profile_enrich_new_only), baseline_exists)

    build_log = build_build_log_dict(
        started=started,
        finished=finished,
        exchanges=exchanges,
        country=country,
        min_mcap_cli=float(args.min_market_cap),
        screener=screener_result,
        included_n=len(included_df),
        excluded_n=len(excluded_df),
        endpoints=endpoints,
        extra_warnings=warn_extra,
    )

    try:
        meta = save_outputs(
            included_df.copy(),
            excluded_df.copy(),
            summary_df.copy(),
            build_log,
            data_dir,
            data_source=DATA_SOURCE_FMP,
        )
    except Exception as e:
        log.error("save_outputs failed: %s: %s", type(e).__name__, e)
        return EXIT_FAILURE

    log.info("saved outputs:")
    for k, v in meta.get("paths", {}).items():
        log.info("  %s: %s", k, v)
    log.info("parquet_ok: %s", meta.get("parquet_ok"))
    log.info("universe build finished successfully")
    return EXIT_OK

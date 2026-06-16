# -*- coding: utf-8 -*-
"""
FMP ``/stable/company-screener`` 기반 거래소별 후보 수집.

* 기본적으로 ``country`` 필터를 넣지 않는다(미국 거래소 상장 ADR 등 포함).
* ``--country US`` 를 명시할 때만 API ``country`` 파라미터를 보낸다.
* 모든 HTTP는 ``fmp_client.fmp_get`` 으로만 수행한다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fmp_client import fmp_get

log = logging.getLogger(__name__)

PATH_COMPANY_SCREENER = "/stable/company-screener"

DEFAULT_EXCHANGES: Tuple[str, ...] = ("NASDAQ", "NYSE", "AMEX")

# 응답 행 수가 이 값 이상이면 상한에 걸렸을 가능성이 높다 → 버킷 재수집 검토.
NEAR_SCREENER_LIMIT: int = 9900

# (marketCapMoreThan, marketCapLowerThan) — 상한 없는 구간은 max=None
# 단위: USD. FMP는 marketCapMoreThan / marketCapLowerThan 조합을 사용한다.
MARKET_CAP_BUCKETS: Tuple[Tuple[int, Optional[int]], ...] = (
    (200_000_000_000, None),  # 200B+
    (50_000_000_000, 200_000_000_000),
    (10_000_000_000, 50_000_000_000),
    (2_000_000_000, 10_000_000_000),
    (300_000_000, 2_000_000_000),
    (50_000_000, 300_000_000),
    (0, 50_000_000),
)


def fetch_screener(
    exchange: str,
    min_market_cap: Optional[int],
    max_market_cap: Optional[int],
    country: Optional[str],
) -> List[Dict[str, Any]]:
    """
    단일 거래소·시총 구간에 대해 company-screener를 호출하고 원본 dict 행만 반환한다.

    * ``min_market_cap`` / ``max_market_cap`` 이 None 이면 해당 파라미터는 요청에 넣지 않는다.
    * ``country`` 가 None 이거나 공백이면 ``country`` 파라미터를 보내지 않는다.
    """
    ex = exchange.strip().upper()
    params: Dict[str, Any] = {
        "exchange": ex,
        "isActivelyTrading": "true",
        "isEtf": "false",
        "isFund": "false",
        "limit": 10000,
    }
    if country is not None and str(country).strip():
        params["country"] = str(country).strip()
    if min_market_cap is not None:
        params["marketCapMoreThan"] = int(min_market_cap)
    if max_market_cap is not None:
        params["marketCapLowerThan"] = int(max_market_cap)

    data = fmp_get(PATH_COMPANY_SCREENER, params)
    if not isinstance(data, list):
        log.warning(
            "company-screener expected list, got %s | exchange=%s",
            type(data).__name__,
            ex,
        )
        raise TypeError(
            f"company-screener response must be a list, got {type(data).__name__} for exchange={ex}"
        )
    out: List[Dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            out.append(item)
        else:
            log.warning("company-screener skip non-dict row | exchange=%s", ex)
    log.info("company-screener | exchange=%s raw_rows=%d", ex, len(out))
    return out


def normalize_screener_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """API camelCase 행을 분석용 snake_case 컬럼으로 정규화한다."""
    return {
        "symbol": _str_upper(raw.get("symbol")),
        "company_name": _pick_str(raw, ["companyName", "name"]),
        "exchange": _pick_str(raw, ["exchange"]),
        "exchange_short_name": _pick_str(raw, ["exchangeShortName"]),
        "country": _pick_str(raw, ["country"]),
        "currency": _pick_str(raw, ["currency"]),
        "sector": _pick_str(raw, ["sector"]),
        "industry": _pick_str(raw, ["industry"]),
        "market_cap": _pick_scalar(raw, ["marketCap", "mktCap"]),
        "price": _pick_scalar(raw, ["price"]),
        "beta": _pick_scalar(raw, ["beta"]),
        "volume": _pick_scalar(raw, ["volume", "vol"]),
        "is_etf": _truthy(raw.get("isEtf")),
        "is_fund": _truthy(raw.get("isFund")),
        "is_actively_trading": _truthy(raw.get("isActivelyTrading", raw.get("isTrading"))),
    }


def fetch_screener_universe(
    exchanges: Sequence[str],
    country: Optional[str],
    min_market_cap: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    거래소별로 스크리너를 호출한 뒤 정규화·``symbol`` 기준 시총 최대 1행으로 병합한다.

    ``min_market_cap`` 이 None 이면 ``marketCapMoreThan`` 을 API에 넣지 않는다.
    """
    raw_all: List[Dict[str, Any]] = []
    for ex in exchanges:
        raw_all.extend(fetch_screener(str(ex), min_market_cap, None, country))
    normalized = [normalize_screener_row(r) for r in raw_all]
    return dedupe_by_symbol_max_mcap(normalized)


def should_use_market_cap_buckets(rows: List[Dict[str, Any]]) -> bool:
    """수집 행 수가 상한(10000)에 가까우면 True — 버킷 재수집을 권장."""
    return len(rows) >= NEAR_SCREENER_LIMIT


def _effective_bucket_bounds(
    bucket_min: int,
    bucket_max: Optional[int],
    user_min_mcap: Optional[int],
) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """
    사용자 하한 ``user_min_mcap`` 과 버킷이 교집합이 있으면 (api_more_than, api_lower_than) 반환.
    교집합이 없으면 None.
    """
    eff_min = max(bucket_min, user_min_mcap) if user_min_mcap is not None else bucket_min
    eff_max = bucket_max
    if eff_max is not None and eff_min > eff_max:
        return None
    api_min: Optional[int] = None if eff_min <= 0 else int(eff_min)
    api_max: Optional[int] = None if eff_max is None else int(eff_max)
    return (api_min, api_max)


def fetch_screener_universe_bucketed(
    exchanges: Sequence[str],
    country: Optional[str],
    min_market_cap: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    거래소 × 시총 버킷별로 스크리너를 호출한 뒤 정규화·시총 최대 기준으로 ``symbol`` 병합.

    ``min_market_cap`` 이 지정되면 각 버킷 하한과 max()로 결합되며, 교집합 없는 버킷은 건너뛴다.
    """
    raw_all: List[Dict[str, Any]] = []
    for ex in exchanges:
        ex_u = str(ex).strip().upper()
        for bmin, bmax in MARKET_CAP_BUCKETS:
            bounds = _effective_bucket_bounds(bmin, bmax, min_market_cap)
            if bounds is None:
                continue
            api_min, api_max = bounds
            raw_all.extend(fetch_screener(ex_u, api_min, api_max, country))
    normalized = [normalize_screener_row(r) for r in raw_all]
    return dedupe_by_symbol_max_mcap(normalized)


def dedupe_by_symbol_max_mcap(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    ``symbol`` 기준 중복 제거. 동일 심볼이 여러 거래소/호출에 있으면 ``market_cap`` 이 가장 큰 행을 남긴다.
    시총이 같으면 먼저 본 행을 유지한다.
    """
    best: Dict[str, Dict[str, Any]] = {}
    best_m: Dict[str, float] = {}
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        m = _to_float_mcap(r.get("market_cap"))
        if sym not in best:
            best[sym] = r
            best_m[sym] = m
        elif m > best_m[sym]:
            best[sym] = r
            best_m[sym] = m
    return list(best.values())


def _to_float_mcap(v: Any) -> float:
    if v is None:
        return float("-inf")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("-inf")


def _truthy(v: Any) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y")


def _str_upper(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip().upper()


def _pick_str(raw: Dict[str, Any], keys: Sequence[str]) -> str:
    for k in keys:
        if k in raw and raw[k] is not None and str(raw[k]).strip():
            return str(raw[k]).strip()
    return ""


def _pick_scalar(raw: Dict[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if k in raw and raw[k] is not None and str(raw[k]).strip() != "":
            return raw[k]
    return None

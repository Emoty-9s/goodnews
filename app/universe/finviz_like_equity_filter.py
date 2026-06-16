# -*- coding: utf-8 -*-
"""
Finviz 스타일 보통주 필터 엔진.

입력: company-screener 정규화 DataFrame, ``etf_symbols``, ``stock_type_map``(symbol → type 토큰 집합).
출력: ``included_df``, ``excluded_df``, ``summary_df``.

* ``stock-list`` 타입은 **명확한 제외 토큰 일치**일 때만 제외(미존재·공란은 통과).
* 이름 휴리스틱에서 **Trust 단독**은 사용하지 않으며, REIT·ADR·클래스주(-A/-B 등)는 suffix 규칙으로 건드리지 않는다.

``reason_code`` 상수는 ``universe_reason_codes`` 에서 가져온다.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple, Union

import pandas as pd

from fmp_etf_stock_reference import is_excluded_stock_type
from universe_reason_codes import (
    REASON_ETF_LIST_MATCH,
    REASON_EXCLUDED_EXCHANGE,
    REASON_EXCLUDED_INDUSTRY,
    REASON_EXCLUDED_NAME_PATTERN,
    REASON_EXCLUDED_SYMBOL_SUFFIX,
    REASON_INACTIVE,
    REASON_INVALID_SYMBOL,
    REASON_PASS,
    REASON_SCREENER_ETF,
    REASON_SCREENER_FUND,
    REASON_STOCK_LIST_EXCLUDED_TYPE,
)

RowLike = Union[pd.Series, Mapping[str, Any]]

# ---------------------------------------------------------------------------
# Exchange canonical (NASDAQ / NYSE / AMEX)
# ---------------------------------------------------------------------------
_EXCHANGE_SYNONYM_TO_CANONICAL: Dict[str, str] = {
    "NYSE": "NYSE",
    "NASDAQ": "NASDAQ",
    "AMEX": "AMEX",
    "NEW YORK STOCK EXCHANGE": "NYSE",
    "NEW YORK STOCK EXCHANGE INC": "NYSE",
    "NEW YORK STOCK EXCHANGE LLC": "NYSE",
    "NYSE ARCA": "NYSE",
    "NYSEARCA": "NYSE",
    "ARCA": "NYSE",
    "NYSE MKT": "AMEX",
    "NYSE AMERICAN": "AMEX",
    "NYSE AMERICAN EQUITIES": "AMEX",
    "NYSE AMERICAN EQUITIES LLC": "AMEX",
    "NASDAQ STOCK MARKET": "NASDAQ",
    "NASDAQ CAPITAL MARKET": "NASDAQ",
    "NASDAQ GLOBAL MARKET": "NASDAQ",
    "NASDAQ GLOBAL SELECT": "NASDAQ",
    "NASDAQ GLOBAL SELECT MARKET": "NASDAQ",
    "NASDAQ GM": "NASDAQ",
    "NASDAQ CM": "NASDAQ",
    "NASDAQ GS": "NASDAQ",
}

_ALLOWED_EXCHANGES: Set[str] = {"NASDAQ", "NYSE", "AMEX"}

# ---------------------------------------------------------------------------
# Industry blocklist (case-insensitive exact match after normalize)
# ---------------------------------------------------------------------------
_EXCLUDED_INDUSTRIES_NORM: Set[str] = {
    "exchange traded fund",
    "closed-end fund - debt",
    "closed-end fund - equity",
    "closed-end fund - foreign",
    "shell companies",
}

# ---------------------------------------------------------------------------
# Name patterns (순서대로 첫 매칭). Trust 단독 제외 없음.
# ---------------------------------------------------------------------------
_NAME_REGEX_CHECKS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bETF\b", re.IGNORECASE), "ETF"),
    (re.compile(r"\bETN\b", re.IGNORECASE), "ETN"),
    (re.compile(r"\bETP\b", re.IGNORECASE), "ETP"),
    (re.compile(r"\bWarrants?\b", re.IGNORECASE), "Warrant(s)"),
    (re.compile(r"\bUnits?\b", re.IGNORECASE), "Unit(s)"),
    (re.compile(r"\bRights?\b", re.IGNORECASE), "Right(s)"),
    (re.compile(r"\bPreferred\b", re.IGNORECASE), "Preferred"),
    (re.compile(r"\bPreference\b", re.IGNORECASE), "Preference"),
    (re.compile(r"Depositary Shares", re.IGNORECASE), "Depositary Shares"),
    (re.compile(r"\bNotes?\b", re.IGNORECASE), "Note(s)"),
    (re.compile(r"\bBonds?\b", re.IGNORECASE), "Bond(s)"),
    (re.compile(r"\bDebentures?\b", re.IGNORECASE), "Debenture(s)"),
    (re.compile(r"Closed[- ]End Fund", re.IGNORECASE), "Closed-End Fund"),
    (re.compile(r"Acquisition Corp", re.IGNORECASE), "Acquisition Corp"),
    (re.compile(r"\bSPAC\b", re.IGNORECASE), "SPAC"),
]

# ---------------------------------------------------------------------------
# Symbol suffix (긴 것 먼저)
# ---------------------------------------------------------------------------
_BAD_SYMBOL_SUFFIXES: Tuple[str, ...] = (
    ".WS",
    "-WS",
    ".WT",
    "-WT",
    ".W",
    "-W",
    ".U",
    "-U",
    ".R",
    "-R",
)

_SYMBOL_VALID = re.compile(r"^[A-Z0-9.\-]+$")


def normalize_exchange(value: str) -> str:
    """거래소 문자열을 정규화한 뒤 NYSE / NASDAQ / AMEX 또는 알 수 없으면 대문자 토큰."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip().upper()
    if not s:
        return ""
    s = re.sub(r"[./\\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _EXCHANGE_SYNONYM_TO_CANONICAL.get(s, s)


def normalize_symbol(value: Any) -> str:
    """심볼: strip + upper."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip().upper()


def is_allowed_exchange(row: RowLike) -> bool:
    """행의 거래소가 NASDAQ / NYSE / AMEX 인지 여부."""
    ex_short = _row_get(row, "exchange_short_name", "")
    ex_main = _row_get(row, "exchange", "")
    raw = ex_short if str(ex_short).strip() else ex_main
    canon = normalize_exchange(str(raw))
    return canon in _ALLOWED_EXCHANGES


def has_bad_name_pattern(name: str) -> Tuple[bool, Optional[str]]:
    """
    회사명에 비보통주성 키워드가 **패턴(단어 경계 등)** 으로 들어가면 (True, 라벨).

    * ``Trust`` 단독 키워드는 사용하지 않는다(Realty Trust 등 오탐 완화).
    """
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return False, None
    text = str(name)
    if not text.strip():
        return False, None
    for rx, label in _NAME_REGEX_CHECKS:
        if rx.search(text):
            return True, label
    return False, None


def has_bad_symbol_suffix(symbol: str) -> Tuple[bool, Optional[str]]:
    """
    워런트·유닛·라이트 등 명확한 접미사. 클래스주(-A/-B, .A/.B)·ADR/REIT 전용 규칙은 여기 없음.
    """
    sym = normalize_symbol(symbol)
    if not sym:
        return False, None
    for suf in sorted(_BAD_SYMBOL_SUFFIXES, key=len, reverse=True):
        if sym.endswith(suf):
            return True, suf
    return False, None


def classify_row(
    row: RowLike,
    etf_symbols: Set[str],
    stock_type_map: Dict[str, Set[str]],
) -> Tuple[bool, str, str]:
    """
    한 행에 대해 포함 여부·``reason_code``·상세 문자열을 반환한다.

    Returns
    -------
    (포함 여부, 사유 코드, 제외 상세 설명)

    통과 시 ``(True, PASS, "")`` .
    """
    sym = normalize_symbol(_row_get(row, "symbol", ""))
    if not sym or not bool(_SYMBOL_VALID.match(sym)):
        return False, REASON_INVALID_SYMBOL, "empty_or_invalid_chars"

    if not is_allowed_exchange(row):
        raw = _row_get(row, "exchange_short_name", "") or _row_get(row, "exchange", "")
        return False, REASON_EXCLUDED_EXCHANGE, f"exchange={raw!r}"

    active = _row_get(row, "is_actively_trading", None)
    if not _is_true(active):
        return False, REASON_INACTIVE, "is_actively_trading_not_true"

    if _is_true(_row_get(row, "is_etf", False)):
        return False, REASON_SCREENER_ETF, "screener_is_etf"

    if _is_true(_row_get(row, "is_fund", False)):
        return False, REASON_SCREENER_FUND, "screener_is_fund"

    if sym in etf_symbols:
        return False, REASON_ETF_LIST_MATCH, "symbol_in_etf_list"

    types = stock_type_map.get(sym, set())
    excl_type, matched_tok = is_excluded_stock_type(types)
    if excl_type and matched_tok:
        return False, REASON_STOCK_LIST_EXCLUDED_TYPE, f"type_token={matched_tok}"

    ind = _row_get(row, "industry", "")
    ind_n = _norm_industry(ind)
    if ind_n and ind_n in _EXCLUDED_INDUSTRIES_NORM:
        return False, REASON_EXCLUDED_INDUSTRY, f"industry={ind!r}"

    cname = _row_get(row, "company_name", "")
    bad_nm, nm_label = has_bad_name_pattern(str(cname) if cname is not None else "")
    if bad_nm:
        return False, REASON_EXCLUDED_NAME_PATTERN, f"name_pattern={nm_label}"

    bad_suf, suf = has_bad_symbol_suffix(sym)
    if bad_suf:
        return False, REASON_EXCLUDED_SYMBOL_SUFFIX, f"suffix={suf}"

    return True, REASON_PASS, ""


def apply_filters(
    df: pd.DataFrame,
    etf_symbols: Set[str],
    stock_type_map: Dict[str, Set[str]],
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    필터 순서는 ``classify_row`` 와 동일.

    * ``included_df``: ``universe_status == "included"``
    * ``excluded_df``: ``universe_status == "excluded"``, ``exclusion_reason``, ``exclusion_detail``
    * ``summary_df``: ``reason_code``, ``count``, ``share_pct`` (입력 행 수 대비)
    """
    if df.empty:
        empty_summary = pd.DataFrame(columns=["reason_code", "count", "share_pct"])
        return df.iloc[0:0].copy(), df.iloc[0:0].copy(), empty_summary

    df_in = df.copy()
    etf_up = {str(s).strip().upper() for s in etf_symbols}

    included_rows: List[Dict[str, Any]] = []
    excluded_rows: List[Dict[str, Any]] = []
    reason_counts: Dict[str, int] = {}

    for _, row in df_in.iterrows():
        ok, reason, detail = classify_row(row, etf_up, stock_type_map)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        rd = row.to_dict()
        if ok:
            rd["universe_status"] = "included"
            included_rows.append(rd)
        else:
            rd["universe_status"] = "excluded"
            rd["exclusion_reason"] = reason
            rd["exclusion_detail"] = detail
            excluded_rows.append(rd)

    included_df = pd.DataFrame(included_rows)
    excluded_df = pd.DataFrame(excluded_rows)

    n = len(df_in)
    summary_rows: List[Dict[str, Any]] = []
    for code in sorted(reason_counts.keys()):
        c = reason_counts[code]
        summary_rows.append(
            {
                "reason_code": code,
                "count": c,
                "share_pct": round(100.0 * float(c) / float(n), 6) if n else 0.0,
            }
        )
    summary_df = pd.DataFrame(summary_rows)
    return included_df, excluded_df, summary_df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_get(row: RowLike, key: str, default: Any = None) -> Any:
    if isinstance(row, pd.Series):
        if key not in row.index:
            return default
        v = row[key]
    else:
        v = row.get(key, default)  # type: ignore[union-attr]
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except (ValueError, TypeError):
        pass
    return v


def _is_true(v: Any) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    if isinstance(v, float) and pd.isna(v):
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y")


def _norm_industry(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return re.sub(r"\s+", " ", str(v).strip().lower())

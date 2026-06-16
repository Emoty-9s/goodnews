# -*- coding: utf-8 -*-
"""
ETF blacklist(etf-list) 및 stock-list 타입 보조 참조.

* ``stock-list`` 는 company-screener 등 **주 유니버스**에 조인해 메타만 보강한다.
* 심볼이 stock-list에 없거나 ``type`` 이 비어 있어도 **제외 사유가 되지 않는다**.
* ``type_norm`` 이 **제외 토큰과 정확히 일치**할 때만 구조적 제외로 본다(REIT·ADR 미포함).

모든 HTTP는 ``fmp_client.fmp_get`` 만 사용한다.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from fmp_client import fmp_get

log = logging.getLogger(__name__)

PATH_ETF_LIST = "/stable/etf-list"
PATH_STOCK_LIST = "/stable/stock-list"

# 정확 일치(exact token)로만 매칭. REIT·ADR 는 넣지 않는다.
EXCLUDED_STOCK_TYPE_TOKENS: frozenset[str] = frozenset(
    {
        "etf",
        "etn",
        "etp",
        "fund",
        "mutual fund",
        "closed-end fund",
        "preferred",
        "preferred stock",
        "warrant",
        "unit",
        "right",
        "bond",
        "note",
        "trust",
        "spac",
    }
)


def fetch_etf_symbols() -> Set[str]:
    """``/stable/etf-list`` 를 받아 ETF 심볼 blacklist 집합을 만든다."""
    data = fmp_get(PATH_ETF_LIST, {})
    if not isinstance(data, list):
        log.warning("etf-list expected list, got %s", type(data).__name__)
        raise TypeError(f"etf-list must return list, got {type(data).__name__}")
    out: Set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        sym = _normalize_symbol(item.get("symbol") or item.get("ticker"))
        if sym:
            out.add(sym)
    log.info("etf-list symbols=%d", len(out))
    return out


def fetch_stock_list_rows() -> List[Dict[str, Any]]:
    """``/stable/stock-list`` 원본 dict 행 리스트."""
    data = fmp_get(PATH_STOCK_LIST, {})
    if not isinstance(data, list):
        log.warning("stock-list expected list, got %s", type(data).__name__)
        raise TypeError(f"stock-list must return list, got {type(data).__name__}")
    rows = [x for x in data if isinstance(x, dict)]
    log.info("stock-list rows=%d", len(rows))
    return rows


def normalize_type_norm(value: Optional[str]) -> str:
    """type_norm: lowercase, strip, 연속 공백 → 단일 공백. 부분 문자열 규칙 없음."""
    if value is None:
        return ""
    s = str(value).strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_stock_list(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    stock-list 행을 표준 컬럼으로 정규화한다.

    컬럼: symbol, name, exchange, exchange_short_name, type_raw, type_norm
    """
    records: List[Dict[str, Any]] = []
    for raw in rows:
        sym = _normalize_symbol(raw.get("symbol") or raw.get("ticker"))
        if not sym:
            continue
        t_raw = raw.get("type")
        t_raw_str = "" if t_raw is None else str(t_raw)
        records.append(
            {
                "symbol": sym,
                "name": _pick_str(raw, ["name", "companyName"]),
                "exchange": _pick_str(raw, ["exchange"]),
                "exchange_short_name": _pick_str(raw, ["exchangeShortName"]),
                "type_raw": t_raw_str.strip(),
                "type_norm": normalize_type_norm(t_raw_str),
            }
        )
    df = pd.DataFrame.from_records(records)
    if df.empty:
        return pd.DataFrame(
            columns=["symbol", "name", "exchange", "exchange_short_name", "type_raw", "type_norm"]
        )
    return df


def split_aggregate_type_norm_cell(type_norm_cell: str) -> Set[str]:
    """
    한 셀에 여러 타입이 붙은 경우(|, ;, 쉼표) 토큰으로 분리해 각각 normalize 한다.
    단독 행은 하나의 토큰으로 취급한다.
    """
    if not type_norm_cell or not str(type_norm_cell).strip():
        return set()
    parts = re.split(r"[|;,]+", str(type_norm_cell))
    return {normalize_type_norm(p) for p in parts if normalize_type_norm(p)}


def build_stock_type_map(stock_list_df: pd.DataFrame) -> Dict[str, Set[str]]:
    """
    심볼별로 해당하는 모든 ``type_norm`` 토큰 집합을 만든다(동일 심볼 다행 병합).

    빈 ``type_norm`` 행은 집합에 아무 토큰도 추가하지 않는다(미정 ≠ 제외).
    """
    result: Dict[str, Set[str]] = {}
    if stock_list_df.empty or "symbol" not in stock_list_df.columns:
        return result
    for _, row in stock_list_df.iterrows():
        sym = _normalize_symbol(row.get("symbol"))
        if not sym:
            continue
        tn = row.get("type_norm")
        cell = "" if tn is None or (isinstance(tn, float) and pd.isna(tn)) else str(tn)
        tokens = split_aggregate_type_norm_cell(cell)
        if sym not in result:
            result[sym] = set()
        result[sym].update(tokens)
    return result


def is_excluded_stock_type(type_set: Set[str]) -> Tuple[bool, Optional[str]]:
    """
    ``type_set`` 안의 토큰 중 ``EXCLUDED_STOCK_TYPE_TOKENS`` 와 **완전 일치**하는 것이 있으면 제외.

    Returns
    -------
    (True, matched_token) | (False, None)

    여러 개 일치 시 결정적(deterministic)으로 가장 앞선 토큰(알파벳 순)을 반환한다.
    """
    if not type_set:
        return False, None
    hits = sorted(type_set & EXCLUDED_STOCK_TYPE_TOKENS)
    if hits:
        return True, hits[0]
    return False, None


def _normalize_symbol(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip().upper()


def _pick_str(raw: Dict[str, Any], keys: List[str]) -> str:
    for k in keys:
        if k in raw and raw[k] is not None and str(raw[k]).strip():
            return str(raw[k]).strip()
    return ""

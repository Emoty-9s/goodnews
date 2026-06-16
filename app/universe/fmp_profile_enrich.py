# -*- coding: utf-8 -*-
"""
신규 심볼에 한해, 핵심 필드 결측 시에만 ``/stable/profile`` 단건 호출로 보강.

* 전 유니버스에 대한 무차별 profile 호출 금지.
* ``profile-bulk`` / batch 미사용.
* HTTP는 ``fmp_client.fmp_get`` 만 사용.

권장 호출 패턴(``--profile-enrich-new-only`` 가 켜진 경우에만):

1. ``previous = load_previous_universe(data_dir)``
2. ``universe_current.csv`` 가 **없으면** 첫 실행으로 보고 ``new_symbols = set()`` 으로 두어 보강을 건너뛴다.
3. 그렇지 않으면 ``new_symbols = find_new_symbols(df['symbol'], previous)``
4. ``enrich_missing_profile_fields(df, new_symbols)``
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

import pandas as pd

from fmp_client import fmp_get

log = logging.getLogger(__name__)

PATH_PROFILE = "/stable/profile"

_CORE_MISSING_COLS: tuple[str, ...] = ("sector", "industry", "market_cap", "beta")

_PROFILE_FIELD_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("company_name", ("companyName", "name")),
    ("sector", ("sector",)),
    ("industry", ("industry",)),
    ("market_cap", ("marketCap", "mktCap")),
    ("price", ("price",)),
    ("beta", ("beta",)),
    ("exchange", ("exchange",)),
    ("exchange_short_name", ("exchangeShortName",)),
    ("country", ("country",)),
    ("currency", ("currency",)),
    ("is_actively_trading", ("isActivelyTrading", "isTrading")),
)


def load_previous_universe(data_dir: Path | str) -> Set[str]:
    """
    ``{data_dir}/universe_current.csv`` 의 ``symbol`` 집합(대문자).

    파일이 없으면 빈 집합. 읽기 실패 시 경고 후 빈 집합.
    """
    p = Path(data_dir) / "universe_current.csv"
    if not p.exists():
        log.info("previous universe not found path=%s (profile new-only baseline empty)", p)
        return set()
    try:
        df = pd.read_csv(p, low_memory=False)
    except Exception as e:
        log.warning("load_previous_universe failed path=%s err=%s", p, type(e).__name__)
        return set()
    if df.empty:
        return set()
    cols = {str(c).strip().lower(): c for c in df.columns}
    if "symbol" not in cols:
        log.warning("universe_current.csv has no symbol column path=%s", p)
        return set()
    sym_col = cols["symbol"]
    syms = (
        df[sym_col]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
    )
    return set(syms[syms != ""])


def find_new_symbols(current_symbols: Iterable[str], previous_symbols: Set[str]) -> Set[str]:
    """``current`` 중 ``previous`` 에 없는 심볼(대문자). 순수 집합 차집합."""
    cur: Set[str] = set()
    for s in current_symbols:
        if s is None or (isinstance(s, float) and pd.isna(s)):
            continue
        u = str(s).strip().upper()
        if u:
            cur.add(u)
    return cur - set(previous_symbols)


def fetch_profile(symbol: str) -> Optional[Dict[str, Any]]:
    """``GET /stable/profile?symbol=`` — 단건. 실패 시 None."""
    sym = str(symbol).strip().upper()
    if not sym:
        return None
    try:
        data = fmp_get(PATH_PROFILE, {"symbol": sym})
    except Exception as e:
        log.warning(
            "profile fetch failed symbol=%s err=%s",
            sym,
            type(e).__name__,
        )
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict) and not data.get("Error Message") and data.get("symbol"):
        return data
    log.warning("profile unexpected payload symbol=%s type=%s", sym, type(data).__name__)
    return None


def enrich_missing_profile_fields(df: pd.DataFrame, new_symbols: Set[str]) -> pd.DataFrame:
    """
    ``new_symbols`` 에 포함된 행만 검사한다.

    그중 ``sector``, ``industry``, ``market_cap``, ``beta`` 중 하나라도 결측이면
    해당 심볼에 대해 **한 번만** ``fetch_profile`` 호출 후, 비어 있는 컬럼만 채운다.

    ``new_symbols`` 가 비어 있으면 API를 호출하지 않는다.
    """
    if df.empty or not new_symbols:
        return df.copy()
    if "symbol" not in df.columns:
        log.warning("enrich_missing_profile_fields: no symbol column, skip")
        return df.copy()

    out = df.copy()
    prof_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    n_calls = 0
    for idx in out.index:
        sym = str(out.at[idx, "symbol"]).strip().upper() if pd.notna(out.at[idx, "symbol"]) else ""
        if not sym or sym not in new_symbols:
            continue
        if not _needs_core_field_enrichment(out.loc[idx]):
            continue
        if sym not in prof_cache:
            prof_cache[sym] = fetch_profile(sym)
            n_calls += 1
        prof = prof_cache[sym]
        if not prof:
            continue
        _merge_profile_into_row(out, idx, prof)

    if n_calls:
        log.info("profile enrich: profile_api_calls=%d (new_symbol_candidates=%d)", n_calls, len(new_symbols))
    return out


def _needs_core_field_enrichment(row: pd.Series) -> bool:
    for col in _CORE_MISSING_COLS:
        if col not in row.index:
            return True
        if _is_missing(row[col]):
            return True
    return False


def _is_missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    try:
        if pd.isna(v):
            return True
    except (ValueError, TypeError):
        pass
    return False


def _pick_prof(d: Dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k not in d:
            continue
        v = d[k]
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _truthy(v: Any) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y")


def _merge_profile_into_row(df: pd.DataFrame, idx: Any, prof: Dict[str, Any]) -> None:
    for col, keys in _PROFILE_FIELD_MAP:
        if col not in df.columns:
            df[col] = pd.NA
        cur = df.at[idx, col]
        if not _is_missing(cur):
            continue
        val = _pick_prof(prof, keys)
        if val is None:
            continue
        if col == "is_actively_trading":
            df.at[idx, col] = _truthy(val)
        else:
            df.at[idx, col] = val

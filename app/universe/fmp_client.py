# -*- coding: utf-8 -*-
"""
FMP HTTP 클라이언트 — 기존 universe 모듈 전체가 의존하는 단일 진입점.

인터페이스:
  fmp_get(path, params)  → list | dict
  get_api_key()          → str  (없으면 RuntimeError)
  ensure_fmp_session()   → None (requests.Session 재사용 준비)
  API_KEY_ENV            → str  (환경변수 이름 상수)

* /stable/ 엔드포인트는 FMP Premium 전용 base URL을 사용한다.
* /api/v3/ 엔드포인트는 기존 FMP 표준 base URL을 사용한다.
* API 키는 로그에 절대 노출하지 않는다.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# ── 환경변수 이름 (기존 코드가 상수로 참조) ───────────────────────────────
API_KEY_ENV: str = "FMP_API_KEY"

# ── Base URL ────────────────────────────────────────────────────────────────
_BASE_URL_STABLE: str = "https://financialmodelingprep.com"   # /stable/ 엔드포인트
_BASE_URL_V3: str = "https://financialmodelingprep.com"        # /api/v3/ 엔드포인트

# ── 재시도 설정 ──────────────────────────────────────────────────────────────
_RETRY_TOTAL: int = 3
_RETRY_BACKOFF: float = 1.5          # 1.5s, 3s, 6s
_RETRY_STATUS: tuple = (429, 500, 502, 503, 504)
_REQUEST_TIMEOUT: int = 60           # seconds

# ── 모듈 내부 Session 싱글톤 ────────────────────────────────────────────────
_session: Optional[requests.Session] = None


def get_api_key() -> str:
    """
    FMP_API_KEY 환경변수를 읽어 반환한다.
    없거나 비어 있으면 RuntimeError.
    """
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(
            f"환경변수 {API_KEY_ENV!r} 가 설정되지 않았습니다. "
            ".env 파일에 FMP_API_KEY=your_key 를 추가하세요."
        )
    return key


def ensure_fmp_session() -> None:
    """
    재시도 로직이 설정된 requests.Session 을 준비한다.
    이미 생성돼 있으면 아무것도 하지 않는다.
    universe_pipeline.run_universe_pipeline() 시작 시 한 번 호출.
    """
    global _session
    if _session is not None:
        return

    retry = Retry(
        total=_RETRY_TOTAL,
        backoff_factor=_RETRY_BACKOFF,
        status_forcelist=list(_RETRY_STATUS),
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    _session = s
    log.debug("FMP requests.Session initialized (retries=%d)", _RETRY_TOTAL)


def _get_session() -> requests.Session:
    """내부용: 세션이 없으면 자동 초기화 후 반환."""
    if _session is None:
        ensure_fmp_session()
    return _session  # type: ignore[return-value]


def _build_url(path: str) -> str:
    """
    path 앞부분으로 base URL 을 결정한다.
    /stable/  → https://financialmodelingprep.com/stable/...
    /api/v3/  → https://financialmodelingprep.com/api/v3/...
    그 외      → /stable/ 기준으로 처리
    """
    p = path.lstrip("/")
    return f"https://financialmodelingprep.com/{p}"


def fmp_get(path: str, params: Dict[str, Any]) -> Any:
    """
    FMP API GET 요청. apikey 는 자동으로 params 에 추가된다.

    Parameters
    ----------
    path   : 엔드포인트 경로 (예: "/stable/company-screener")
    params : 쿼리 파라미터 dict (apikey 제외)

    Returns
    -------
    파싱된 JSON (list 또는 dict). HTTP 오류 또는 파싱 실패 시 예외를 전파.
    """
    url = _build_url(path)
    key = get_api_key()

    full_params = dict(params)
    full_params["apikey"] = key   # 키는 params 에만 포함, URL 문자열·로그에 노출 안 함

    session = _get_session()

    log.debug(
        "fmp_get path=%s params=%s",
        path,
        {k: v for k, v in full_params.items() if k != "apikey"},  # 키 마스킹
    )

    start = time.monotonic()
    try:
        resp = session.get(url, params=full_params, timeout=_REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        log.error("fmp_get network error path=%s err=%s", path, type(e).__name__)
        raise

    elapsed = time.monotonic() - start

    if resp.status_code == 429:
        # Rate Limit: 재시도 어댑터가 처리하지만 명시적으로 로그
        log.warning("fmp_get rate_limited path=%s status=429 elapsed=%.2fs", path, elapsed)

    if not resp.ok:
        log.error(
            "fmp_get http_error path=%s status=%d elapsed=%.2fs",
            path,
            resp.status_code,
            elapsed,
        )
        resp.raise_for_status()

    log.debug("fmp_get ok path=%s status=%d elapsed=%.2fs", path, resp.status_code, elapsed)

    try:
        return resp.json()
    except Exception as e:
        log.error("fmp_get json_parse_error path=%s err=%s body_head=%s", path, e, resp.text[:200])
        raise

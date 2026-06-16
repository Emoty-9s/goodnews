# -*- coding: utf-8 -*-
"""
유니버스 빌드 러너 — ``build_universe`` CLI 를 Python API 로 감싸는 래퍼.

Celery 태스크 또는 FastAPI 엔드포인트에서 직접 import 해서 호출할 수 있다.

    from app.universe.universe_runner import run_universe_build, UniverseBuildConfig

    config = UniverseBuildConfig(min_market_cap=100_000_000)
    result = run_universe_build(config)
    print(result.included_count, result.tickers[:5])
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.universe.ticker_store import get_universe_tickers, get_universe_stats

log = logging.getLogger(__name__)

settings = get_settings()


@dataclass
class UniverseBuildConfig:
    """유니버스 빌드 파라미터."""

    data_dir: Path = field(default_factory=lambda: Path(settings.universe_data_dir))
    min_market_cap: float = 100_000_000.0   # USD, 기본 1억
    exchanges: str = "NASDAQ,NYSE,AMEX"
    country: Optional[str] = None           # None → FMP에 country 파라미터 미전송
    save_raw: bool = False
    profile_enrich_new_only: bool = False
    log_level: str = "INFO"


@dataclass
class UniverseBuildResult:
    """빌드 실행 결과 요약."""

    exit_code: int
    included_count: int
    excluded_count: int
    tickers: list[str]
    snapshot_date: Optional[str]
    warnings: list[str]
    data_dir: str

    @property
    def success(self) -> bool:
        return self.exit_code == 0


def _make_argparse_namespace(config: UniverseBuildConfig) -> argparse.Namespace:
    """UniverseBuildConfig → build_universe 가 기대하는 argparse.Namespace 변환."""
    ns = argparse.Namespace()
    ns.data_dir = config.data_dir
    ns.min_market_cap = float(config.min_market_cap)
    ns.exchanges = config.exchanges
    ns.country = config.country
    ns.save_raw = config.save_raw
    ns.profile_enrich_new_only = config.profile_enrich_new_only
    ns.log_level = config.log_level
    ns.debug = (config.log_level == "DEBUG")
    return ns


def run_universe_build(config: Optional[UniverseBuildConfig] = None) -> UniverseBuildResult:
    """
    유니버스 빌드 파이프라인을 실행하고 결과를 반환한다.

    Parameters
    ----------
    config : UniverseBuildConfig. None 이면 기본값 사용.

    Returns
    -------
    UniverseBuildResult — exit_code 0 이면 성공.
    """
    if config is None:
        config = UniverseBuildConfig()

    config.data_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "universe_runner: start build | exchanges=%s min_mcap=%.0f data_dir=%s",
        config.exchanges,
        config.min_market_cap,
        config.data_dir.resolve(),
    )

    # universe_pipeline 을 universe 패키지 내부에서 import (sys.path 조작 없이)
    # universe 디렉터리를 일시적으로 sys.path 에 추가해 기존 코드의 flat import 를 지원
    universe_pkg_dir = str(Path(__file__).parent.resolve())
    _path_added = False
    if universe_pkg_dir not in sys.path:
        sys.path.insert(0, universe_pkg_dir)
        _path_added = True

    try:
        from universe_pipeline import run_universe_pipeline, EXIT_OK  # type: ignore

        ns = _make_argparse_namespace(config)
        exit_code = run_universe_pipeline(ns)
    except Exception as e:
        log.error("universe_runner: pipeline crashed: %s: %s", type(e).__name__, e)
        exit_code = 4
    finally:
        if _path_added and universe_pkg_dir in sys.path:
            sys.path.remove(universe_pkg_dir)

    # 결과 취합
    tickers = get_universe_tickers(config.data_dir / "universe_current.csv")
    stats = get_universe_stats(config.data_dir / "universe_current.csv")

    result = UniverseBuildResult(
        exit_code=exit_code,
        included_count=stats.get("total", len(tickers)),
        excluded_count=0,   # build_log.json 에서 읽을 수도 있지만 여기선 생략
        tickers=tickers,
        snapshot_date=stats.get("snapshot_date"),
        warnings=[],
        data_dir=str(config.data_dir.resolve()),
    )

    if result.success:
        log.info(
            "universe_runner: build success | included=%d snapshot=%s",
            result.included_count,
            result.snapshot_date,
        )
    else:
        log.error("universe_runner: build failed | exit_code=%d", exit_code)

    return result

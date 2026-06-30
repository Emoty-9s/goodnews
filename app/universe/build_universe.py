# -*- coding: utf-8 -*-
r"""
VQGRS — Finviz 유사 미국 상장 보통주 유니버스 빌더
====================================================

목적
----
NASDAQ / NYSE / AMEX 상장 후보를 FMP company-screener로 모으고, ETF·펀드·워런트 등
비보통주성 종목을 걸러 **분석용 유니버스**를 ``data/`` 에 저장한다.

Premium 플랜 제약
-----------------
* **bulk / batch / profile-bulk** 및 이에 준하는 대량 엔드포인트는 사용하지 않는다.
* ``/stable/profile`` 은 **신규·핵심 필드 결측** 등 제한적 보강에만 사용한다(전 종목 무차별 호출 금지).

사용 엔드포인트
---------------
* ``/stable/company-screener`` — 후보 풀
* ``/stable/etf-list`` — ETF 심볼 블랙리스트
* ``/stable/stock-list`` — 심볼별 ``type`` 보조 참조
* ``/stable/profile`` — (선택) 신규 종목 필드 보강

제외 대상(요약)
---------------
* ETF / 펀드 / ETN / 우선주 / 워런트 / 유닛 / 라이트 / 채권·노트류 / SPAC·특정 산업·이름·심볼 접미사 등
  (상세 규칙은 ``finviz_like_equity_filter`` 참고)

유지 대상
---------
* **ADR**, **REIT**, **클래스 주**(예: BRK-B, GOOG/GOOGL 등 접미사 규칙에 해당하지 않는 경우)

출력 파일
---------
* ``universe_current.csv`` / ``universe_current.parquet`` — 포함 유니버스
* ``universe_excluded.csv`` / ``universe_excluded.parquet`` — 제외 목록 및 사유
* ``universe_summary.csv`` — 제외 사유별 집계
* ``universe_build_log.json`` — 실행 메타
* ``--save-raw`` 시 ``raw_fmp/`` 아래 API 응답 JSON(중간 실패 시 디버깅용)

환경 변수
---------
* ``FMP_API_KEY`` (필수) — 로그·출력에 **절대 노출하지 않는다**.

실행 예시
---------
.. code-block:: text

   python build_universe.py --data-dir ./data --debug --save-raw

(``--min-market-cap`` 생략 시 기본 **1억 USD** 하한이 스크리너에 적용된다. 전체 시총 구간을 보려면 ``--min-market-cap 0``)

상세 파이프라인은 ``universe_pipeline`` 모듈을 참고한다.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from universe_pipeline import EXIT_FAILURE, run_universe_pipeline

log = logging.getLogger(__name__)

# 스크리너 ``marketCapMoreThan`` 기본 하한 (USD). 0을 넘기면 API에서 하한 미전송.
DEFAULT_MIN_MARKET_CAP_USD: float = 100_000_000.0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """CLI 인자 파싱. 기본값은 미국 주요 거래소·시총 하한 100M USD·country 미지정."""
    p = argparse.ArgumentParser(
        description="FMP 기반 Finviz-like 미국 상장 보통주 유니버스 빌드.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--data-dir", type=Path, default=Path("./data"), help="출력·raw 하위 디렉터리")
    p.add_argument(
        "--min-market-cap",
        type=float,
        default=DEFAULT_MIN_MARKET_CAP_USD,
        help="FMP 시총 하한(USD). 기본 1억(100M). 0이면 하한 파라미터 생략",
    )
    p.add_argument("--exchanges", type=str, default="NASDAQ,NYSE,AMEX", help="쉼표 구분")
    p.add_argument(
        "--country",
        type=str,
        default=None,
        help="FMP screener country; 미지정 시 파라미터 생략(미국 거래소 상장 해외 발행인 포함)",
    )
    p.add_argument("--debug", action="store_true", help="루트 로거 DEBUG")
    p.add_argument(
        "--save-raw",
        action="store_true",
        help="raw_fmp/ 에 screener·리스트 JSON 저장(실패 시에도 중간 산출물 확인)",
    )
    p.add_argument(
        "--profile-enrich-new-only",
        action="store_true",
        help="이전 universe_current.csv 대비 신규 포함 종목만 profile 보강",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    p.add_argument(
        "--upload-to-supabase",
        action="store_true",
        help=(
            "빌드 완료 후 universe_tickers Supabase 테이블에 upsert. "
            "python -m app.universe.build_universe 또는 프로젝트 루트에서 실행 필요"
        ),
    )
    return p.parse_args(argv)


def configure_logging(level_name: str, debug_flag: bool) -> None:
    """콘솔 로깅 설정(API 키는 로그에 남기지 않음)."""
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    cfg: Dict[str, Any] = {
        "level": level,
        "format": "%(asctime)s [%(levelname)s] %(message)s",
    }
    if sys.version_info >= (3, 8):
        cfg["force"] = True
    logging.basicConfig(**cfg)
    if debug_flag:
        logging.getLogger().setLevel(logging.DEBUG)


def main() -> int:
    """진입점: 파이프라인 실행 및 종료 코드 반환."""
    args = parse_args()
    configure_logging(args.log_level, bool(args.debug))
    data_dir = Path(args.data_dir)
    try:
        exit_code = run_universe_pipeline(args)

        if exit_code == 0 and getattr(args, "upload_to_supabase", False):
            import asyncio
            import sys
            import pandas as pd

            # 프로젝트 루트를 sys.path에 추가 (직접 실행 시 app 패키지 접근 보장)
            _proj_root = str(Path(__file__).resolve().parent.parent.parent)
            if _proj_root not in sys.path:
                sys.path.insert(0, _proj_root)

            from universe_save import save_to_supabase

            csv_path = data_dir / "universe_current.csv"
            if csv_path.exists():
                df = pd.read_csv(csv_path)
                count = asyncio.run(save_to_supabase(df))
                log.info("Supabase upload 완료: %d rows upserted", count)
            else:
                log.warning("universe_current.csv 없음 — Supabase 업로드 스킵")

        return exit_code
    except Exception as e:
        log.exception("pipeline crashed: %s", type(e).__name__)
        if args.save_raw:
            log.info(
                "디버깅: 일부 원시 응답이 다음 디렉터리에 저장되었을 수 있습니다 — %s",
                (data_dir / "raw_fmp").resolve(),
            )
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())

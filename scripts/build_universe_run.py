#!/usr/bin/env python3
"""
유니버스 빌드 실행 스크립트
============================
사용법:
  python scripts/build_universe_run.py

실행 결과:
  data/universe/universe_current.csv    ← 뉴스 수집 대상 종목 (PASS 목록)
  data/universe/universe_excluded.csv   ← 제외된 종목 및 사유
  data/universe/universe_summary.csv    ← 제외 사유별 집계
  data/universe/universe_build_log.json ← 빌드 메타 로그

첫 실행 후 tasks.py의 load_all_tickers()가 자동으로
universe_current.csv 를 읽어 뉴스 수집 대상으로 사용합니다.
"""

import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# 프로젝트 루트를 sys.path에 추가
ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

# universe 패키지 내부의 flat import 지원
UNIVERSE_PKG = ROOT / "app" / "universe"
sys.path.insert(0, str(UNIVERSE_PKG))

from app.universe.universe_runner import run_universe_build, UniverseBuildConfig


def main():
    print("=" * 55)
    print("GoodNews AI — 유니버스 빌드 시작")
    print("=" * 55)

    # FMP_API_KEY 확인
    if not os.environ.get("FMP_API_KEY", "").strip():
        print("\n[오류] FMP_API_KEY 환경변수가 설정되지 않았습니다.")
        print("  .env 파일에 FMP_API_KEY=your_key_here 를 추가하세요.")
        sys.exit(1)

    config = UniverseBuildConfig(
        data_dir=ROOT / "data" / "universe",
        min_market_cap=100_000_000.0,   # 시총 1억 USD 이상
        exchanges="NASDAQ,NYSE,AMEX",
        country=None,                    # ADR 포함 (country 필터 미적용)
        save_raw=True,                   # 디버깅용 원본 JSON 저장
        profile_enrich_new_only=False,   # 첫 실행: 신규 종목 없으므로 False
        log_level="INFO",
    )

    print(f"\n대상 거래소: {config.exchanges}")
    print(f"시총 하한:   ${config.min_market_cap:,.0f} USD")
    print(f"저장 경로:   {config.data_dir.resolve()}")
    print(f"\n수집 중... (수분 소요될 수 있습니다)\n")

    result = run_universe_build(config)

    print("\n" + "=" * 55)
    if result.success:
        print(f"[완료] 유니버스 빌드 성공!")
        print(f"  포함 종목 수:  {result.included_count:,}개")
        print(f"  스냅샷 날짜:   {result.snapshot_date}")
        print(f"  저장 위치:     {result.data_dir}")
        print(f"\n  상위 10개 종목: {result.tickers[:10]}")
        print("\n이제 뉴스 수집 배치를 실행할 수 있습니다:")
        print("  python scripts/test_pipeline.py")
    else:
        print(f"[실패] 빌드 실패 (exit_code={result.exit_code})")
        print("  로그를 확인하거나 --save-raw 옵션으로 raw_fmp/ 디렉터리를 검토하세요.")
        sys.exit(result.exit_code)


if __name__ == "__main__":
    main()

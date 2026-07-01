"""전체 유니버스 백필 래퍼 — 티커 파일을 읽어 backfill_full.main() 직접 호출."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backfill_full import main as backfill_main

if __name__ == "__main__":
    ticker_file = "data/all_universe_tickers.txt"
    with open(ticker_file) as f:
        tickers_str = f.read().strip()

    tickers_count = len(tickers_str.split(","))
    print(f"[run_backfill_universe] 총 {tickers_count}개 종목 백필 시작", flush=True)

    # sys.argv를 설정해 argparse가 올바른 인자를 받도록 함
    sys.argv = [
        "backfill_full.py",
        "--weeks", "12",
        "--tickers", tickers_str,
        "--skip-benchmarks",
        "--force",
    ]
    asyncio.run(backfill_main())

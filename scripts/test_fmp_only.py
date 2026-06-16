#!/usr/bin/env python3
import os, sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
UNIVERSE_PKG = ROOT / "app" / "universe"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(UNIVERSE_PKG))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

def ok(msg):   print(f"  ✅ {msg}")
def fail(msg): print(f"  ❌ {msg}")
def info(msg): print(f"     {msg}")
def section(t): print(f"\n[{t}]")

def test_env():
    section("1. 환경변수 확인")
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        fail("FMP_API_KEY 가 .env에 없습니다.")
        return False
    masked = key[:4] + "****" + key[-4:] if len(key) > 8 else "****"
    ok(f"FMP_API_KEY 확인됨: {masked}")
    return True

def test_screener():
    section("2. FMP /stable/company-screener 호출")
    try:
        from fmp_client import fmp_get, ensure_fmp_session
        ensure_fmp_session()
        rows = fmp_get("/stable/company-screener", {
            "exchange": "NASDAQ", "isActivelyTrading": "true",
            "isEtf": "false", "isFund": "false", "limit": 5,
        })
        if not isinstance(rows, list):
            fail(f"응답이 list가 아님: {type(rows).__name__} | {str(rows)[:200]}")
            return False
        ok(f"응답 수신: {len(rows)}행")
        if rows:
            s = rows[0]
            info(f"샘플: {s.get('symbol')} | {str(s.get('companyName',''))[:30]} | marketCap={s.get('marketCap')}")
        return True
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
        return False

def test_stock_news():
    section("3. FMP /api/v3/stock_news 뉴스 수집 (AAPL)")
    try:
        from fmp_client import fmp_get
        rows = fmp_get("/stable/news/stock", {"symbols": "AAPL", "limit": 3})
        if not isinstance(rows, list) or not rows:
            fail(f"뉴스 0건 또는 오류: {str(rows)[:200]}")
            return False
        ok(f"뉴스 수신: {len(rows)}건")
        for i, item in enumerate(rows[:3], 1):
            info(f"  [{i}] {item.get('publishedDate','')[:10]} | {str(item.get('title',''))[:60]}")
        return True
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
        return False

def test_etf_list():
    section("4. FMP /stable/etf-list 호출")
    try:
        from fmp_client import fmp_get
        rows = fmp_get("/stable/etf-list", {})
        if not isinstance(rows, list):
            fail(f"응답이 list가 아님")
            return False
        ok(f"ETF 목록 수신: {len(rows):,}개")
        return True
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
        return False

def main():
    print("=" * 50)
    print("GoodNews AI — FMP API 연결 테스트")
    print("=" * 50)
    results = {}
    results["env"] = test_env()
    if not results["env"]:
        print("\n⛔ FMP_API_KEY 없이는 이후 테스트 불가.")
        sys.exit(1)
    results["screener"]  = test_screener()
    results["news"]      = test_stock_news()
    results["etf_list"]  = test_etf_list()

    passed = sum(1 for v in results.values() if v)
    total  = len(results)
    print("\n" + "=" * 50)
    print(f"결과: {passed}/{total} 통과")
    for name, v in results.items():
        print(f"  {'✅' if v else '❌'}  {name}")
    print()
    if passed == total:
        print("🎉 모두 통과! 다음 실행:")
        print("   python scripts/build_universe_run.py")
    else:
        print("❌ 실패 항목 확인 후 재시도하세요.")

if __name__ == "__main__":
    main()
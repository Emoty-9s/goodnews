"""
FMP 가격/섹터 엔드포인트 응답 구조 확인용 스크립트.

주간 리포트의 가격 변동률(종목/섹터/S&P500) 수집 방식을 잡기 위해
실제 응답의 필드명, 날짜 정렬 순서, 값의 형태(절대값 vs %)를 출력한다.

실행: python scripts/check_price_endpoints.py
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

# Windows 콘솔(cp949) 한글/특수문자 출력 깨짐 방지
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import httpx

from app.core.config import get_settings

settings = get_settings()
BASE = "https://financialmodelingprep.com"

# 테스트 기간 (이번 주 월~금)
FROM = "2026-06-08"
TO = "2026-06-12"


def _section(title: str):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def _dump(data, limit: int = 5):
    """리스트면 앞 limit개, 그 외엔 전체를 보기 좋게 출력."""
    if isinstance(data, list):
        print(f"(list, 총 {len(data)}건 — 앞 {min(limit, len(data))}건 표시)")
        for item in data[:limit]:
            print(json.dumps(item, ensure_ascii=False))
        if data:
            print(f"\n필드 키: {sorted(data[0].keys())}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])


async def get(client: httpx.AsyncClient, path: str, params: dict):
    url = f"{BASE}{path}"
    full = {**params, "apikey": settings.fmp_api_key}
    try:
        r = await client.get(url, params=full, timeout=30)
        print(f"\nGET {path}?{ '&'.join(f'{k}={v}' for k,v in params.items()) }")
        print(f"  status={r.status_code}")
        if r.status_code != 200:
            print(f"  body(앞 500자): {r.text[:500]}")
            return None
        return r.json()
    except Exception as e:
        print(f"  요청 실패: {e}")
        return None


async def main():
    async with httpx.AsyncClient() as client:
        # (1) 종목 주간 가격
        _section("(1) 종목 EOD light — AAPL")
        data = await get(
            client,
            "/stable/historical-price-eod/light",
            {"symbol": "AAPL", "from": FROM, "to": TO},
        )
        if data is not None:
            _dump(data)

        # (2) S&P500 지수 — ^GSPC / ^SPX 둘 다 시도
        for idx_sym in ("^GSPC", "^SPX"):
            _section(f"(2) 지수 EOD light — symbol={idx_sym}")
            data = await get(
                client,
                "/stable/historical-price-eod/light",
                {"symbol": idx_sym, "from": FROM, "to": TO},
            )
            if data is not None:
                _dump(data)

        # (3-a) 섹터 목록
        _section("(3a) available-sectors")
        data = await get(client, "/stable/available-sectors", {})
        if data is not None:
            _dump(data, limit=20)

        # (3-b) 섹터 퍼포먼스 (exchange 파라미터 없음 → 기본값)
        _section("(3b) historical-sector-performance — Technology (no exchange)")
        data = await get(
            client,
            "/stable/historical-sector-performance",
            {"sector": "Technology", "from": FROM, "to": TO},
        )
        if data is not None:
            _dump(data, limit=10)

        # (3-c) 섹터 퍼포먼스 — exchange=NYSE 명시
        _section("(3c) historical-sector-performance — Technology + exchange=NYSE")
        data = await get(
            client,
            "/stable/historical-sector-performance",
            {"sector": "Technology", "exchange": "NYSE", "from": FROM, "to": TO},
        )
        if data is not None:
            _dump(data, limit=10)
            exchanges = sorted({d.get("exchange") for d in data}) if isinstance(data, list) else []
            print(f"\n반환된 exchange 값: {exchanges}")


if __name__ == "__main__":
    asyncio.run(main())

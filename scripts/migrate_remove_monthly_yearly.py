"""
news_summaries 테이블에서 monthly/yearly 행을 삭제하는 DB 마이그레이션.

실행: python scripts/migrate_remove_monthly_yearly.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from sqlalchemy import text
from app.models.database import AsyncSessionLocal


async def main():
    async with AsyncSessionLocal() as session:
        # 삭제될 행 수 미리 확인
        result = await session.execute(
            text(
                "SELECT digest_type, COUNT(*) AS cnt "
                "FROM news_summaries "
                "WHERE digest_type IN ('monthly', 'yearly') "
                "GROUP BY digest_type"
            )
        )
        rows = result.mappings().all()

        if not rows:
            print("삭제할 행이 없습니다 (monthly/yearly 데이터 없음).")
            return

        total = 0
        print("\n삭제 대상:")
        for row in rows:
            print(f"  digest_type={row['digest_type']:10}  {row['cnt']:>6}건")
            total += row["cnt"]
        print(f"  합계: {total}건\n")

        answer = input(f"위 {total}건을 삭제합니다. 계속하려면 yes를 입력하세요: ").strip().lower()
        if answer != "yes":
            print("취소됐습니다.")
            return

        await session.execute(
            text("DELETE FROM news_summaries WHERE digest_type IN ('monthly', 'yearly')")
        )
        await session.commit()
        print(f"\n완료: {total}건 삭제됐습니다.")


if __name__ == "__main__":
    asyncio.run(main())

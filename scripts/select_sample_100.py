"""
시뮬레이션용 100종목 선정 스크립트.

선정 기준
---------
1. universe_current.csv 에서 universe_status='included' 이고
   is_etf=False, is_fund=False, is_common_stock_like=True 인 종목만 사용.
2. market_cap 분위로 3개 그룹 구성:
   - large: 상위 10% (90th percentile 초과)
   - mid:   40~60 percentile 구간
   - small: 하위 10% (10th percentile 이하)
   각 그룹 목표: large=34, mid=33, small=33
3. 각 그룹 내에서 11개 섹터 균등 배분 (그룹 인원 ÷ 11, 남으면 대형 섹터 우선 보충).
   섹터별 종목이 목표보다 적으면 있는 만큼만 사용, 부족분은 다른 섹터에서 보충.
4. 같은 그룹/섹터 내 랜덤 샘플 (seed=42, 재현 가능).

출력
----
- 콘솔: 그룹별·섹터별 종목 리스트 + market_cap
- sim_results/sample_100_tickers.json
- sim_results/sample_100_tickers.txt (심볼 줄바꿈 구분)

실행: python scripts/select_sample_100.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd

# ──────────────────────────────────────────
# 설정
# ──────────────────────────────────────────
UNIVERSE_CSV = ROOT / "data" / "universe" / "universe_current.csv"
OUT_DIR = ROOT / "sim_results"

GROUP_TARGETS = {"large": 34, "mid": 33, "small": 33}

SECTORS = [
    "Technology", "Healthcare", "Financial Services", "Industrials",
    "Consumer Cyclical", "Energy", "Basic Materials", "Real Estate",
    "Communication Services", "Consumer Defensive", "Utilities",
]

SEED = 42


# ──────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────

def _fmt_cap(v: float) -> str:
    if v >= 1e12:
        return f"${v/1e12:.2f}T"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    return f"${v/1e6:.0f}M"


def _pick_from_group(df: pd.DataFrame, target: int, rng: np.random.Generator) -> pd.DataFrame:
    """
    그룹 df 에서 11개 섹터 균등 배분으로 target 개 추출.
    섹터 부족 시 남은 슬롯을 다른 섹터에서 보충(market_cap 내림차순 상위).
    """
    if len(df) <= target:
        return df.copy()

    per_sector = target // len(SECTORS)
    remainder = target % len(SECTORS)

    # 섹터별 셔플 (재현용 seed 유지)
    picked_indices = []
    deficit = 0

    for i, sector in enumerate(SECTORS):
        sector_df = df[df["sector"] == sector]
        n_want = per_sector + (1 if i < remainder else 0)
        n_avail = len(sector_df)

        if n_avail == 0:
            deficit += n_want
            continue

        if n_avail <= n_want:
            picked_indices.extend(sector_df.index.tolist())
            deficit += n_want - n_avail
        else:
            sampled = sector_df.sample(n=n_want, random_state=SEED + i)
            picked_indices.extend(sampled.index.tolist())

    # 부족분: 아직 안 뽑힌 나머지에서 market_cap 상위로 보충
    if deficit > 0:
        remaining = df.loc[~df.index.isin(picked_indices)]
        extra = remaining.nlargest(deficit, "market_cap")
        picked_indices.extend(extra.index.tolist())

    return df.loc[picked_indices].copy()


# ──────────────────────────────────────────
# 메인
# ──────────────────────────────────────────

def main():
    rng = np.random.default_rng(SEED)

    # 로드 & 필터
    df = pd.read_csv(UNIVERSE_CSV, low_memory=False)
    df = df[df["universe_status"] == "included"].copy()
    df = df[df["is_etf"].fillna(False).astype(bool) == False]
    df = df[df["is_fund"].fillna(False).astype(bool) == False]
    df = df[df["is_common_stock_like"].fillna(False).astype(bool) == True]
    df = df[df["market_cap"].notna()]
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df = df.drop_duplicates(subset=["symbol"])
    print(f"필터 후 유니버스: {len(df)}개 종목")

    # 그룹 분위 계산
    p10 = df["market_cap"].quantile(0.10)
    p40 = df["market_cap"].quantile(0.40)
    p60 = df["market_cap"].quantile(0.60)
    p90 = df["market_cap"].quantile(0.90)

    groups = {
        "large": df[df["market_cap"] > p90].copy(),
        "mid":   df[(df["market_cap"] >= p40) & (df["market_cap"] <= p60)].copy(),
        "small": df[df["market_cap"] <= p10].copy(),
    }
    for g, gdf in groups.items():
        print(f"  {g}: {len(gdf)}개 (cap {_fmt_cap(gdf['market_cap'].min())}~{_fmt_cap(gdf['market_cap'].max())})")

    # 각 그룹에서 섹터 균등 추출
    selected: dict[str, list[dict]] = {}
    all_symbols: list[str] = []

    for group_name, target in GROUP_TARGETS.items():
        gdf = groups[group_name]
        picked = _pick_from_group(gdf, target, rng)
        picked = picked.sort_values("market_cap", ascending=False)

        rows = []
        for _, row in picked.iterrows():
            rows.append({
                "symbol": row["symbol"],
                "sector": str(row.get("sector", "") or ""),
                "exchange": str(row.get("exchange_short_name", "") or ""),
                "market_cap": float(row["market_cap"]),
            })
        selected[group_name] = rows
        all_symbols.extend(r["symbol"] for r in rows)

        # 콘솔 출력
        print(f"\n── {group_name.upper()} ({len(rows)}개) ──")
        sector_dist: dict[str, list[str]] = {}
        for r in rows:
            sector_dist.setdefault(r["sector"], []).append(r["symbol"])
        for sec in SECTORS:
            syms = sector_dist.get(sec, [])
            if syms:
                syms_str = ", ".join(syms)
                print(f"  {sec:25} ({len(syms)}): {syms_str}")

    total = sum(len(v) for v in selected.values())
    print(f"\n총 선정: {total}개 / 목표 100개")
    if len(set(all_symbols)) != len(all_symbols):
        dups = [s for s in all_symbols if all_symbols.count(s) > 1]
        print(f"⚠️  중복 심볼: {set(dups)}")

    # 저장
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "sample_100_tickers.json"
    txt_path  = OUT_DIR / "sample_100_tickers.txt"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(selected, f, ensure_ascii=False, indent=2)
    print(f"\n저장: {json_path}")

    with open(txt_path, "w", encoding="utf-8") as f:
        for sym in all_symbols:
            f.write(sym + "\n")
    print(f"저장: {txt_path}")
    print(f"콤마 구분 (앞 20개): {','.join(all_symbols[:20])},...")


if __name__ == "__main__":
    main()

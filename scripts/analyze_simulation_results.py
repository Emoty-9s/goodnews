"""
시뮬레이션 결과 분석 스크립트.

usage_summary.json 과 sample_100_tickers.json 을 읽어
그룹별(large/mid/small) 종목당 평균 호출 수/토큰 수를 비교하고
전체 유니버스(4,005종목) 한 달 백필 비용을 추정한다.

실행: python scripts/analyze_simulation_results.py
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

# ──────────────────────────────────────────
# 설정
# ──────────────────────────────────────────
DRY_RUN_DIR  = ROOT / "sim_results" / "dry_run_2026-05-01_2026-05-31"
USAGE_JSON   = DRY_RUN_DIR / "usage_summary.json"
SAMPLE_JSON  = ROOT / "sim_results" / "sample_100_tickers.json"
UNIVERSE_CSV = ROOT / "data" / "universe" / "universe_current.csv"


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _fmt(n: int) -> str:
    return f"{n:>10,}"


def main():
    if not USAGE_JSON.exists():
        print(f"아직 usage_summary.json 이 없습니다: {USAGE_JSON}")
        return

    usage    = load_json(USAGE_JSON)
    sample   = load_json(SAMPLE_JSON)

    # ── 1) 결과 JSON 전체 출력 ──────────────────────────────
    sep = "=" * 70
    print(sep)
    print("  usage_summary.json 내용")
    print(sep)
    print(json.dumps(usage, ensure_ascii=False, indent=2))
    print()

    # ── 2) 종목→그룹 매핑 ──────────────────────────────────
    ticker_to_group: dict[str, str] = {}
    for group, rows in sample.items():
        for row in rows:
            ticker_to_group[row["symbol"]] = group

    # ── 3) 종목별 저장 파일 개수로 실제 호출 수 추정 ──────────
    # usage.json 의 by_digest_type 는 전체 합산이므로 종목별 분해가 안됨.
    # 대신 저장된 JSON 파일 수로 그룹별 생성 건수를 센다.
    group_counts: dict[str, dict] = {
        "large": {"daily": 0, "weekly_draft": 0, "weekly_final": 0, "tickers": 0},
        "mid":   {"daily": 0, "weekly_draft": 0, "weekly_final": 0, "tickers": 0},
        "small": {"daily": 0, "weekly_draft": 0, "weekly_final": 0, "tickers": 0},
    }

    all_tickers = list(ticker_to_group.keys())
    for ticker in all_tickers:
        group = ticker_to_group.get(ticker)
        if group not in group_counts:
            continue
        tdir = DRY_RUN_DIR / ticker
        if not tdir.exists():
            continue
        group_counts[group]["tickers"] += 1
        files = list(tdir.glob("*.json"))
        for f in files:
            name = f.stem
            if name.startswith("daily_"):
                group_counts[group]["daily"] += 1
            elif name.startswith("weekly_") and name.endswith("_draft"):
                group_counts[group]["weekly_draft"] += 1
            elif name.startswith("weekly_") and name.endswith("_final"):
                group_counts[group]["weekly_final"] += 1

    print(sep)
    print("  그룹별 생성 건수 (종목 폴더 내 파일 기준)")
    print(sep)
    print(f"{'그룹':8}  {'종목수':>5}  {'daily':>8}  {'wk-draft':>9}  {'wk-final':>9}  {'합계':>8}  {'종목당 합계':>12}")
    for g, c in group_counts.items():
        n = c["tickers"] if c["tickers"] else 1
        total = c["daily"] + c["weekly_draft"] + c["weekly_final"]
        avg   = total / n
        print(
            f"{g:8}  {c['tickers']:>5}  {c['daily']:>8}  {c['weekly_draft']:>9}"
            f"  {c['weekly_final']:>9}  {total:>8}  {avg:>12.1f}"
        )

    # ── 4) 전체 유니버스 추정 ───────────────────────────────
    print()
    print(sep)
    print("  전체 유니버스(4,005종목) 1개월 백필 추정")
    print(sep)

    import pandas as pd
    df = pd.read_csv(UNIVERSE_CSV, low_memory=False)
    df = df[df["universe_status"] == "included"]
    df = df[df["market_cap"].notna()]

    p10 = df["market_cap"].quantile(0.10)
    p40 = df["market_cap"].quantile(0.40)
    p60 = df["market_cap"].quantile(0.60)
    p90 = df["market_cap"].quantile(0.90)

    universe_groups = {
        "large": int((df["market_cap"] > p90).sum()),
        "mid":   int(((df["market_cap"] >= p40) & (df["market_cap"] <= p60)).sum()),
        "small": int((df["market_cap"] <= p10).sum()),
        "other": 0,   # 나머지 (40-40% + top 10% 내부)
    }
    # other = 전체 - large - mid - small
    universe_groups["other"] = len(df) - universe_groups["large"] - universe_groups["mid"] - universe_groups["small"]

    # 그룹별 종목당 평균 총 호출 수 (실제 측정치)
    avg_calls: dict[str, float] = {}
    for g, c in group_counts.items():
        n = c["tickers"] if c["tickers"] else 1
        total_llm = c["daily"] + c["weekly_draft"] + c["weekly_final"]
        avg_calls[g] = total_llm / n
    # "other" 그룹(40~90 percentile)은 mid와 large 사이 → mid 기준 사용
    avg_calls["other"] = avg_calls.get("mid", 5.0)

    print(f"\n{'그룹':8}  {'유니버스 종목수':>14}  {'종목당 평균 호출':>16}  {'그룹 예상 호출':>14}")
    total_calls_est = 0
    for g, n_universe in universe_groups.items():
        avg = avg_calls.get(g, avg_calls.get("mid", 5.0))
        est = n_universe * avg
        total_calls_est += est
        print(f"{g:8}  {n_universe:>14}  {avg:>16.1f}  {int(est):>14,}")

    print(f"\n  예상 총 LLM 호출 수: {int(total_calls_est):,}")

    # ── 5) 모델별 토큰 사용량 추정 ──────────────────────────
    by_model = usage.get("by_model", {})
    total_calls_sample = sum(v["calls"] for v in by_model.values())
    if total_calls_sample == 0:
        print("  (LLM 호출 데이터 없음 — 분석 불가)")
        return

    scale = total_calls_est / total_calls_sample  # 100종목 → 4005종목 스케일 팩터
    print(f"\n  스케일 팩터 (100종목→유니버스): {scale:.1f}x")
    print()
    print(f"{'모델':40}  {'100종목 호출':>12}  {'100종목 입력토큰':>16}  {'100종목 출력토큰':>16}  {'유니버스 입력토큰':>18}  {'유니버스 출력토큰':>18}")
    for model, d in by_model.items():
        m_scale = (d["calls"] / total_calls_sample) * total_calls_est / d["calls"] if d["calls"] else scale
        est_inp = int(d["input_tokens"] * scale)
        est_out = int(d["output_tokens"] * scale)
        print(
            f"  {model:38}  {d['calls']:>12,}  {d['input_tokens']:>16,}  {d['output_tokens']:>16,}"
            f"  {est_inp:>18,}  {est_out:>18,}"
        )

    # ── 6) digest_type별 비율 ───────────────────────────────
    print()
    print("  digest_type별 분포 (100종목 기준)")
    by_digest = usage.get("by_digest_type", {})
    print(f"{'타입':18}  {'호출':>8}  {'입력토큰':>12}  {'출력토큰':>12}")
    for dt, d in by_digest.items():
        print(f"  {dt:16}  {d['calls']:>8,}  {d['input_tokens']:>12,}  {d['output_tokens']:>12,}")

    # ── 7) 요약 ────────────────────────────────────────────
    total_inp_sample = sum(v["input_tokens"] for v in by_model.values())
    total_out_sample = sum(v["output_tokens"] for v in by_model.values())
    print()
    print(sep)
    print("  요약")
    print(sep)
    print(f"  [100종목 실측] 총 호출={total_calls_sample:,}  입력토큰={total_inp_sample:,}  출력토큰={total_out_sample:,}")
    print(f"  [4005종목 추정] 총 호출≈{int(total_calls_est):,}  입력토큰≈{int(total_inp_sample*scale):,}  출력토큰≈{int(total_out_sample*scale):,}")
    stats = usage.get("stats", {})
    print(f"  [파이프라인 통계]")
    for stage, s in stats.items():
        ok   = s.get("ok", 0)
        skip = s.get("skip", 0)
        fail = s.get("fail", 0)
        total_stage = ok + skip + fail
        pct  = ok / total_stage * 100 if total_stage else 0
        print(f"    {stage:<18}  ok={ok:>4}  skip={skip:>5}  fail={fail:>3}  (뉴스 발생률 {pct:.1f}%)")
    print(sep)


if __name__ == "__main__":
    main()

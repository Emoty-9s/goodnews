"""
Q1(2026-01-01 ~ 2026-03-31) dry-run 시뮬레이션 결과 분석.

usage_summary.json + sample_100_tickers.json 을 읽어
90일 실측치를 1개월 평균으로 정규화한 뒤, 전체 유니버스 비용을 추정한다.

실행: python scripts/analyze_simulation_results_q1.py
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
DRY_RUN_DIR = ROOT / "sim_results" / "dry_run_2026-01-01_2026-03-31"
USAGE_JSON = DRY_RUN_DIR / "usage_summary.json"
SAMPLE_JSON = ROOT / "sim_results" / "sample_100_tickers.json"
UNIVERSE_CSV = ROOT / "data" / "universe" / "universe_current.csv"

SIMULATION_DAYS = 90  # 2026-01-01 ~ 2026-03-31
MONTHLY_NORMALIZE_FACTOR = SIMULATION_DAYS / 30.0  # 3.0


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _normalize_usage(usage: dict) -> dict:
    """90일 실측치를 1개월 평균으로 정규화한 복사본 반환."""
    factor = MONTHLY_NORMALIZE_FACTOR
    norm = json.loads(json.dumps(usage))  # deep copy

    for bucket_key in ("by_model", "by_digest_type"):
        bucket = norm.get(bucket_key, {})
        for key, d in bucket.items():
            d["calls"] = d["calls"] / factor
            d["input_tokens"] = int(d["input_tokens"] / factor)
            d["output_tokens"] = int(d["output_tokens"] / factor)

    return norm


def main():
    if not USAGE_JSON.exists():
        print(f"아직 usage_summary.json 이 없습니다: {USAGE_JSON}")
        return

    usage = load_json(USAGE_JSON)
    sample = load_json(SAMPLE_JSON)
    usage_monthly = _normalize_usage(usage)

    sep = "=" * 70
    print(sep)
    print(f"  Q1 dry-run 분석  ({SIMULATION_DAYS}일 → 1개월 정규화, factor={MONTHLY_NORMALIZE_FACTOR:.1f})")
    print(sep)
    print()
    print("  [원본 usage_summary.json — 90일 실측]")
    print(json.dumps(usage, ensure_ascii=False, indent=2))
    print()
    print("  [1개월 정규화 usage (÷3.0)]")
    print(json.dumps(usage_monthly, ensure_ascii=False, indent=2))
    print()

    # ── 종목→그룹 매핑 ──
    ticker_to_group: dict[str, str] = {}
    for group, rows in sample.items():
        for row in rows:
            ticker_to_group[row["symbol"]] = group

    group_counts: dict[str, dict] = {
        "large": {"daily": 0, "weekly_draft": 0, "weekly_final": 0, "midterm": 0, "tickers": 0},
        "mid":   {"daily": 0, "weekly_draft": 0, "weekly_final": 0, "midterm": 0, "tickers": 0},
        "small": {"daily": 0, "weekly_draft": 0, "weekly_final": 0, "midterm": 0, "tickers": 0},
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
        for f in tdir.glob("*.json"):
            name = f.stem
            if name.startswith("daily_"):
                group_counts[group]["daily"] += 1
            elif name.startswith("weekly_") and name.endswith("_draft"):
                group_counts[group]["weekly_draft"] += 1
            elif name.startswith("weekly_") and name.endswith("_final"):
                group_counts[group]["weekly_final"] += 1
            elif name.startswith("midterm_"):
                group_counts[group]["midterm"] += 1

    sector_news_dir = DRY_RUN_DIR / "sector_news"
    sector_news_file_count = (
        len(list(sector_news_dir.glob("*.json"))) if sector_news_dir.exists() else 0
    )
    benchmarks_dir = DRY_RUN_DIR / "benchmarks"
    benchmark_file_count = (
        len(list(benchmarks_dir.glob("*.json"))) if benchmarks_dir.exists() else 0
    )

    print(sep)
    print("  그룹별 생성 건수 (종목 폴더 내 파일 기준, 90일 전체)")
    print(sep)
    print(
        f"{'그룹':8}  {'종목':>5}  {'daily':>7}  {'wk-drft':>8}  {'wk-fin':>8}  "
        f"{'midterm':>8}  {'합계':>7}  {'종목당':>8}"
    )
    for g, c in group_counts.items():
        n = c["tickers"] if c["tickers"] else 1
        total = c["daily"] + c["weekly_draft"] + c["weekly_final"] + c["midterm"]
        avg = total / n
        print(
            f"{g:8}  {c['tickers']:>5}  {c['daily']:>7}  {c['weekly_draft']:>8}  "
            f"{c['weekly_final']:>8}  {c['midterm']:>8}  {total:>7}  {avg:>8.1f}"
        )

    print()
    print(f"  sector_news 파일 수 (시장 전체, 종목 무관): {sector_news_file_count}건")
    print(f"  benchmarks 파일 수 (시장 전체): {benchmark_file_count}건")
    print(
        "  ※ sector_news는 종목 수와 무관하게 주 1회(금요일) 시장 전체에 대해 생성되는 "
        "고정 비용입니다. 유니버스 확장 시 종목 수에 비례하지 않습니다."
    )

    # ── 전체 유니버스 추정 ──
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
        "mid": int(((df["market_cap"] >= p40) & (df["market_cap"] <= p60)).sum()),
        "small": int((df["market_cap"] <= p10).sum()),
        "other": 0,
    }
    universe_groups["other"] = (
        len(df) - universe_groups["large"] - universe_groups["mid"] - universe_groups["small"]
    )

    # 종목당 평균 LLM 호출 (daily+weekly+midterm, 90일 → 1개월 정규화)
    avg_calls: dict[str, float] = {}
    for g, c in group_counts.items():
        n = c["tickers"] if c["tickers"] else 1
        total_llm = c["daily"] + c["weekly_draft"] + c["weekly_final"] + c["midterm"]
        avg_calls[g] = (total_llm / n) / MONTHLY_NORMALIZE_FACTOR
    avg_calls["other"] = avg_calls.get("mid", 5.0)

    print(f"\n{'그룹':8}  {'유니버스':>10}  {'종목당/월 호출':>14}  {'그룹 예상 호출/월':>16}")
    total_calls_est = 0.0
    for g, n_universe in universe_groups.items():
        avg = avg_calls.get(g, avg_calls.get("mid", 5.0))
        est = n_universe * avg
        total_calls_est += est
        print(f"{g:8}  {n_universe:>10}  {avg:>14.1f}  {int(est):>16,}")

    weeks_in_period = SIMULATION_DAYS / 7.0
    sector_news_calls_per_month = (
        (sector_news_file_count / weeks_in_period) * 4.3 if weeks_in_period else 0
    )
    total_calls_est_with_sector = total_calls_est + sector_news_calls_per_month

    print(f"\n  [종목 비례] daily+weekly+midterm 예상 호출/월: {int(total_calls_est):,}")
    print(
        f"  [고정 비용] sector_news 예상 호출/월: {sector_news_calls_per_month:.1f} "
        f"(90일 실측 {sector_news_file_count}건 → 월 환산, 종목 수 무관)"
    )
    print(f"  [합계] 예상 총 LLM 호출/월: {int(total_calls_est_with_sector):,}")

    # ── 모델별 토큰 (1개월 정규화 기준) ──
    by_model = usage_monthly.get("by_model", {})
    total_calls_sample = sum(v["calls"] for v in by_model.values())
    if total_calls_sample == 0:
        print("  (LLM 호출 데이터 없음 — 분석 불가)")
        return

    # 스케일: 100종목 1개월 → 유니버스 1개월 (종목 비례 부분만)
    scale_ticker = total_calls_est / total_calls_sample if total_calls_sample else 1

    print(f"\n  스케일 팩터 (100종목→유니버스, 종목 비례): {scale_ticker:.1f}x")
    print()
    print(
        f"{'모델':38}  {'100종/월 호출':>12}  {'100종/월 입력':>14}  {'100종/월 출력':>14}  "
        f"{'유니버스/월 입력':>16}  {'유니버스/월 출력':>16}"
    )
    for model, d in by_model.items():
        est_inp = int(d["input_tokens"] * scale_ticker)
        est_out = int(d["output_tokens"] * scale_ticker)
        print(
            f"  {model:36}  {d['calls']:>12.1f}  {d['input_tokens']:>14,}  "
            f"{d['output_tokens']:>14,}  {est_inp:>16,}  {est_out:>16,}"
        )

    # sector_news 토큰 (고정, 스케일 안 함)
    by_digest = usage_monthly.get("by_digest_type", {})
    sn = by_digest.get("sector_news", {})
    if sn:
        sn_calls_monthly = sn.get("calls", 0)
        print()
        print(
            f"  sector_news (고정/월): 호출≈{sector_news_calls_per_month:.1f}  "
            f"입력토큰≈{int(sn.get('input_tokens', 0) * sector_news_calls_per_month / sn_calls_monthly) if sn_calls_monthly else 0:,}  "
            f"(100종목 실측 sector_news는 시장 전체 1회/주)"
        )

    print()
    print("  digest_type별 (100종목, 1개월 정규화)")
    print(f"{'타입':18}  {'호출/월':>10}  {'입력토큰/월':>14}  {'출력토큰/월':>14}")
    for dt, d in by_digest.items():
        print(
            f"  {dt:16}  {d['calls']:>10.1f}  {d['input_tokens']:>14,}  {d['output_tokens']:>14,}"
        )

    total_inp_sample = sum(v["input_tokens"] for v in by_model.values())
    total_out_sample = sum(v["output_tokens"] for v in by_model.values())
    sn_inp = int(sn.get("input_tokens", 0)) if sn else 0
    sn_out = int(sn.get("output_tokens", 0)) if sn else 0

    print()
    print(sep)
    print("  요약")
    print(sep)
    print(
        f"  [100종목 실측 90일] "
        f"호출={sum(v['calls'] for v in usage.get('by_model', {}).values()):,.0f}  "
        f"입력={sum(v['input_tokens'] for v in usage.get('by_model', {}).values()):,}  "
        f"출력={sum(v['output_tokens'] for v in usage.get('by_model', {}).values()):,}"
    )
    print(
        f"  [100종목 1개월 정규화] "
        f"호출={total_calls_sample:.0f}  "
        f"입력={total_inp_sample:,}  출력={total_out_sample:,}"
    )
    print(
        f"  [4005종목 추정/월, 종목비례] "
        f"호출≈{int(total_calls_est):,}  "
        f"입력≈{int(total_inp_sample * scale_ticker):,}  "
        f"출력≈{int(total_out_sample * scale_ticker):,}"
    )
    print(
        f"  [4005종목 추정/월, sector_news 고정 추가] "
        f"호출≈{int(total_calls_est_with_sector):,}  "
        f"입력≈{int(total_inp_sample * scale_ticker + sn_inp):,}  "
        f"출력≈{int(total_out_sample * scale_ticker + sn_out):,}"
    )

    stats = usage.get("stats", {})
    print("  [파이프라인 통계 — 90일 전체]")
    for stage, s in stats.items():
        ok = s.get("ok", 0)
        skip = s.get("skip", 0)
        fail = s.get("fail", 0)
        tmpl = s.get("template", 0)
        total_stage = ok + skip + fail
        pct = ok / total_stage * 100 if total_stage else 0
        tmpl_str = f"  template={tmpl}" if tmpl else ""
        print(
            f"    {stage:<18}  ok={ok:>5}  skip={skip:>6}  fail={fail:>4}{tmpl_str}  "
            f"(성공률 {pct:.1f}%)"
        )

    print()
    print(
        "  ※ 위 추정치는 샘플 100종목 Q1 dry-run 기반이며, 실제 운영 시 "
        "뉴스 발생량·계절성·API 오류율에 따라 달라질 수 있습니다."
    )
    print(sep)


if __name__ == "__main__":
    main()

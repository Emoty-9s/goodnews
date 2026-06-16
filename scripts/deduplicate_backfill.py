#!/usr/bin/env python3
"""
data/backfill/ 의 월별 뉴스를 중복 제거해서 data/clean/ 에 월별 1개 파일로 저장.

중복 제거 규칙:
  [규칙 1] 동일 티커 내 같은 URL → 완전 동일 중복 제거
  [규칙 2] 다른 티커 간 같은 URL → 1건만 남기고 tickers 태그 병합
  [규칙 3] 동일 티커 내 제목 Jaccard >= 0.8 → 본문이 더 긴 것만 유지

실행: python scripts/deduplicate_backfill.py
"""
import hashlib
import json
import re
import sys
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

BACKFILL_DIR = ROOT / "data" / "backfill"
CLEAN_DIR = ROOT / "data" / "clean"

TITLE_SIMILARITY_THRESHOLD = 0.8
MONTH_PATTERN = re.compile(r"^\d{4}_\d{2}$")


# ── 유틸 ───────────────────────────────────────────

def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def title_words(title: str) -> set:
    return set(re.findall(r"\w+", (title or "").lower()))


def jaccard(words_a: set, words_b: set) -> float:
    if not words_a and not words_b:
        return 1.0
    if not words_a or not words_b:
        return 0.0
    inter = len(words_a & words_b)
    union = len(words_a | words_b)
    return inter / union if union else 0.0


# ── 월별 로드 ──────────────────────────────────────

def load_month_articles(month_dir: Path) -> list[dict]:
    """월 폴더의 모든 TICKER.json 을 읽어 평탄화한 기사 리스트 반환."""
    articles = []
    for json_file in sorted(month_dir.glob("*.json")):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception as e:
            logger.warning(f"파일 읽기 실패 {json_file.name}: {e}")
            continue
        if not isinstance(items, list):
            continue
        fallback_symbol = json_file.stem.upper()
        for item in items:
            symbol = (item.get("symbol") or fallback_symbol).upper()
            articles.append(
                {
                    "url": item.get("url", "") or "",
                    "title": item.get("title", "") or "",
                    "text": item.get("text", "") or "",
                    "published_at": item.get("publishedDate", "") or "",
                    "source": item.get("source", "") or "",
                    "symbol": symbol,
                }
            )
    return articles


# ── 규칙 1: 동일 티커 내 URL 중복 제거 ──────────────

def dedup_same_ticker_url(articles: list[dict]) -> list[dict]:
    seen = {}
    result = []
    for idx, a in enumerate(articles):
        url = a["url"]
        # URL 이 없으면 병합 대상에서 제외 (각각 고유 취급)
        key = (a["symbol"], url) if url else (a["symbol"], f"__nourl_{idx}")
        if key in seen:
            continue
        seen[key] = True
        result.append(a)
    return result


# ── 규칙 2: 크로스 티커 URL 병합 ────────────────────

def merge_cross_ticker(articles: list[dict]) -> list[dict]:
    by_url = {}
    order = []
    for idx, a in enumerate(articles):
        url = a["url"]
        key = url if url else f"__nourl_{idx}"
        if key not in by_url:
            merged = {
                "url": url,
                "title": a["title"],
                "text": a["text"],
                "published_at": a["published_at"],
                "source": a["source"],
                "tickers": [a["symbol"]],
            }
            by_url[key] = merged
            order.append(key)
        else:
            merged = by_url[key]
            if a["symbol"] not in merged["tickers"]:
                merged["tickers"].append(a["symbol"])
            # 더 긴 본문을 대표로 유지
            if len(a["text"]) > len(merged["text"]):
                merged["title"] = a["title"]
                merged["text"] = a["text"]
                merged["published_at"] = a["published_at"]
                merged["source"] = a["source"]
    return [by_url[k] for k in order]


# ── 규칙 3: 동일 티커 내 유사 제목 제거 ─────────────

def dedup_similar_titles(articles: list[dict]) -> list[dict]:
    n = len(articles)
    removed = [False] * n
    word_sets = [title_words(a["title"]) for a in articles]

    # 티커별로 묶어서 비교 (비교 범위를 같은 티커로 제한)
    ticker_to_indices: dict[str, list[int]] = {}
    for i, a in enumerate(articles):
        for t in a["tickers"]:
            ticker_to_indices.setdefault(t, []).append(i)

    for indices in ticker_to_indices.values():
        for pos_a in range(len(indices)):
            i = indices[pos_a]
            if removed[i]:
                continue
            for pos_b in range(pos_a + 1, len(indices)):
                j = indices[pos_b]
                if removed[j]:
                    continue
                if jaccard(word_sets[i], word_sets[j]) >= TITLE_SIMILARITY_THRESHOLD:
                    # 본문이 더 긴 쪽을 유지, 짧은 쪽 제거 + 티커 병합
                    if len(articles[i]["text"]) >= len(articles[j]["text"]):
                        for t in articles[j]["tickers"]:
                            if t not in articles[i]["tickers"]:
                                articles[i]["tickers"].append(t)
                        removed[j] = True
                    else:
                        for t in articles[i]["tickers"]:
                            if t not in articles[j]["tickers"]:
                                articles[j]["tickers"].append(t)
                        removed[i] = True
                        break

    return [a for i, a in enumerate(articles) if not removed[i]]


# ── 최종 출력 포맷 ─────────────────────────────────

def to_output(articles: list[dict]) -> list[dict]:
    out = []
    for a in articles:
        out.append(
            {
                "url": a["url"],
                "url_hash": sha256_hex(a["url"]),
                "title": a["title"],
                "text": a["text"],
                "published_at": a["published_at"],
                "source": a["source"],
                "tickers": sorted(a["tickers"]),
            }
        )
    return out


# ── 메인 ──────────────────────────────────────────

def main():
    if not BACKFILL_DIR.exists():
        logger.error(
            f"backfill 폴더가 없습니다: {BACKFILL_DIR}\n"
            f"먼저 python scripts/backfill_news.py 를 실행하세요"
        )
        sys.exit(1)

    month_dirs = sorted(
        d for d in BACKFILL_DIR.iterdir()
        if d.is_dir() and MONTH_PATTERN.match(d.name)
    )
    if not month_dirs:
        logger.error(f"처리할 월 폴더가 없습니다: {BACKFILL_DIR}")
        sys.exit(1)

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)

    total_raw = 0
    total_final = 0

    for month_dir in month_dirs:
        month_key = month_dir.name

        articles = load_month_articles(month_dir)
        raw_count = len(articles)
        total_raw += raw_count
        logger.info(f"[{month_key}] 원본: {raw_count:,}건")

        if raw_count == 0:
            logger.info(f"[{month_key}] 건너뜀 (원본 0건)")
            continue

        step1 = dedup_same_ticker_url(articles)
        logger.info(
            f"[{month_key}] URL 중복 제거: {len(step1):,}건 ({len(step1) - raw_count:,}건)"
        )

        step2 = merge_cross_ticker(step1)
        logger.info(
            f"[{month_key}] 크로스 티커 병합: {len(step2):,}건 ({len(step2) - len(step1):,}건)"
        )

        step3 = dedup_similar_titles(step2)
        logger.info(
            f"[{month_key}] 유사 뉴스 제거: {len(step3):,}건 ({len(step3) - len(step2):,}건)"
        )

        output = to_output(step3)
        total_final += len(output)

        out_path = CLEAN_DIR / f"{month_key}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        logger.info(f"[{month_key}] 저장 완료: {out_path.relative_to(ROOT)}")

    removed = total_raw - total_final
    pct = (removed / total_raw * 100) if total_raw else 0.0

    print()
    print("===== 중복 제거 완료 =====")
    print(f"원본 총계:   {total_raw:,}건")
    print(f"최종 총계:   {total_final:,}건")
    print(f"제거된 건수: {removed:,}건 ({pct:.1f}%)")
    print(f"{CLEAN_DIR.relative_to(ROOT)} 에 저장 완료")


if __name__ == "__main__":
    main()

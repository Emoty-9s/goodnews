from functools import lru_cache

import numpy as np
from loguru import logger
from sentence_transformers import SentenceTransformer

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
SIMILARITY_THRESHOLD = 0.85


@lru_cache(maxsize=1)
def get_model() -> SentenceTransformer:
    """sentence-transformers 모델을 1회만 로드 (싱글톤)."""
    logger.info(f"sentence-transformers 모델 로드: {MODEL_NAME}")
    return SentenceTransformer(MODEL_NAME)


def _encode_titles(titles: list[str]) -> np.ndarray:
    """제목 리스트를 정규화된 임베딩 행렬로 변환."""
    model = get_model()
    embeddings = model.encode(
        titles,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    return embeddings


def deduplicate(articles: list[dict]) -> list[dict]:
    """
    단일 티커 뉴스 리스트에서 제목 기준 중복 제거.

    cosine similarity > 0.85 이면 중복으로 보고, 먼저 나온 뉴스만 유지한다.
    """
    if len(articles) <= 1:
        return list(articles)

    titles = [a.get("title", "") or "" for a in articles]
    embeddings = _encode_titles(titles)

    kept: list[dict] = []
    kept_indices: list[int] = []

    for i, article in enumerate(articles):
        is_duplicate = False
        for j in kept_indices:
            similarity = float(np.dot(embeddings[i], embeddings[j]))
            if similarity > SIMILARITY_THRESHOLD:
                is_duplicate = True
                break
        if not is_duplicate:
            kept.append(article)
            kept_indices.append(i)

    return kept


def deduplicate_cross_ticker(
    news_by_ticker: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """
    {ticker: [news_list]} 전체에서 중복 제거.

    1) 각 티커 내부 중복 제거.
    2) 여러 티커에 걸친 동일 뉴스는 첫 번째 티커에 tickers 필드로 병합하고,
       나머지 티커에서는 제거.
    """
    # 1단계: 티커별 내부 중복 제거
    deduped_by_ticker: dict[str, list[dict]] = {}
    for ticker, articles in news_by_ticker.items():
        before = len(articles)
        deduped = deduplicate(articles)
        after = len(deduped)
        removed = before - after
        logger.info(f"dedup: {ticker} {before}건 → {after}건 ({removed}건 제거)")
        deduped_by_ticker[ticker] = deduped

    # 2단계: 티커 간 교차 중복 병합
    # 모든 (ticker, article) 쌍을 펼쳐서 임베딩 후 비교
    flat: list[tuple[str, dict]] = []
    for ticker, articles in deduped_by_ticker.items():
        for article in articles:
            flat.append((ticker, article))

    if not flat:
        return {ticker: [] for ticker in news_by_ticker}

    titles = [article.get("title", "") or "" for _, article in flat]
    embeddings = _encode_titles(titles)

    result: dict[str, list[dict]] = {ticker: [] for ticker in deduped_by_ticker}
    # 대표(첫 등장) 항목 인덱스 → 결과 내 article 참조
    representative_articles: dict[int, dict] = {}
    kept_indices: list[int] = []

    for i, (ticker, article) in enumerate(flat):
        matched_rep = None
        for j in kept_indices:
            similarity = float(np.dot(embeddings[i], embeddings[j]))
            if similarity > SIMILARITY_THRESHOLD:
                matched_rep = j
                break

        if matched_rep is None:
            # 신규 대표 뉴스: tickers 필드 부여 후 결과에 추가
            new_article = dict(article)
            new_article["tickers"] = [ticker]
            result[ticker].append(new_article)
            representative_articles[i] = new_article
            kept_indices.append(i)
        else:
            # 기존 대표 뉴스에 ticker 병합 (중복 방지)
            rep_article = representative_articles[matched_rep]
            if ticker not in rep_article["tickers"]:
                rep_article["tickers"].append(ticker)

    return result

import json
import re
import time

from google import genai
from google.genai import types
from loguru import logger
from pydantic import BaseModel

from app.core.config import get_settings

settings = get_settings()
client = genai.Client(api_key=settings.gemini_api_key)

# Gemini 503(UNAVAILABLE) 일시적 과부하 재시도 설정
MAX_RETRIES = 3
RETRY_DELAY_503 = 5  # seconds, 503 고정 대기 (backoff 없음)


# 모델별 기본 generation 설정
# max_output_tokens: 반복 폭주 방지. 섹터뉴스/midterm 은 좀 더 넉넉하게.
_GEN_CONFIG_DEFAULTS: dict[str, dict] = {
    "flash-lite": {"max_output_tokens": 3000, "temperature": 0.4},
    "flash":      {"max_output_tokens": 8192, "temperature": 0.4},
}

def _make_gen_config(
    model: str,
    max_output_tokens: int | None = None,
    temperature: float | None = None,
    response_schema: type[BaseModel] | None = None,
) -> types.GenerateContentConfig:
    """모델명에서 적절한 GenerateContentConfig 를 생성한다.

    response_schema 가 주어지면 Gemini structured output(JSON) 모드로 설정한다.
    """
    key = "flash-lite" if "flash-lite" in model else "flash"
    defaults = _GEN_CONFIG_DEFAULTS[key]
    kwargs = dict(
        max_output_tokens=max_output_tokens or defaults["max_output_tokens"],
        temperature=temperature if temperature is not None else defaults["temperature"],
    )
    if response_schema is not None:
        kwargs["response_mime_type"] = "application/json"
        kwargs["response_schema"] = response_schema
    return types.GenerateContentConfig(**kwargs)


def _generate_content(
    prompt: str,
    ticker: str = "",
    model: str | None = None,
    max_output_tokens: int | None = None,
    temperature: float | None = None,
) -> str | None:
    """
    Gemini generate_content 공통 호출 + 503 재시도.

    model:             사용할 Gemini 모델명. None이면 settings.gemini_model(Flash).
    max_output_tokens: 출력 토큰 상한. None이면 모델별 기본값 사용.
                       flash-lite 기본=3000, flash 기본=8192.
    temperature:       생성 온도. None이면 모델별 기본값(0.4) 사용.
    최대 MAX_RETRIES회 시도, 재시도 간격 RETRY_DELAY초.
    재시도 모두 실패하거나 503 외 예외 발생 시 None 반환.
    반환: 생성된 텍스트(strip 적용) 또는 None.
    """
    model = model or settings.gemini_model
    gen_config = _make_gen_config(model, max_output_tokens, temperature)
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=gen_config,
            )
            return (response.text or "").strip()
        except Exception as e:
            if "503" in str(e) and attempt < MAX_RETRIES - 1:
                logger.warning(
                    f"[{ticker}] Gemini 503 — {RETRY_DELAY_503}초 후 재시도 "
                    f"({attempt + 1}/{MAX_RETRIES})"
                )
                time.sleep(RETRY_DELAY_503)
                continue
            logger.warning(f"[{ticker}] Gemini 호출 실패: {e}")
            return None


def _generate_structured(
    prompt: str,
    schema: type[BaseModel],
    ticker: str = "",
    model: str | None = None,
    max_output_tokens: int | None = None,
    temperature: float | None = None,
) -> BaseModel | None:
    """
    Gemini structured output(JSON) 호출 + 503 재시도 + 스키마 검증.

    schema:  응답을 강제할 Pydantic 모델. Gemini가 이 스키마에 맞는
             JSON만 반환하도록 response_schema 로 전달된다.
    그 외 파라미터는 _generate_content와 동일한 의미.

    반환: schema 인스턴스, 또는 호출/파싱 실패 시 None.
          (재시도 로직은 _generate_content와 동일하게 503만 재시도)
    """
    model = model or settings.gemini_model
    gen_config = _make_gen_config(
        model, max_output_tokens, temperature, response_schema=schema
    )
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=gen_config,
            )
            raw = (response.text or "").strip()
            if not raw:
                logger.warning(f"[{ticker}] Gemini 구조화 응답 비어있음")
                return None
            try:
                return schema.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    f"[{ticker}] 구조화 응답 파싱/검증 실패: {e} "
                    f"(raw 앞부분: {raw[:200]!r})"
                )
                return None
        except Exception as e:
            if "503" in str(e) and attempt < MAX_RETRIES - 1:
                logger.warning(
                    f"[{ticker}] Gemini 503 — {RETRY_DELAY_503}초 후 재시도 "
                    f"({attempt + 1}/{MAX_RETRIES})"
                )
                time.sleep(RETRY_DELAY_503)
                continue
            logger.warning(f"[{ticker}] Gemini 호출 실패: {e}")
            return None

SENTIMENT_MAP = {
    "호재 우세": "positive",
    "악재 우세": "negative",
    "혼조": "mixed",
    "중립": "neutral",
}


def parse_sentiment(summary_text: str) -> str:
    match = re.search(
        r"\[주가 영향\]\s*(호재 우세|악재 우세|혼조|중립)",
        summary_text,
    )
    if match:
        return SENTIMENT_MAP.get(match.group(1), "neutral")
    return "neutral"


MAX_INPUT_CHARS = 12000  # 위클리 입력(daily_reports 또는 raw_articles)의
                         # 누적 글자수 상한. flash-lite 출력 하드캡
                         # (~8192 토큰, ~32000자) 대비 안전 마진을 둔 값.
                         # 실험적으로 output/input ≈ 1.56× 확인 →
                         # 12000 × 1.56 ≈ 18700자 ≈ 4675 토큰으로 안전.


def _trim_to_char_budget(
    items: list[dict],
    date_key_candidates: list[str],
    text_key_candidates: list[str],
    max_chars: int = MAX_INPUT_CHARS,
) -> list[dict]:
    """items를 날짜 기준 최신순으로 정렬한 뒤, 누적 글자수가 max_chars를
    넘기기 전까지만 포함시킨다.

    건수가 아니라 글자수 기준이므로 기사가 길면 자동으로 적은 건수만,
    짧으면 더 많은 건수가 포함된다. 개별 항목 내부는 자르지 않는다.
    단, 첫 항목 하나는 max_chars를 초과해도 반드시 포함(빈 입력 방지).
    반환 순서는 오래된 것 → 최신(기존 포매터가 기대하는 방향).
    """
    def _date_key(item: dict) -> str:
        for key in date_key_candidates:
            val = item.get(key)
            if val:
                return str(val)
        return ""

    def _text_len(item: dict) -> int:
        total = 0
        for key in text_key_candidates:
            val = item.get(key)
            if val:
                total += len(str(val))
        return total

    if not items:
        return items

    sorted_desc = sorted(items, key=_date_key, reverse=True)
    kept: list[dict] = []
    total_chars = 0

    for item in sorted_desc:
        item_len = _text_len(item)
        if kept and total_chars + item_len > max_chars:
            break
        kept.append(item)
        total_chars += item_len

    if len(kept) < len(items):
        logger.info(
            f"[입력 상한] {len(items)}건 → {len(kept)}건만 사용 "
            f"(누적 {total_chars}자, 상한 {max_chars}자)"
        )

    kept_ids = {id(x) for x in kept}
    return [x for x in sorted(items, key=_date_key) if id(x) in kept_ids]


# ──────────────────────────────────────────
# news_input 빌더
# ──────────────────────────────────────────

def build_news_input(articles: list[dict]) -> str:
    blocks = []
    for idx, article in enumerate(articles, 1):
        date = (article.get("publishedDate", "") or "")[:10]
        title = article.get("title", "") or ""
        text = (article.get("text", "") or "")[:1500]
        blocks.append(
            f"[{idx}] {date}\n"
            f"제목: {title}\n"
            f"내용: {text}"
        )
    return "\n\n".join(blocks)


# ──────────────────────────────────────────
# 프롬프트 2종 (하드코딩)
# ──────────────────────────────────────────

PROMPT_SIMPLE = """You are a stock news analyst for individual retail investors in Korea.

Rules:
- ONLY use information from the provided news snippets. No outside knowledge.
- Do NOT speculate. Stick to facts in the snippets.
- Write in Korean. Plain language, no jargon.
- If a news item is clearly unrelated to this ticker, silently ignore it.
- Sentiment rules:
  * 호재: 주가에 긍정적 영향을 줄 가능성이 높은 뉴스
    - 확정된 긍정 결과 (실적 어닝 비트, 매출 성장)
    - 구체적 수치나 일정이 포함된 투자/사업 계획 발표
    - 애널리스트 목표주가 상향 또는 투자의견 업그레이드
    - 수익 전망 상향 (가이던스 상향)
    - 구체적 규모가 명시된 주요 계약 또는 파트너십 체결
    - 신제품 출시, FDA 승인 등 구체적 긍정 이벤트
  * 악재: 주가에 부정적 영향을 줄 가능성이 높은 뉴스
    - 확정된 부정 결과 (실적 미스, 매출 감소)
    - 규제 조사 착수, 소송 제기, 벌금 부과
    - 애널리스트 목표주가 하향 또는 투자의견 다운그레이드
    - 수익 전망 하향 (가이던스 하향)
    - CEO 돌연 사임, 핵심 인력 이탈
    - 리콜, 생산 중단, 제품 결함 확인
  * 중립: 영향이 불명확하거나 단순 정보성 뉴스
    - 출처 불명확한 루머 또는 단순 추측성 보도
    - 특정 종목 영향이 불명확한 섹터 전반 뉴스
    - 수치나 일정 없는 막연한 계획 발표
    - 단순 인사 발표 (맥락 없는 CEO 교체 등)
    - 시장 전반 논평 또는 거시경제 뉴스
  * 판단 기준: "확정 여부"보다 "구체성과 신뢰도"로 판단
    구체적 수치/일정/출처가 있으면 → 호재 또는 악재
    막연하거나 출처 불명확하면 → 중립
  * When in doubt → 중립
- Do not include citation numbers like ([1]), ([2]), ([3])
  in any part of the output.
- Keep each bullet point concise.
  Maximum 2 Korean sentences per bullet point.
- For [주가 영향], use exactly one of these values:
  호재 우세 / 악재 우세 / 혼조 / 중립

Output format:

[오늘 무슨 일]
- (사건 1) [호재 / 악재 / 중립]
- (사건 2) [호재 / 악재 / 중립]
- (사건 3, 없으면 생략) [호재 / 악재 / 중립]

[요약]
(전체 흐름 2-3문장, 추측 없이)

[주가 영향] 호재 우세 / 악재 우세 / 혼조 / 중립

[다음에 주목할 뉴스]
- (후속 뉴스 1)
- (후속 뉴스 2)

---
Ticker: {ticker}
News snippets:
{news_input}
---"""


PROMPT_FULL = """You are a stock news analyst for individual retail investors in Korea.

Rules:
- ONLY use information from the provided news snippets. No outside knowledge.
- Do NOT speculate. If something is unclear, say so briefly.
- Write all text values in Korean. Plain language, no jargon.
- If a news item is clearly unrelated to this ticker, silently ignore it.
- Sentiment rules:
  * 호재: 주가에 긍정적 영향을 줄 가능성이 높은 뉴스
    - 확정된 긍정 결과 (실적 어닝 비트, 매출 성장)
    - 구체적 수치나 일정이 포함된 투자/사업 계획 발표
    - 애널리스트 목표주가 상향 또는 투자의견 업그레이드
    - 수익 전망 상향 (가이던스 상향)
    - 구체적 규모가 명시된 주요 계약 또는 파트너십 체결
    - 신제품 출시, FDA 승인 등 구체적 긍정 이벤트
  * 악재: 주가에 부정적 영향을 줄 가능성이 높은 뉴스
    - 확정된 부정 결과 (실적 미스, 매출 감소)
    - 규제 조사 착수, 소송 제기, 벌금 부과
    - 애널리스트 목표주가 하향 또는 투자의견 다운그레이드
    - 수익 전망 하향 (가이던스 하향)
    - CEO 돌연 사임, 핵심 인력 이탈
    - 리콜, 생산 중단, 제품 결함 확인
  * 중립: 영향이 불명확하거나 단순 정보성 뉴스
    - 출처 불명확한 루머 또는 단순 추측성 보도
    - 특정 종목 영향이 불명확한 섹터 전반 뉴스
    - 수치나 일정 없는 막연한 계획 발표
    - 단순 인사 발표 (맥락 없는 CEO 교체 등)
    - 시장 전반 논평 또는 거시경제 뉴스
  * 판단 기준: "확정 여부"보다 "구체성과 신뢰도"로 판단
    구체적 수치/일정/출처가 있으면 → 호재 또는 악재
    막연하거나 출처 불명확하면 → 중립
  * When in doubt → 중립
- Classify ALL relevant news items into positives / negatives / neutral_items.
  neutral_items also covers sector-wide or macro news with unclear ticker-specific impact.
- temperature_gap: Find where positive and negative news collide,
  OR explain why the market is moving against the news.
  If no contradiction exists, describe the single dominant theme.
- checkpoint_section (market_reaction / checkpoint): This is NOT a "market is wrong,
  here's the truth" contrast. Both fields are simply facts placed side by side:
  market_reaction is the surface-level reason the market seems to be reacting to,
  and checkpoint is another fact worth keeping in mind alongside it
  (e.g. a risk, a detail, a longer-term factor). Do not frame either one as
  more "correct" than the other — avoid words implying the market's reaction
  is mistaken or overdone.
  If this cannot be judged from the snippets, set BOTH fields to null.
- sentiment_reason: Write as a natural flowing sentence or two, the way an analyst
  would explain it in conversation. Do NOT prefix it with a label like "근거:"
  or "이유:" — just state the reasoning directly.
- Do not include citation numbers like ([1]), ([2]), ([3])
  in any field. Never reference source numbers.
- Each bullet-like string (positives, negatives, neutral_items, next_watch)
  must be a single self-contained item, maximum 2 Korean sentences.
- If positives/negatives/neutral_items have nothing to report, return an empty list,
  NOT a string like "없음".
- sentiment must be exactly one of: positive, negative, mixed, neutral.

Respond with a single JSON object matching the required schema. No prose outside JSON.

---
Ticker: {ticker}
News snippets:
{news_input}
---"""


# ──────────────────────────────────────────
# PROMPT_FULL 구조화 출력 스키마 + 고정 템플릿 렌더링
#
# LLM은 아래 FullReportData 필드만 채우고, 헤더/순서/문구/구두점은
# render_full_report()가 고정 템플릿으로 조립한다.
# (지금까지의 [오늘의 핵심 한 줄] 등 출력 형식을 그대로 유지하므로
#  downstream — DB 저장, weekly 입력 — 은 변경 없이 호환된다.)
# ──────────────────────────────────────────

class CheckpointSection(BaseModel):
    """시장 반응과 함께 봐야 할 체크포인트.

    '시장 반응 vs 실제 상황' 같은 대비/반박 구도가 아니라,
    시장이 보고 있는 표면적 이유와 동시에 같이 챙겨봐야 할 사실을
    나란히 제시하는 섹션. 어느 쪽이 '맞다'는 판단은 하지 않는다.
    """
    market_reaction: str | None = None
    checkpoint: str | None = None


class InvestorView(BaseModel):
    short_term: str
    long_term: str


class FullReportData(BaseModel):
    headline: str
    positives: list[str] = []
    negatives: list[str] = []
    neutral_items: list[str] = []
    temperature_gap: str
    checkpoint_section: CheckpointSection | None = None
    sentiment: str  # positive / negative / mixed / neutral
    sentiment_reason: str
    investor_view: InvestorView
    next_watch: list[str] = []


_SENTIMENT_TO_LABEL = {
    "positive": "호재 우세",
    "negative": "악재 우세",
    "mixed": "혼조",
    "neutral": "중립",
}


def _dedup_keep_order(items: list[str]) -> list[str]:
    """리스트 내 완전 중복 문자열 제거(순서 유지). 공백 차이는 무시하고 비교."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def render_full_report(data: FullReportData) -> str:
    """
    FullReportData → 고정 템플릿 마크다운 텍스트.

    기존 PROMPT_FULL이 LLM에게 직접 쓰게 했던 출력 형식과
    글자 단위로 동일한 골격을 만든다. 값이 비어 있을 때의 처리
    (예: "없음")도 여기서 결정론적으로 처리한다.
    """
    positives = _dedup_keep_order(data.positives)
    negatives = _dedup_keep_order(data.negatives)
    neutral_items = _dedup_keep_order(data.neutral_items)
    next_watch = _dedup_keep_order(data.next_watch)

    lines: list[str] = []
    lines.append("[오늘의 핵심 한 줄]")
    lines.append(data.headline.strip())
    lines.append("")

    lines.append("[호재]")
    lines.extend(f"- {x}" for x in positives) if positives else lines.append("- 없음")
    lines.append("")

    lines.append("[악재 및 우려]")
    lines.extend(f"- {x}" for x in negatives) if negatives else lines.append("- 없음")
    lines.append("")

    lines.append("[중립/매크로]")
    lines.extend(f"- {x}" for x in neutral_items) if neutral_items else lines.append("- 없음")
    lines.append("")

    lines.append("[오늘의 온도차]")
    lines.append(data.temperature_gap.strip())
    lines.append("")

    cps = data.checkpoint_section
    if cps is not None and cps.market_reaction and cps.checkpoint:
        lines.append("[시장 반응과 체크포인트]")
        lines.append(f"시장 반응: {cps.market_reaction.strip()}")
        lines.append(f"체크포인트: {cps.checkpoint.strip()}")
        lines.append("")

    sentiment_label = _SENTIMENT_TO_LABEL.get(data.sentiment, "중립")
    lines.append(f"[주가 영향] {sentiment_label}")
    lines.append(data.sentiment_reason.strip())
    lines.append("")

    lines.append("[투자자 관점]")
    lines.append(f"단기: {data.investor_view.short_term.strip()}")
    lines.append(f"장기: {data.investor_view.long_term.strip()}")
    lines.append("")

    lines.append("[다음에 체크해야 할 뉴스]")
    if next_watch:
        lines.extend(f"- {x}" for x in next_watch)
    else:
        lines.append("- 없음")

    return "\n".join(lines)


# ──────────────────────────────────────────
# 주간 리포트 — 구조화 출력 스키마 + 고정 템플릿 렌더링
#
# daily(PROMPT_FULL)와 동일한 방식: LLM은 WeeklyReportData 필드만
# 채우고, 헤더/순서/불릿 형식/구두점은 render_weekly_report()가
# 고정 템플릿으로 조립한다.
#
# 카테고리는 호재/악재가 따로 쓰던 카테고리명을 5개로 통합했다.
# sentiment(positive/negative)와 category를 독립된 필드로 받으므로
# midterm 등 후속 단계에서 텍스트 정규식 없이 바로 집계할 수 있다.
# ──────────────────────────────────────────

WEEKLY_CATEGORIES = [
    "실적_재무",   # 어닝, 매출, 가이던스, 마진
    "사업_운영",   # 계약, 파트너십, 신제품, FDA, 리콜, 생산중단, 소송
    "시장평가",    # 목표주가, 투자의견, 커버리지
    "경영_인사",   # CEO/경영진 변화, 자사주매입, 배당, 구조조정
    "거시_섹터",   # 규제, 정책, 업황, 관세, 조사
]


class CategorizedItem(BaseModel):
    content: str
    category: str  # WEEKLY_CATEGORIES 중 하나


class WeeklyReportData(BaseModel):
    headline: str
    weekly_flow: str
    positives: list[CategorizedItem] = []
    positives_interpretation: str | None = None
    negatives: list[CategorizedItem] = []
    negatives_interpretation: str | None = None
    sentiment_start: str | None = None   # positive/negative/mixed/neutral, 주초
    sentiment_end: str | None = None     # positive/negative/mixed/neutral, 주말
    temperature_reason: str | None = None
    next_watch: list[str] = []
    sentiment: str            # 이번 주 종합 판단. positive/negative/mixed/neutral
    sentiment_reason: str


_CATEGORY_LABEL = {c: c.replace("_", "/") for c in WEEKLY_CATEGORIES}


def _dedup_categorized_keep_order(items: list[CategorizedItem]) -> list[CategorizedItem]:
    """content 완전 중복 제거(순서 유지, 공백 차이 무시)."""
    seen: set[str] = set()
    result: list[CategorizedItem] = []
    for item in items:
        key = item.content.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _render_categorized_section(
    header: str,
    items: list[CategorizedItem],
    interpretation: str | None,
    lines: list[str],
) -> None:
    """[호재]/[악재 및 우려] 공통 렌더링. 항목 없으면 섹션 전체 생략."""
    deduped = _dedup_categorized_keep_order(items)
    if not deduped:
        return
    lines.append(header)
    for item in deduped:
        label = _CATEGORY_LABEL.get(item.category, item.category)
        lines.append(f"• {item.content.strip()} (카테고리: {label})")
    if interpretation:
        lines.append(f"→ 해석: {interpretation.strip()}")
    lines.append("")


def render_weekly_report(data: WeeklyReportData) -> str:
    """
    WeeklyReportData → 고정 템플릿 마크다운 텍스트.

    기존 PROMPT_WEEKLY_* 가 LLM에게 직접 쓰게 했던 출력 형식과
    동일한 골격(헤더/불릿/구두점)을 유지한다. 값이 없을 때의 처리
    (섹션 생략 등)도 여기서 결정론적으로 처리한다.
    """
    next_watch = []
    seen_watch: set[str] = set()
    for x in data.next_watch:
        key = x.strip()
        if key and key not in seen_watch:
            seen_watch.add(key)
            next_watch.append(x)

    lines: list[str] = []
    lines.append("[이번 주 핵심 한 줄]")
    lines.append(data.headline.strip())
    lines.append("")

    lines.append("[주간 흐름]")
    lines.append(data.weekly_flow.strip())
    lines.append("")

    _render_categorized_section("[호재]", data.positives, data.positives_interpretation, lines)
    _render_categorized_section("[악재 및 우려]", data.negatives, data.negatives_interpretation, lines)

    if data.sentiment_start and data.sentiment_end and data.temperature_reason:
        lines.append("[주간 온도 변화]")
        lines.append(f"{data.sentiment_start} 시작 → {data.sentiment_end} 마감")
        lines.append(data.temperature_reason.strip())
        lines.append("")

    lines.append("[다음 주 체크해야 할 뉴스]")
    if next_watch:
        lines.extend(f"- {x}" for x in next_watch)
    else:
        lines.append("- 없음")
    lines.append("")

    sentiment_label = _SENTIMENT_TO_LABEL.get(data.sentiment, "중립")
    lines.append(f"[이번 주 종합 판단] {sentiment_label}")
    lines.append(data.sentiment_reason.strip())

    return "\n".join(lines)


_WEEKLY_SCHEMA_RULES = """
- weekly_flow: 최대 3문장으로 작성하세요. 핵심만 압축해서 쓰고,
  문장을 늘려 늘어놓지 마세요.
- positives/negatives 작성 시: 표현이나 출처만 다를 뿐 사실상 같은
  소식(같은 사건, 같은 발표, 같은 수치를 다르게 서술한 경우)이면
  여러 항목으로 나누지 말고 하나의 항목으로 합쳐서 작성하세요.
  (예: "A사 실적 호조 발표"와 "A사 4분기 매출 예상치 상회"가 같은
  실적 발표를 가리키면 하나로 통합)
- positives/negatives: 각 항목은 {{"content": "...", "category": "..."}} 형태.
  category는 다음 5개 중 정확히 하나여야 합니다: 실적_재무, 사업_운영, 시장평가, 경영_인사, 거시_섹터
  - 실적_재무: 어닝 비트/미스, 매출 증가/감소, 가이던스 상향/하향, 마진 변화
  - 사업_운영: 계약/파트너십 체결(금액 명시), 신제품 출시, FDA 승인, 리콜, 생산 중단, 소송
  - 시장평가: 목표주가 상향/하향, 투자의견 업/다운그레이드, 커버리지 개시
  - 경영_인사: CEO/핵심 인력 변화, 자사주 매입, 배당 변경, 구조조정
  - 거시_섹터: 규제 강화/완화, 관세, 정책, 업황, 조사 착수
- positives/negatives에 해당 내용이 없으면 빈 리스트를 반환하세요 (억지로 채우지 마세요).
- positives_interpretation/negatives_interpretation: 해당 리스트가 비어있지 않을 때만 작성.
  어느 방향에서 온 이슈인지, 단기/구조적인지, 전체 흐름에 주는 의미를 2~3문장으로.
  리스트가 비어있으면 null로 두세요.
- sentiment_start/sentiment_end/temperature_reason: 주초→주말 sentiment 변화를
  판단할 근거가 부족하면 셋 다 null로 두세요. 판단 가능하면 셋 다 채우세요.
  sentiment_start/sentiment_end는 positive/negative/mixed/neutral 중 하나.
- next_watch: 아래 두 조건을 모두 충족하는 항목만 포함하세요.
  ① 제공된 자료 본문에 명시적으로 언급된 내용
  ② 구체적인 날짜 또는 이벤트명이 있는 것
  조건 미충족 시 빈 리스트로 두세요.
- sentiment(이번 주 종합 판단)는 positive/negative/mixed/neutral 중 정확히 하나.
- "~로 알려졌다", "~할 것으로 보인다" 같은 추측성 표현 금지.
- Do not include citation numbers like ([1]), ([2]) in any field.
- sentiment_reason은 "근거:" 같은 라벨 없이 자연스러운 문장으로 쓰세요.

Respond with a single JSON object matching the required schema. No prose outside JSON.
"""


# ──────────────────────────────────────────
# 주간 리포트 프롬프트
# ──────────────────────────────────────────

PROMPT_WEEKLY_FROM_DAILIES = ("""
당신은 미국 주식 뉴스를 분석하는 전문 애널리스트입니다.
아래는 [{ticker}] 종목의 이번 주 일간 리포트 목록입니다.

=== 이번 주 일간 리포트 ===
{daily_reports}

위 일간 리포트들을 바탕으로 주간 리포트 데이터를 JSON으로 작성하세요.

───────────────────────────────
작성 규칙 (반드시 준수)
───────────────────────────────
- [언어] 입력된 뉴스나 리포트가 영어로 되어 있어도, 인용하는 모든 문장과
  숫자 설명은 반드시 한국어로 번역해서 작성하세요. 영어 원문을 그대로
  복사하거나 영어 단어를 섞어 쓰지 마세요. (인물명, 회사명, 티커 등
  고유명사는 예외)
- 모든 내용은 위에 제공된 일간 리포트에 근거해야 합니다.
- 수치(금액, %, 날짜)는 리포트에 명시된 것만 사용하세요.
- 추측이나 일반적 상식으로 내용을 채우지 마세요.
- 여러 일간 리포트에 같은 이슈가 반복되면 가장 최신/구체적인 것 하나만 사용하세요.
- weekly_flow: 주초 → 중반 → 주말 순서로 스토리 전개를 서술하세요.
  요일을 나열하는 것이 아니라 분위기와 맥락의 변화를 중심으로 작성하세요.
  예) "주초 거시 불안으로 약세 출발 → 중반 실적 대기 속 관망 → 주말 어닝 서프라이즈로 반전"
- sentiment_start/sentiment_end는 주초/주말 분위기를 반영하고,
  주말(금요일) 분위기에 좀 더 가중치를 두어 최종 sentiment를 판단하세요
  (주말 흐름이 다음 주에 영향을 주므로).
""" + _WEEKLY_SCHEMA_RULES + """
---
Ticker: {ticker}
""").strip()


PROMPT_WEEKLY_FROM_ARTICLES = ("""
당신은 미국 주식 뉴스를 분석하는 전문 애널리스트입니다.
아래는 [{ticker}] 종목의 이번 주 수집된 뉴스 목록입니다.

=== 이번 주 뉴스 ===
{articles}

위 뉴스들을 바탕으로 주간 리포트 데이터를 JSON으로 작성하세요.

───────────────────────────────
작성 규칙 (반드시 준수)
───────────────────────────────
- [언어] 입력된 뉴스나 리포트가 영어로 되어 있어도, 인용하는 모든 문장과
  숫자 설명은 반드시 한국어로 번역해서 작성하세요. 영어 원문을 그대로
  복사하거나 영어 단어를 섞어 쓰지 마세요. (인물명, 회사명, 티커 등
  고유명사는 예외)
- 모든 내용은 위에 제공된 뉴스에 근거해야 합니다.
- 수치(금액, %, 날짜)는 뉴스에 명시된 것만 사용하세요.
- 추측이나 일반적 상식으로 내용을 채우지 마세요.
- 뉴스가 적을 경우(1~3건)에도 headline과 sentiment/sentiment_reason은 반드시 작성하세요.
- weekly_flow: 뉴스 발생 순서를 바탕으로 이번 주 스토리 전개를 서술하세요.
  뉴스가 적으면 있는 내용 중심으로 간략하게 작성하세요.
- sentiment_start/sentiment_end/temperature_reason: 뉴스가 2건 미만이면 셋 다 null로 두세요.
""" + _WEEKLY_SCHEMA_RULES + """
---
Ticker: {ticker}
""").strip()


PROMPT_WEEKLY_UPDATE = ("""
당신은 미국 주식 뉴스를 분석하는 전문 애널리스트입니다.
아래는 [{ticker}] 종목의 주간 리포트 초안과 이번 주 추가된 일간 리포트입니다.

=== 월요일 작성 주간 초안 ===
{draft_report}

=== 이번 주 추가된 일간 리포트 (월~금) ===
{daily_reports}

초안을 이번 주 전체 내용으로 업데이트해서 최종 주간 리포트 데이터를 JSON으로 작성하세요.

───────────────────────────────
작성 규칙 (반드시 준수)
───────────────────────────────
- [언어] 입력된 뉴스나 리포트가 영어로 되어 있어도, 인용하는 모든 문장과
  숫자 설명은 반드시 한국어로 번역해서 작성하세요. 영어 원문을 그대로
  복사하거나 영어 단어를 섞어 쓰지 마세요. (인물명, 회사명, 티커 등
  고유명사는 예외)
- 초안의 내용을 기반으로 이번 주 일간 리포트를 통합하세요.
- 초안과 일간 리포트에 같은 이슈가 있으면 가장 최신/구체적인 것만 사용하세요.
- 모든 수치(금액, %, 날짜)는 제공된 리포트에 명시된 것만 사용하세요.
- 추측이나 일반적 상식으로 내용을 채우지 마세요.
- headline: 초안보다 이번 주 전체 흐름을 더 잘 반영하도록 업데이트하세요.
- weekly_flow: 주초 → 중반 → 주말 전체 흐름을 서술. 초안의 주초 내용 + 이번 주 추가 내용을 통합하세요.
- sentiment_start는 주초(월요일 전후), sentiment_end는 주말(금요일) 기준이며,
  금요일 마감 분위기에 가중치를 두어 최종 sentiment를 판단하세요.
""" + _WEEKLY_SCHEMA_RULES + """
---
Ticker: {ticker}
""").strip()


# ──────────────────────────────────────────
# 프롬프트 선택
# ──────────────────────────────────────────

def select_prompt(news_count: int) -> str:
    """
    뉴스 건수로 PROMPT_SIMPLE/PROMPT_FULL 중 어느 템플릿 문자열을 쓸지 결정.

    주의: PROMPT_FULL은 JSON 구조화 출력(response_schema=FullReportData)
    전용 프롬프트로 바뀌었으므로, 이 함수가 반환한 PROMPT_FULL을
    그냥 텍스트 모드(_generate_content)로 호출하면 안 된다.
    summarize_ticker / summarize_update는 이 함수를 쓰지 않고
    news_count <= 4 여부를 직접 분기해 적절한 생성 함수
    (_generate_content vs _generate_structured)를 선택한다.
    이 함수는 과거 호환/참고용으로만 남겨둔다.
    """
    if news_count <= 4:
        return PROMPT_SIMPLE
    return PROMPT_FULL


# ──────────────────────────────────────────
# 메인 요약 함수
# ──────────────────────────────────────────

_VALID_SENTIMENTS = {"positive", "negative", "mixed", "neutral"}


def summarize_ticker(ticker: str, news_list: list[dict], digest_type: str) -> dict | None:
    if not news_list:
        return {
            "ticker": ticker,
            "digest_type": digest_type,
            "summary_text": f"**{ticker}**: 해당 기간 주요 뉴스 없음.",
            "sentiment": "neutral",
            "source_urls": [],
        }

    logger.info(f"[{ticker}] {digest_type} 요약 시작 ({len(news_list)}건)")

    news_input = build_news_input(news_list)

    # 뉴스 4건 이하 → PROMPT_SIMPLE (자유 텍스트, 기존 방식 그대로)
    # 뉴스 5건 이상 → PROMPT_FULL (JSON 구조화 출력 + 고정 템플릿 렌더링)
    if len(news_list) <= 4:
        prompt = PROMPT_SIMPLE.format(ticker=ticker, news_input=news_input)
        summary_text = _generate_content(prompt, ticker, model=settings.gemini_model_lite)
        if summary_text is None:
            return None
        sentiment = parse_sentiment(summary_text)
    else:
        news_list = _trim_to_char_budget(
            news_list, ["publishedDate"], ["title", "text"],
        )
        prompt = PROMPT_FULL.format(ticker=ticker, news_input=build_news_input(news_list))
        data = _generate_structured(
            prompt, FullReportData, ticker, model=settings.gemini_model_lite,
            max_output_tokens=16000,
        )
        if data is None:
            return None
        sentiment = data.sentiment if data.sentiment in _VALID_SENTIMENTS else "neutral"
        summary_text = render_full_report(data)

    return {
        "ticker": ticker,
        "digest_type": digest_type,
        "summary_text": summary_text,
        "sentiment": sentiment,
        "source_urls": [a.get("url", "") for a in news_list[:10]],
    }


# ──────────────────────────────────────────
# Phase2: premarket 업데이트 요약
# ──────────────────────────────────────────

PROMPT_UPDATE = """You are a stock news analyst for individual retail investors in Korea.

기존에 작성된 closing 리포트와, 그 이후 밤사이 추가된 새 뉴스가 주어집니다.
새 뉴스를 반영해 기존 리포트를 업데이트한 premarket 리포트를 작성하세요.

Rules:
- 기존 리포트 내용을 기반으로 하되, 새 뉴스로 바뀐 부분을 반영/보강한다.
- ONLY use information from the existing report and the new snippets. No outside knowledge.
- Do NOT speculate. Write in Korean. Plain language, no jargon.
- Do not include citation numbers like ([1]), ([2]) in any part of the output.
- Keep each bullet point concise. Maximum 2 Korean sentences per bullet point.
- For [주가 영향], use exactly one of these values:
  호재 우세 / 악재 우세 / 혼조 / 중립
  This value must match the sentiment field:
  호재 우세 → positive / 악재 우세 → negative / 혼조 → mixed / 중립 → neutral

Output format:

[밤사이 업데이트]
- (새 뉴스로 바뀐/추가된 핵심)

[현재 상황 요약]
(기존 리포트 + 새 뉴스를 통합한 2-3문장)

[주가 영향] 호재 우세 / 악재 우세 / 혼조 / 중립
근거: (1-2문장)

[다음에 주목할 뉴스]
- (후속 뉴스 1)
- (후속 뉴스 2)

---
Ticker: {ticker}

[기존 closing 리포트]
{existing_report}

[밤사이 추가된 뉴스]
{news_input}
---"""


def summarize_update(ticker: str, existing_report, new_articles: list[dict]) -> dict | None:
    """
    기존 closing 리포트 + 밤사이 새 뉴스로 premarket 리포트 생성 (Phase2).

    existing_report 가 없으면 새 뉴스만으로 일반 요약을 생성한다.
    Gemini 호출 실패 시 None 반환.
    반환: {"summary_text", "sentiment", "source_urls"} 또는 None
    """
    source_urls = [a.get("url", "") for a in new_articles[:10]]

    if not new_articles:
        return {
            "summary_text": existing_report or f"**{ticker}**: 신규 뉴스 없음.",
            "sentiment": parse_sentiment(existing_report or ""),
            "source_urls": [],
        }

    logger.info(f"[{ticker}] premarket 업데이트 요약 시작 ({len(new_articles)}건)")

    news_input = build_news_input(new_articles)
    if existing_report:
        prompt = PROMPT_UPDATE.format(
            ticker=ticker,
            existing_report=existing_report,
            news_input=news_input,
        )
        summary_text = _generate_content(prompt, ticker, model=settings.gemini_model_lite)
        if summary_text is None:
            return None
        sentiment = parse_sentiment(summary_text)
    elif len(new_articles) <= 4:
        prompt = PROMPT_SIMPLE.format(ticker=ticker, news_input=news_input)
        summary_text = _generate_content(prompt, ticker, model=settings.gemini_model_lite)
        if summary_text is None:
            return None
        sentiment = parse_sentiment(summary_text)
    else:
        # existing_report 없음 + 새 기사 5건 이상 → PROMPT_FULL(JSON 구조화)
        prompt = PROMPT_FULL.format(ticker=ticker, news_input=news_input)
        data = _generate_structured(
            prompt, FullReportData, ticker, model=settings.gemini_model_lite,
            max_output_tokens=16000,
        )
        if data is None:
            return None
        sentiment = data.sentiment if data.sentiment in _VALID_SENTIMENTS else "neutral"
        summary_text = render_full_report(data)

    return {
        "summary_text": summary_text,
        "sentiment": sentiment,
        "source_urls": source_urls,
    }


# ──────────────────────────────────────────
# 주간 리포트: 입력 포맷 (daily → weekly)
# ──────────────────────────────────────────

def _format_daily_reports(daily_reports: list[dict]) -> str:
    """일간 리포트 리스트를 프롬프트용 텍스트 블록으로 변환."""
    blocks = []
    for idx, report in enumerate(daily_reports, 1):
        report_date = report.get("report_date") or report.get("date") or ""
        version = report.get("version") or ""
        sentiment = report.get("sentiment") or ""
        body = report.get("summary_text") or report.get("summary") or ""
        header = f"[{idx}] {report_date}"
        if version:
            header += f" ({version})"
        if sentiment:
            header += f" / sentiment={sentiment}"
        blocks.append(f"{header}\n{body}")
    return "\n\n".join(blocks)


def _dedup_daily_bullets(reports_text: str, max_dupes: int = 2) -> str:
    """
    daily 리포트 합산 텍스트 전처리 — 중복 불릿 제거.

    불릿(•, -, *) 항목 첫 5단어가 동일한 항목이 max_dupes+1개 이상이면
    처음 max_dupes개만 남기고 나머지를 제거한다.

    예: 같은 실적/계약 뉴스가 여러 일간 리포트에 반복 등장하는 경우.
    """
    lines = reports_text.split("\n")
    seen: dict[str, int] = {}   # fingerprint → count
    result: list[str] = []

    for line in lines:
        stripped = line.lstrip()
        # 불릿 라인 판별
        if stripped and stripped[0] in ("•", "-", "*") and len(stripped) > 3:
            # 첫 5단어를 소문자 fingerprint 로 사용
            words = re.split(r"\s+", stripped[1:].strip())
            fp = " ".join(w.lower() for w in words[:5])
            count = seen.get(fp, 0)
            seen[fp] = count + 1
            if count >= max_dupes:
                # 이미 max_dupes 개 기록됨 → 이 줄 스킵
                continue
        result.append(line)

    removed = len(lines) - len(result)
    if removed:
        logger.debug(f"[dedup_bullets] 중복 불릿 {removed}줄 제거")
    return "\n".join(result)


# ──────────────────────────────────────────
# 주간 리포트 생성 (월요일 초안)
# ──────────────────────────────────────────

_VALID_WEEKLY_SENTIMENTS = {"positive", "negative", "mixed", "neutral"}


def summarize_weekly(
    ticker: str,
    daily_reports: list[dict] | None = None,
    raw_articles: list[dict] | None = None,
) -> dict | None:
    """
    주간 리포트 생성.

    입력 우선순위:
    1. daily_reports 3개 이상 → PROMPT_WEEKLY_FROM_DAILIES
    2. daily_reports 1~2개    → PROMPT_WEEKLY_FROM_ARTICLES (원본 뉴스 보완)
    3. daily_reports 없음     → PROMPT_WEEKLY_FROM_ARTICLES (원본 뉴스만)
    4. 둘 다 없음             → None 반환 (스킵)

    LLM은 WeeklyReportData(JSON)만 채우고, 헤더/순서/불릿 형식은
    render_weekly_report()가 고정 템플릿으로 조립한다.

    반환: {"summary_text": ..., "sentiment": ...} 또는 None
    """
    daily_reports = daily_reports or []
    raw_articles = raw_articles or []

    daily_reports = _trim_to_char_budget(
        daily_reports, ["report_date", "date"], ["summary_text", "summary"],
    )
    raw_articles = _trim_to_char_budget(
        raw_articles, ["publishedDate"], ["title", "text"],
    )

    if len(daily_reports) >= 3:
        deduped = _dedup_daily_bullets(_format_daily_reports(daily_reports))
        prompt = PROMPT_WEEKLY_FROM_DAILIES.format(
            ticker=ticker,
            daily_reports=deduped,
        )
    elif raw_articles:
        # daily 1~2개(원본 보완) 또는 daily 0개(원본만)
        prompt = PROMPT_WEEKLY_FROM_ARTICLES.format(
            ticker=ticker,
            articles=build_news_input(raw_articles),
        )
    elif daily_reports:
        # 원본 뉴스가 없지만 일간 1~2개라도 있으면 그것으로 생성
        deduped = _dedup_daily_bullets(_format_daily_reports(daily_reports))
        prompt = PROMPT_WEEKLY_FROM_DAILIES.format(
            ticker=ticker,
            daily_reports=deduped,
        )
    else:
        return None

    logger.info(
        f"[{ticker}] weekly 요약 시작 "
        f"(daily {len(daily_reports)}건 / articles {len(raw_articles)}건)"
    )

    data = _generate_structured(
        prompt, WeeklyReportData, ticker,
        model=settings.gemini_model_lite,
        max_output_tokens=16000,
    )
    if data is None:
        return None

    sentiment = data.sentiment if data.sentiment in _VALID_WEEKLY_SENTIMENTS else "neutral"
    return {
        "summary_text": render_weekly_report(data),
        "sentiment": sentiment,
    }


# ──────────────────────────────────────────
# 주간 리포트 최종본 업데이트 (금요일)
# ──────────────────────────────────────────

def summarize_weekly_update(
    ticker: str,
    draft_report: str,
    daily_reports: list[dict],
) -> dict | None:
    """
    금요일 최종본 업데이트 — 월요일 초안 + 이번 주 일간 리포트로 갱신.
    PROMPT_WEEKLY_UPDATE 사용.

    반환: {"summary_text": ..., "sentiment": ...} 또는 None
    """
    daily_reports = daily_reports or []
    if not draft_report and not daily_reports:
        return None

    daily_reports = _trim_to_char_budget(
        daily_reports, ["report_date", "date"], ["summary_text", "summary"],
    )
    deduped_daily = _dedup_daily_bullets(_format_daily_reports(daily_reports))
    prompt = PROMPT_WEEKLY_UPDATE.format(
        ticker=ticker,
        draft_report=draft_report or "(초안 없음)",
        daily_reports=deduped_daily,
    )

    logger.info(
        f"[{ticker}] weekly 최종본 업데이트 시작 (daily {len(daily_reports)}건)"
    )

    data = _generate_structured(
        prompt, WeeklyReportData, ticker,
        model=settings.gemini_model_lite,
        max_output_tokens=16000,
    )
    if data is None:
        return None

    sentiment = data.sentiment if data.sentiment in _VALID_WEEKLY_SENTIMENTS else "neutral"
    return {
        "summary_text": render_weekly_report(data),
        "sentiment": sentiment,
    }


# ──────────────────────────────────────────
# 섹터별 주간 시장 뉴스 리포트
# ──────────────────────────────────────────

SECTOR_CATEGORIES = [
    "Basic Materials", "Communication Services", "Consumer Cyclical",
    "Consumer Defensive", "Energy", "Financial Services", "Healthcare",
    "Industrials", "Real Estate", "Technology", "Utilities", "MACRO",
]


PROMPT_SECTOR_NEWS_WEEKLY = """
당신은 미국 주식시장을 분석하는 전문 애널리스트입니다.
아래는 이번 주(월~금) 수집된 일반 시장 뉴스 목록입니다.

=== 이번 주 시장 뉴스 ===
{articles}

위 뉴스들을 아래 12개 카테고리로 분류하여 각각 요약하세요.
(11개 섹터 + MACRO: 연준/금리/인플레이션/지정학 등 시장 전반 이슈)

카테고리: Basic Materials, Communication Services, Consumer Cyclical,
Consumer Defensive, Energy, Financial Services, Healthcare, Industrials,
Real Estate, Technology, Utilities, MACRO

───────────────────────────────
작성 규칙
───────────────────────────────
1. 해당 주에 관련 뉴스가 없는 카테고리는 통째로 생략하세요. 억지로 채우지 마세요.
2. 모든 내용은 제공된 뉴스에 근거해야 합니다. 추측 금지.
3. 각 카테고리는 아래 형식으로 작성:

## [카테고리명]
핵심 테마: (한 줄)
주요 이슈:
- (내용 1)
- (내용 2)
시장 해석: (1~2문장, 이 흐름이 해당 섹터/시장에 주는 의미)
sentiment: positive | negative | mixed | neutral

4. sentiment 기준:
   - positive: 섹터/시장에 긍정적 뉴스가 우세
   - negative: 부정적 뉴스가 우세
   - mixed: 호재/악재 혼재
   - neutral: 방향성 뉴스 없음 (정보성 뉴스만)

5. 카테고리명은 위 영문 표기 그대로 사용하세요 (예: ## Technology, ## MACRO).
"""


def _normalize_category(raw: str) -> str | None:
    """헤더 텍스트에서 표준 카테고리명 추출. 매칭 실패 시 None."""
    c = (raw or "").strip().strip("[]").strip()
    for cat in SECTOR_CATEGORIES:
        if c.lower() == cat.lower():
            return cat
    return None


def parse_sector_sentiment(section: str) -> str:
    """섹션 텍스트에서 sentiment 값 추출. 못 찾으면 neutral."""
    m = re.search(
        r"sentiment\s*[:：]\s*(positive|negative|mixed|neutral)",
        section, re.IGNORECASE,
    )
    return m.group(1).lower() if m else "neutral"


# ──────────────────────────────────────────
# 중장기(Midterm) 리포트 — 최근 12주 집계
# ──────────────────────────────────────────

# Midterm 구조화 출력 스키마 + 고정 템플릿 렌더링
# daily(FullReportData)/weekly(WeeklyReportData)와 동일한 방식.

# ── Part A: 뉴스 기반 스키마 ──────────────────────────────────
class MidtermPartAData(BaseModel):
    headline: str
    flow_narrative: str
    trend_items: list[CategorizedItem] = []
    trend_interpretation: str | None = None


# ── Part B: 수치/판단 기반 스키마 ────────────────────────────
class MidtermPartBData(BaseModel):
    benchmark_interpretation: str
    sector_comparison: str | None = None
    macro_analysis: str | None = None
    sentiment: str            # positive/negative/mixed/neutral
    sentiment_reason: str


def render_midterm_part_a(data: MidtermPartAData) -> str:
    """MidtermPartAData → 뉴스 기반 섹션 텍스트."""
    lines: list[str] = []
    lines.append("[중장기 핵심 뉴스 한 줄]")
    lines.append(data.headline.strip())
    lines.append("")

    lines.append("[중장기 뉴스 흐름]")
    lines.append(data.flow_narrative.strip())
    lines.append("")

    deduped = _dedup_categorized_keep_order(data.trend_items)
    if deduped:
        lines.append("[호재/악재 추세]")
        for item in deduped:
            label = _CATEGORY_LABEL.get(item.category, item.category)
            lines.append(f"• {item.content.strip()} (카테고리: {label})")
        if data.trend_interpretation:
            lines.append(f"→ 해석: {data.trend_interpretation.strip()}")
        lines.append("")

    return "\n".join(lines).strip()


def render_midterm_part_b(
    data: MidtermPartBData | None,
    sector_name: str,
    exchange: str,
    stock_cumulative: float,
    sp500_cumulative: float,
    sector_cumulative: float,
    alpha_vs_market: float,
    alpha_vs_sector: float,
) -> str:
    """
    MidtermPartBData → 수치/판단 섹션 텍스트.
    [누적 성과 vs 벤치마크] 수치 줄은 코드가 직접 삽입.
    """
    lines: list[str] = []
    lines.append("[누적 성과 vs 벤치마크]")
    lines.append(f"이 종목 누적 수익률: {stock_cumulative:+.2f}%")
    lines.append(f"S&P500 누적 수익률: {sp500_cumulative:+.2f}%")
    lines.append(f"{sector_name} 섹터({exchange}) 누적 수익률: {sector_cumulative:+.2f}%")
    lines.append(f"시장 대비 alpha: {alpha_vs_market:+.2f}%p")
    lines.append(f"섹터 대비 alpha: {alpha_vs_sector:+.2f}%p")
    lines.append("")

    if data is not None:
        lines.append(data.benchmark_interpretation.strip())
        lines.append("")

        if data.sector_comparison:
            lines.append("[종목 vs 섹터 흐름 비교]")
            lines.append(data.sector_comparison.strip())
            lines.append("")

        if data.macro_analysis:
            lines.append("[거시환경 분석]")
            lines.append(data.macro_analysis.strip())
            lines.append("")

        sentiment_label = _SENTIMENT_TO_LABEL.get(data.sentiment, "중립")
        lines.append(f"[중장기 종합 판단] {sentiment_label}")
        lines.append(data.sentiment_reason.strip())

    return "\n".join(lines)


_MIDTERM_PART_A_SCHEMA_RULES = """
- headline: 전체 기간을 관통하는 핵심 한 줄 (티커명 + 주제). 50자 이내.
- flow_narrative: 주요 변곡점 중심 서술. 단순 나열 금지. 최대 4~5문장으로 압축.
  (예: "1~3주차 실적 기대감 → 4주차 발표 후 반전 → 5~6주차 안정")
- trend_items: 여러 주에 걸쳐 반복/심화된 이슈 목록. 비슷한 이슈는 대표 1개로 통합.
  각 항목은 {{"content": "...", "category": "..."}} 형태.
  category는 다음 5개 중 정확히 하나:
    실적_재무: 어닝, 매출, 가이던스, 마진 변화
    사업_운영: 계약/파트너십, 신제품, FDA, 리콜, 생산 중단, 소송
    시장평가: 목표주가, 투자의견, 커버리지
    경영_인사: CEO/핵심 인력, 자사주 매입, 배당, 구조조정
    거시_섹터: 규제, 정책, 업황, 관세, 조사
  해당 내용이 없으면 빈 리스트 [] (억지로 채우지 마세요).
- trend_interpretation: trend_items ≥ 1일 때만 작성. 없으면 null.
  반복/증가/감소한 이슈를 2~3문장으로 요약.
- "~로 알려졌다", "~할 것으로 보인다" 추측 표현 금지.
- Do not include citation numbers like ([1]), ([2]) in any field.

Respond with a single JSON object matching the required schema. No prose outside JSON.
"""

_MIDTERM_PART_B_SCHEMA_RULES = """
- benchmark_interpretation: 수치 성과 해석 문단 (2~3문장).
  숫자(%/%p)를 이 필드에 직접 적지 마세요 — 시스템이 자동 삽입합니다.
  사전 계산값은 해석할 때만 참고하세요.
- sector_comparison: 종목과 섹터의 동조/이탈 관계를 한 문단으로 진단.
  날짜/주차별 나열 금지. "과거에는 ~했지만 최근에는" 시간 흐름 전개 금지.
  전체 기간을 한 덩어리로 보고 관계를 짧게 진단하세요.
  섹터 비교 자료가 없으면 null.
- macro_analysis: 섹터 뉴스 기반 거시환경 분석, 2~3문장. 섹터 뉴스 없으면 null.
- sentiment: positive/negative/mixed/neutral 중 정확히 하나.
- sentiment_reason: alpha + 뉴스 흐름 종합 한 줄. "근거:" 라벨 없이 자연스럽게.
- 입력된 섹터 뉴스가 영어여도 한국어로 번역 (고유명사 제외).
- "~로 알려졌다", "~할 것으로 보인다" 추측 표현 금지.
- Do not include citation numbers like ([1]), ([2]) in any field.

Respond with a single JSON object matching the required schema. No prose outside JSON.
"""

PROMPT_MIDTERM_PART_A = ("""
당신은 미국 주식 뉴스를 분석하는 전문 애널리스트입니다.
아래는 [{ticker}] 종목의 최근 {week_count}주간 주간 리포트입니다.

=== 주간 리포트 시퀀스 (과거 → 최근) ===
{weekly_reports}

위 뉴스 흐름을 바탕으로 파트 A (뉴스 기반) 데이터를 JSON으로 작성하세요.

───────────────────────────────
작성 규칙 (반드시 준수)
───────────────────────────────
- [언어] 입력된 뉴스나 리포트가 영어로 되어 있어도, 모든 문장은 반드시
  한국어로 번역해서 작성하세요. (인물명, 회사명, 티커 등 고유명사는 예외)
- 모든 내용은 제공된 주간 리포트에 근거해야 합니다. 추측 금지.
""" + _MIDTERM_PART_A_SCHEMA_RULES + """
---
Ticker: {ticker}
""").strip()

PROMPT_MIDTERM_PART_B = ("""
당신은 미국 주식 투자 분석가입니다.
[{ticker}] 종목의 중장기 성과 해석 및 종합 판단을 작성하세요.

=== 누적 성과 (참고용 — 해석에만 활용, 출력 필드에 숫자 직접 적지 마세요) ===
이 종목 누적 수익률: {stock_cumulative}%
S&P500 누적 수익률: {sp500_cumulative}%
{sector_name} 섹터({exchange}) 누적 수익률: {sector_cumulative}%
시장 대비 alpha: {alpha_vs_market}%p
섹터 대비 alpha: {alpha_vs_sector}%p

=== 같은 기간 {sector_name} 섹터 주간 뉴스 ===
{sector_news}

위 수치와 섹터 뉴스를 바탕으로 파트 B (수치/판단 기반) 데이터를 JSON으로 작성하세요.

───────────────────────────────
작성 규칙 (반드시 준수)
───────────────────────────────
- [언어] 입력된 섹터 뉴스가 영어여도 한국어로 번역 (고유명사 제외).
- benchmark_interpretation에 %/%p 숫자 직접 적지 마세요 (시스템이 자동 삽입).
- "~로 알려졌다", "~할 것으로 보인다" 추측 표현 금지.
""" + _MIDTERM_PART_B_SCHEMA_RULES + """
---
Ticker: {ticker}
""").strip()


def _calc_cumulative(series: list) -> float:
    """None을 제외한 수익률 시퀀스를 복리 합성. 반환: % 단위."""
    factor = 1.0
    for x in series:
        if x is not None:
            factor *= (1 + x / 100)
    return (factor - 1) * 100


def _format_weekly_reports_for_midterm(weekly_reports: list[dict]) -> str:
    """weekly_reports 목록을 프롬프트용 텍스트 블록으로 변환."""
    blocks = []
    for r in weekly_reports:
        wm = r.get("week_monday", "")
        sentiment = r.get("sentiment") or "N/A"
        pct = r.get("price_change_pct")
        pct_str = f"{pct:+.2f}%" if pct is not None else "N/A"
        body = r.get("summary_text") or ""
        blocks.append(
            f"=== {wm} 주간 (sentiment={sentiment}, 주간수익률={pct_str}) ===\n{body}"
        )
    return "\n\n".join(blocks)


def _format_sector_news_for_midterm(sector_news: list[dict]) -> str:
    """섹터 뉴스 요약 목록을 프롬프트용 텍스트 블록으로 변환."""
    if not sector_news:
        return "(섹터 뉴스 없음)"
    blocks = []
    for item in sector_news:
        wm = item.get("week_monday", "")
        sentiment = item.get("sentiment") or "N/A"
        body = item.get("summary_text") or ""
        blocks.append(f"--- {wm} [{sentiment}] ---\n{body}")
    return "\n\n".join(blocks)


def generate_midterm_part_a(
    ticker: str,
    weekly_reports: list[dict],
) -> str | None:
    """
    뉴스 기반 파트 A 생성 (weekly_reports >= 2일 때만).
    반환: 렌더링된 텍스트 또는 None.
    """
    if len(weekly_reports) < 2:
        return None

    week_count = len(weekly_reports)
    prompt = PROMPT_MIDTERM_PART_A.format(
        ticker=ticker,
        week_count=week_count,
        weekly_reports=_format_weekly_reports_for_midterm(weekly_reports),
    )
    logger.info(f"[{ticker}] midterm Part A 생성 ({week_count}주 기반)")
    data = _generate_structured(
        prompt, MidtermPartAData, ticker,
        max_output_tokens=8000,
        model=settings.gemini_model_lite,
    )
    if data is None:
        return None
    return render_midterm_part_a(data)


def _generate_part_b_data(
    ticker: str,
    stock_cumulative: float,
    sp500_cumulative: float,
    sector_cumulative: float,
    alpha_vs_market: float,
    alpha_vs_sector: float,
    sector_name: str,
    exchange: str,
    sector_news: list[dict],
) -> MidtermPartBData | None:
    """파트 B LLM 호출 → 구조화 데이터 반환 (내부용)."""
    prompt = PROMPT_MIDTERM_PART_B.format(
        ticker=ticker,
        stock_cumulative=f"{stock_cumulative:+.2f}",
        sp500_cumulative=f"{sp500_cumulative:+.2f}",
        sector_name=sector_name,
        exchange=exchange,
        sector_cumulative=f"{sector_cumulative:+.2f}",
        alpha_vs_market=f"{alpha_vs_market:+.2f}",
        alpha_vs_sector=f"{alpha_vs_sector:+.2f}",
        sector_news=_format_sector_news_for_midterm(sector_news),
    )
    logger.info(f"[{ticker}] midterm Part B 생성")
    return _generate_structured(
        prompt, MidtermPartBData, ticker,
        max_output_tokens=4000,
        model=settings.gemini_model_lite,
    )


def generate_midterm_part_b(
    ticker: str,
    stock_cumulative: float,
    sp500_cumulative: float,
    sector_cumulative: float,
    alpha_vs_market: float,
    alpha_vs_sector: float,
    sector_name: str,
    exchange: str,
    sector_news: list[dict],
) -> str:
    """
    수치/판단 기반 파트 B 텍스트 반환 (항상 반환).
    LLM 실패 시 수치 헤더만 포함한 폴백 텍스트.
    """
    data = _generate_part_b_data(
        ticker, stock_cumulative, sp500_cumulative, sector_cumulative,
        alpha_vs_market, alpha_vs_sector, sector_name, exchange, sector_news,
    )
    return render_midterm_part_b(
        data, sector_name, exchange,
        stock_cumulative, sp500_cumulative, sector_cumulative,
        alpha_vs_market, alpha_vs_sector,
    )


def summarize_midterm(
    ticker: str,
    weekly_reports: list[dict],
    sp500_series: list,
    sector_series: list,
    sector_name: str,
    exchange: str,
    sector_news: list[dict],
) -> dict | None:
    """
    중장기 리포트 생성.

    - weekly_reports 0개 → None
    - weekly_reports 1개 → Part A 없음, Part B만
    - weekly_reports 2개 이상 → Part A + Part B

    반환: {"summary_text", "sentiment" (nullable), "price_change_pct"}
    """
    if not weekly_reports:
        return None

    stock_series = [r.get("price_change_pct") for r in weekly_reports]
    stock_cumulative = _calc_cumulative(stock_series)
    sp500_cumulative = _calc_cumulative(sp500_series)
    sector_cumulative = _calc_cumulative(sector_series)
    alpha_vs_market = stock_cumulative - sp500_cumulative
    alpha_vs_sector = stock_cumulative - sector_cumulative

    # Part A: 뉴스 기반 (2주 이상일 때만)
    part_a_text = (
        generate_midterm_part_a(ticker, weekly_reports)
        if len(weekly_reports) >= 2 else None
    )

    # Part B: 수치/판단 기반 (항상)
    part_b_data = _generate_part_b_data(
        ticker, stock_cumulative, sp500_cumulative, sector_cumulative,
        alpha_vs_market, alpha_vs_sector, sector_name, exchange, sector_news,
    )
    part_b_text = render_midterm_part_b(
        part_b_data, sector_name, exchange,
        stock_cumulative, sp500_cumulative, sector_cumulative,
        alpha_vs_market, alpha_vs_sector,
    )

    parts = [p for p in [part_a_text, part_b_text] if p]
    summary_text = "\n\n".join(parts)

    sentiment = None
    if part_b_data is not None:
        sentiment = (
            part_b_data.sentiment
            if part_b_data.sentiment in _VALID_WEEKLY_SENTIMENTS
            else "neutral"
        )

    return {
        "summary_text": summary_text,
        "sentiment": sentiment,
        "price_change_pct": stock_cumulative,
    }


def summarize_sector_news(articles: list[dict]) -> dict[str, dict] | None:
    """
    일반 시장 뉴스 → 12개 카테고리별 주간 요약 (1회 LLM 호출).

    반환: {category: {"summary_text": ..., "sentiment": ...}, ...}
          관련 뉴스가 없어 생략된 카테고리는 결과에서 제외.
          기사 없음/LLM 실패 시 None.
    """
    if not articles:
        return None

    prompt = PROMPT_SECTOR_NEWS_WEEKLY.format(articles=build_news_input(articles))
    logger.info(f"[SECTOR-NEWS] 섹터별 주간 요약 시작 (기사 {len(articles)}건)")

    summary_text = _generate_content(prompt, "SECTOR", model=settings.gemini_model_lite)
    if summary_text is None:
        return None

    # "## " 헤더 기준으로 섹션 분리 (첫 조각은 헤더 이전 서문)
    parts = re.split(r"(?m)^##\s+", summary_text)
    result: dict[str, dict] = {}
    for part in parts:
        if not part.strip():
            continue
        head, _, _body = part.partition("\n")
        category = _normalize_category(head)
        if not category:
            continue
        section_text = f"## {part.strip()}"
        result[category] = {
            "summary_text": section_text,
            "sentiment": parse_sector_sentiment(part),
        }

    logger.info(f"[SECTOR-NEWS] 생성된 카테고리 {len(result)}개: {sorted(result)}")
    return result or None


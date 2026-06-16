import re
import time

from google import genai
from google.genai import types
from loguru import logger

from app.core.config import get_settings

settings = get_settings()
client = genai.Client(api_key=settings.gemini_api_key)

# Gemini 503(UNAVAILABLE) 일시적 과부하 재시도 설정
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


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
) -> types.GenerateContentConfig:
    """모델명에서 적절한 GenerateContentConfig 를 생성한다."""
    key = "flash-lite" if "flash-lite" in model else "flash"
    defaults = _GEN_CONFIG_DEFAULTS[key]
    return types.GenerateContentConfig(
        max_output_tokens=max_output_tokens or defaults["max_output_tokens"],
        temperature=temperature if temperature is not None else defaults["temperature"],
    )


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
                    f"[{ticker}] Gemini 503 — {RETRY_DELAY}초 후 재시도 "
                    f"({attempt + 1}/{MAX_RETRIES})"
                )
                time.sleep(RETRY_DELAY)
                continue
            logger.warning(f"[{ticker}] Gemini 호출 실패: {e}")
            return None

SENTIMENT_MAP = {
    "호재 우세": "bullish",
    "악재 우세": "bearish",
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
- Classify ALL relevant news items.
- [오늘의 온도차]: Find where positive and negative news collide,
  OR explain why the market is moving against the news.
  If no contradiction exists, describe the single dominant theme.
- [시장 반응 vs 실제 상황]: What is the market reacting to right now?
  What does the news actually show that the market may not be pricing in?
  If unclear from snippets, omit this section entirely.
- Do not include citation numbers like ([1]), ([2]), ([3])
  in any part of the output. Never reference source numbers.
- Keep each bullet point concise.
  Maximum 2 Korean sentences per bullet point.
- For [주가 영향], use exactly one of these values:
  호재 우세 / 악재 우세 / 혼조 / 중립
  This value must match the sentiment field:
  호재 우세 → bullish
  악재 우세 → bearish
  혼조      → mixed
  중립      → neutral

Output format:

[오늘의 핵심 한 줄]
(오늘 이 종목을 한 문장으로)

[호재]
- (없으면 "없음")

[악재 및 우려]
- (없으면 "없음")

[중립/섹터]
- (없으면 "없음")

[오늘의 온도차]
(호재와 악재가 충돌하는 지점, 또는 시장이 뉴스와 반대로 움직이는 이유.
 충돌이 없으면 오늘 뉴스의 지배적 흐름 1-2줄)

[시장 반응 vs 실제 상황]
시장 반응: (1문장)
실제 상황: (1문장)
(판단 불가 시 섹션 전체 생략)

[주가 영향] 호재 우세 / 악재 우세 / 혼조 / 중립
근거: (1-2문장, 스니펫 기반)

[투자자 관점]
단기: (1문장)
장기: (1문장)

[다음에 주목할 뉴스]
- (후속 뉴스 1)
- (후속 뉴스 2)
- (후속 뉴스 3)

---
Ticker: {ticker}
News snippets:
{news_input}
---"""


# ──────────────────────────────────────────
# 주간 리포트 프롬프트
# ──────────────────────────────────────────

PROMPT_WEEKLY_FROM_DAILIES = """
당신은 미국 주식 뉴스를 분석하는 전문 애널리스트입니다.
아래는 [{ticker}] 종목의 이번 주 일간 리포트 목록입니다.

=== 이번 주 일간 리포트 ===
{daily_reports}

위 일간 리포트들을 바탕으로 주간 리포트를 작성하세요.

───────────────────────────────
작성 규칙 (반드시 준수)
───────────────────────────────
1. 모든 내용은 위에 제공된 일간 리포트에 근거해야 합니다.
2. 수치(금액, %, 날짜)는 리포트에 명시된 것만 사용하세요.
3. 추측이나 일반적 상식으로 내용을 채우지 마세요.
4. 호재/악재가 없으면 해당 섹션을 생략하세요. 억지로 채우지 마세요.
5. 여러 일간 리포트에 같은 이슈가 반복되면 가장 최신/구체적인 것 하나만 사용하세요.
6. "~로 알려졌다", "~할 것으로 보인다" 같은 추측성 표현을 쓰지 마세요.
7. [다음 주 주목할 뉴스] 섹션은 아래 3가지를 모두 충족할 때만 작성하세요:
   ① 일간 리포트 본문에 명시적으로 언급된 내용
   ② 구체적인 날짜 또는 이벤트명이 있는 것
   ③ 위 두 조건 미충족 시 섹션 전체를 생략하세요.

───────────────────────────────
출력 형식 (마크다운, 한국어)
───────────────────────────────

[이번 주 핵심 한 줄]
한 주 전체를 관통하는 가장 중요한 이슈를 1문장으로 작성.
월간/연간 리포트에서 이 주를 한 줄로 요약할 때 사용됩니다.

[주간 흐름]
주초 → 중반 → 주말 순서로 스토리 전개를 서술하세요.
요일을 나열하는 것이 아니라 분위기와 맥락의 변화를 중심으로 작성하세요.
예) "주초 거시 불안으로 약세 출발 → 중반 실적 대기 속 관망 → 주말 어닝 서프라이즈로 반전"

[호재]
※ 아래 카테고리 중 해당하는 것만 작성. 없으면 섹션 전체 생략.

카테고리 기준:
- 실적/재무: 어닝 비트, 매출 성장, 가이던스 상향, 수익 개선
- 사업/계약: 계약 체결(금액 명시), 파트너십, 신제품 출시, FDA 승인
- 시장평가: 목표주가 상향, 투자의견 업그레이드, 커버리지 개시(Buy)
- 경영/전략: 자사주 매입, 배당 증가, 구조조정 효과
- 거시/섹터: 규제 완화, 업황 개선, 정책 수혜

형식:
• [내용] (카테고리: OOO)
• [내용] (카테고리: OOO)

→ 해석: 어느 방향에서 온 호재인지, 한 방향이면 그 의미를,
        여러 방향이면 각각의 의미와 전체 흐름을 2~3문장으로 설명하세요.

[악재 및 우려]
※ 아래 카테고리 중 해당하는 것만 작성. 없으면 섹션 전체 생략.

카테고리 기준:
- 실적/재무: 어닝 미스, 매출 감소, 가이던스 하향, 수익 악화
- 사업/운영: 리콜, 생산 중단, 계약 해지, 제품 결함, 소송 제기
- 시장평가: 목표주가 하향, 투자의견 다운그레이드
- 경영/인사: CEO 돌연 사임, 핵심 인력 이탈
- 거시/규제: 규제 강화, 관세, 조사 착수, 벌금 부과

형식:
• [내용] (카테고리: OOO)
• [내용] (카테고리: OOO)

→ 해석: 어느 방향에서 온 악재인지, 단기적 이슈인지 구조적 문제인지,
        여러 방향이면 각각의 의미와 전체적인 리스크 수준을 2~3문장으로 설명하세요.

[주간 온도 변화]
주초 sentiment → 주말 sentiment 변화를 한 줄로 표현한 후,
그 변화의 원인과 의미를 2~3문장으로 설명하세요.
예) "bearish 시작 → bullish 마감: 관세 우려로 약세 출발했으나 예상을 상회한
    실적 발표가 분위기를 반전시켰으며, 애널리스트 목표주가 상향이 이를 뒷받침."

[다음 주 주목할 뉴스]
※ 작성 조건: 일간 리포트 본문에 명시적으로 언급 + 구체적 날짜/이벤트명 있을 때만 작성.
  두 조건 중 하나라도 미충족 시 이 섹션 전체를 생략하세요.

[이번 주 종합 판단]
주간 sentiment: 호재 우세 → bullish / 악재 우세 → bearish / 그 외 → neutral
근거: 판단의 핵심 이유를 한 줄로.
※ 주말 분위기에 가중치를 두세요 (주말 흐름이 다음 주에 영향을 주므로).
"""


PROMPT_WEEKLY_FROM_ARTICLES = """
당신은 미국 주식 뉴스를 분석하는 전문 애널리스트입니다.
아래는 [{ticker}] 종목의 이번 주 수집된 뉴스 목록입니다.

=== 이번 주 뉴스 ===
{articles}

위 뉴스들을 바탕으로 주간 리포트를 작성하세요.

───────────────────────────────
작성 규칙 (반드시 준수)
───────────────────────────────
1. 모든 내용은 위에 제공된 뉴스에 근거해야 합니다.
2. 수치(금액, %, 날짜)는 뉴스에 명시된 것만 사용하세요.
3. 추측이나 일반적 상식으로 내용을 채우지 마세요.
4. 호재/악재가 없으면 해당 섹션을 생략하세요. 억지로 채우지 마세요.
5. 뉴스가 적을 경우(1~3건) 있는 내용만 작성하고,
   [이번 주 핵심 한 줄]과 [이번 주 종합 판단]은 반드시 포함하세요.
6. "~로 알려졌다", "~할 것으로 보인다" 같은 추측성 표현을 쓰지 마세요.
7. [다음 주 주목할 뉴스] 섹션은 아래 3가지를 모두 충족할 때만 작성하세요:
   ① 뉴스 본문에 명시적으로 언급된 내용
   ② 구체적인 날짜 또는 이벤트명이 있는 것
   ③ 위 두 조건 미충족 시 섹션 전체를 생략하세요.

───────────────────────────────
출력 형식 (마크다운, 한국어)
───────────────────────────────

[이번 주 핵심 한 줄]
이번 주 가장 중요한 이슈를 1문장으로 작성.

[주간 흐름]
뉴스 발생 순서를 바탕으로 이번 주 스토리 전개를 서술하세요.
뉴스가 적으면 있는 내용 중심으로 간략하게 작성하세요.

[호재]
※ 없으면 섹션 전체 생략.

카테고리 기준:
- 실적/재무: 어닝 비트, 매출 성장, 가이던스 상향
- 사업/계약: 계약 체결(금액 명시), 파트너십, 신제품 출시, FDA 승인
- 시장평가: 목표주가 상향, 투자의견 업그레이드
- 경영/전략: 자사주 매입, 배당 증가
- 거시/섹터: 규제 완화, 업황 개선

형식:
• [내용] (카테고리: OOO)

→ 해석: 어느 방향에서 온 호재인지 2~3문장으로 설명.

[악재 및 우려]
※ 없으면 섹션 전체 생략.

카테고리 기준:
- 실적/재무: 어닝 미스, 매출 감소, 가이던스 하향
- 사업/운영: 리콜, 생산 중단, 소송, 제품 결함
- 시장평가: 목표주가 하향, 투자의견 다운그레이드
- 경영/인사: CEO 사임, 핵심 인력 이탈
- 거시/규제: 규제 강화, 조사 착수, 벌금

형식:
• [내용] (카테고리: OOO)

→ 해석: 어느 방향에서 온 악재인지, 단기/구조적 문제인지 2~3문장으로 설명.

[주간 온도 변화]
※ 뉴스가 2건 이상일 때만 작성. 1건이면 생략.
이번 주 분위기 변화를 한 줄로 표현 후 2~3문장 설명.

[다음 주 주목할 뉴스]
※ 뉴스 본문에 명시 + 구체적 날짜/이벤트명 있을 때만 작성.
  조건 미충족 시 섹션 전체 생략.

[이번 주 종합 판단]
주간 sentiment: 호재 우세 → bullish / 악재 우세 → bearish / 그 외 → neutral
근거: 판단의 핵심 이유를 한 줄로.
"""


PROMPT_WEEKLY_UPDATE = """
당신은 미국 주식 뉴스를 분석하는 전문 애널리스트입니다.
아래는 [{ticker}] 종목의 주간 리포트 초안과 이번 주 추가된 일간 리포트입니다.

=== 월요일 작성 주간 초안 ===
{draft_report}

=== 이번 주 추가된 일간 리포트 (월~금) ===
{daily_reports}

초안을 이번 주 전체 내용으로 업데이트해서 최종 주간 리포트를 작성하세요.

───────────────────────────────
작성 규칙 (반드시 준수)
───────────────────────────────
1. 초안의 내용을 기반으로 이번 주 일간 리포트를 통합하세요.
2. 초안과 일간 리포트에 같은 이슈가 있으면 가장 최신/구체적인 것만 사용하세요.
3. 모든 수치(금액, %, 날짜)는 제공된 리포트에 명시된 것만 사용하세요.
4. 추측이나 일반적 상식으로 내용을 채우지 마세요.
5. 호재/악재가 없으면 해당 섹션을 생략하세요.
6. [다음 주 주목할 뉴스]는 리포트 본문에 명시 + 구체적 날짜/이벤트명 있을 때만 작성.
7. 주간 sentiment는 주말(금요일) 분위기에 가중치를 두세요.

───────────────────────────────
출력 형식 (마크다운, 한국어)
───────────────────────────────

[이번 주 핵심 한 줄]
한 주 전체를 관통하는 가장 중요한 이슈를 1문장으로.
초안보다 이번 주 전체 흐름을 더 잘 반영하도록 업데이트하세요.

[주간 흐름]
주초 → 중반 → 주말 전체 흐름을 서술.
초안의 주초 내용 + 이번 주 추가 내용을 통합하세요.

[호재]
※ 없으면 섹션 전체 생략.
• [내용] (카테고리: OOO)
→ 해석: 2~3문장

[악재 및 우려]
※ 없으면 섹션 전체 생략.
• [내용] (카테고리: OOO)
→ 해석: 2~3문장

[주간 온도 변화]
주초(월요일 전후) → 주말(금요일) sentiment 변화와 그 원인.

[다음 주 주목할 뉴스]
※ 리포트 본문에 명시 + 구체적 날짜/이벤트명 있을 때만 작성.
  조건 미충족 시 섹션 전체 생략.

[이번 주 종합 판단]
주간 sentiment: bullish / bearish / neutral
근거: 판단의 핵심 이유를 한 줄로.
※ 금요일 마감 분위기에 가중치.
"""


# ──────────────────────────────────────────
# 프롬프트 선택
# ──────────────────────────────────────────

def select_prompt(news_count: int) -> str:
    if news_count <= 4:
        return PROMPT_SIMPLE
    return PROMPT_FULL


# ──────────────────────────────────────────
# 메인 요약 함수
# ──────────────────────────────────────────

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
    prompt = select_prompt(len(news_list)).format(ticker=ticker, news_input=news_input)

    summary_text = _generate_content(prompt, ticker, model=settings.gemini_model_lite)
    if summary_text is None:
        return None

    sentiment = parse_sentiment(summary_text)

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
  호재 우세 → bullish / 악재 우세 → bearish / 혼조 → mixed / 중립 → neutral

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
    else:
        prompt = select_prompt(len(new_articles)).format(
            ticker=ticker, news_input=news_input
        )

    summary_text = _generate_content(prompt, ticker, model=settings.gemini_model_lite)
    if summary_text is None:
        return None

    return {
        "summary_text": summary_text,
        "sentiment": parse_sentiment(summary_text),
        "source_urls": source_urls,
    }


# ──────────────────────────────────────────
# 주간 리포트: sentiment 파싱 / 입력 포맷
# ──────────────────────────────────────────

def parse_weekly_sentiment(summary_text: str) -> str:
    """
    [이번 주 종합 판단] 섹션에서 bullish/bearish/neutral 추출.
    섹션을 찾으면 해당 구간에서, 못 찾으면 전체에서 첫 매칭을 사용.
    """
    section = summary_text
    m = re.search(r"\[이번 주 종합 판단\](.*)", summary_text, re.DOTALL)
    if m:
        section = m.group(1)
    m2 = re.search(r"\b(bullish|bearish|neutral)\b", section, re.IGNORECASE)
    return m2.group(1).lower() if m2 else "neutral"


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


# 반복 감지용 섹션 헤더 패턴
_REPEAT_SECTION_RE = re.compile(
    r"(\[호재\]|\[악재 및 우려\]|\[호재/악재 추세\])",
    re.IGNORECASE,
)


def validate_response(text: str, ticker: str = "") -> str:
    """
    LLM 응답 후처리 — 반복 폭주 감지 및 잘라내기.

    [호재] / [악재 및 우려] 섹션 내 불릿 항목의 첫 10단어가 동일한 패턴이
    3회 이상 연속되면, 해당 지점 이후를 잘라내고 경고 한 줄을 추가한다.
    전체 텍스트 길이가 15,000자 초과 시에도 무조건 잘라낸다(하드캡).
    """
    HARD_CAP = 15_000
    REPEAT_THRESHOLD = 3

    # 하드캡
    if len(text) > HARD_CAP:
        logger.warning(
            f"[{ticker}] 응답 길이 {len(text):,}자 — 하드캡 {HARD_CAP}자로 잘라냄"
        )
        text = text[:HARD_CAP] + "\n\n*(응답이 너무 길어 자동으로 잘렸습니다)*"

    lines = text.split("\n")
    result: list[str] = []
    # 섹션 진입 후 불릿 fingerprint 카운터
    in_section = False
    bullet_fp_count: dict[str, int] = {}

    for i, line in enumerate(lines):
        # 섹션 헤더 진입
        if _REPEAT_SECTION_RE.search(line):
            in_section = True
            bullet_fp_count = {}
            result.append(line)
            continue

        # 다른 섹션 헤더 → 리셋
        if line.startswith("[") and line.endswith("]") and line != lines[0]:
            in_section = False
            bullet_fp_count = {}

        if in_section:
            stripped = line.lstrip()
            is_bullet = stripped and stripped[0] in ("•", "-", "*") and len(stripped) > 3
            if is_bullet:
                words = re.split(r"\s+", stripped[1:].strip())
                fp = " ".join(w.lower() for w in words[:10])
                cnt = bullet_fp_count.get(fp, 0) + 1
                bullet_fp_count[fp] = cnt
                if cnt > REPEAT_THRESHOLD:
                    logger.warning(
                        f"[{ticker}] 반복 불릿 {cnt}회 감지 (fp='{fp[:40]}…') "
                        f"— 이후 내용 잘라냄 (line {i})"
                    )
                    result.append(
                        "\n*(동일 항목 반복 감지 — 이후 내용 생략됨)*"
                    )
                    break

        result.append(line)

    return "\n".join(result)


# ──────────────────────────────────────────
# 주간 리포트 생성 (월요일 초안)
# ──────────────────────────────────────────

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

    반환: {"summary_text": ..., "sentiment": ...} 또는 None
    """
    daily_reports = daily_reports or []
    raw_articles = raw_articles or []

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

    summary_text = _generate_content(
        prompt, ticker,
        model=settings.gemini_model_lite,
        max_output_tokens=3000,
    )
    if summary_text is None:
        return None

    summary_text = validate_response(summary_text, ticker)
    return {
        "summary_text": summary_text,
        "sentiment": parse_weekly_sentiment(summary_text),
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

    deduped_daily = _dedup_daily_bullets(_format_daily_reports(daily_reports))
    prompt = PROMPT_WEEKLY_UPDATE.format(
        ticker=ticker,
        draft_report=draft_report or "(초안 없음)",
        daily_reports=deduped_daily,
    )

    logger.info(
        f"[{ticker}] weekly 최종본 업데이트 시작 (daily {len(daily_reports)}건)"
    )

    summary_text = _generate_content(
        prompt, ticker,
        model=settings.gemini_model_lite,
        max_output_tokens=3000,
    )
    if summary_text is None:
        return None

    summary_text = validate_response(summary_text, ticker)
    return {
        "summary_text": summary_text,
        "sentiment": parse_weekly_sentiment(summary_text),
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
# 연간 리포트용 주간 데이터 추출 헬퍼
# ──────────────────────────────────────────

def extract_weekly_headline(summary_text: str) -> str:
    """[이번 주 핵심 한 줄] 섹션의 본문 한 줄 추출. 없으면 빈 문자열."""
    m = re.search(r"\[이번 주 핵심 한 줄\]\s*\n(.+)", summary_text)
    return m.group(1).strip() if m else ""


def extract_sector_theme(summary_text: str) -> str:
    """섹터 뉴스 리포트의 '핵심 테마: ...' 한 줄 추출. 없으면 빈 문자열."""
    m = re.search(r"핵심 테마:\s*(.+)", summary_text)
    return m.group(1).strip() if m else ""


def extract_next_week_watch(summary_text: str) -> str | None:
    """[다음 주 주목할 뉴스] 섹션 본문 추출. 섹션 없으면 None."""
    m = re.search(
        r"\[다음 주 주목할 뉴스\]\s*\n(.+?)(?=\n\[|\Z)", summary_text, re.DOTALL
    )
    if not m:
        return None
    content = m.group(1).strip()
    return content if content else None



# ──────────────────────────────────────────
# 중장기(Midterm) 리포트 — 최근 12주 집계
# ──────────────────────────────────────────

PROMPT_MIDTERM = """
당신은 미국 주식 뉴스를 분석하는 전문 애널리스트입니다.
아래는 [{ticker}] 종목의 최근 {week_count}주간 데이터입니다.

=== 주간 리포트 시퀀스 (과거 → 최근) ===
{weekly_reports}

=== 누적 성과 (사전 계산됨) ===
이 종목 누적 수익률: {stock_cumulative}%
S&P500 누적 수익률: {sp500_cumulative}%
{sector_name} 섹터({exchange}) 누적 수익률: {sector_cumulative}%
시장 대비 alpha: {alpha_vs_market}%p
섹터 대비 alpha: {alpha_vs_sector}%p

=== 같은 기간 {sector_name} 섹터 주간 뉴스 ===
{sector_news}

위 데이터를 바탕으로 중장기 리포트를 작성하세요.

───────────────────────────────
작성 규칙 (반드시 준수)
───────────────────────────────
1. 모든 내용은 제공된 주간 리포트/섹터 뉴스에 근거해야 합니다. 추측 금지.
2. 누적 성과 수치는 위에 제공된 값을 그대로 사용하세요. 직접 계산하지 마세요.
3. "~로 알려졌다", "~할 것으로 보인다" 같은 추측성 표현 금지.
4. 섹터 뉴스가 없는 주는 [종목 vs 섹터 흐름 비교] 비교에서 제외하세요.
5. 호재 또는 악재가 전혀 없었으면 [호재/악재 추세] 섹션 전체를 생략하세요.

───────────────────────────────
출력 형식 (마크다운, 한국어)
───────────────────────────────
[중장기 핵심 한 줄]
최근 {week_count}주를 관통하는 가장 중요한 흐름을 1문장으로.

[중장기 흐름]
주차별 스토리를 변곡점 중심으로 서술. 단순 나열 금지.
(예: "1~3주차 실적 기대감 → 4주차 발표 후 반전 → 5~6주차 안정")

[호재/악재 추세]
주간 리포트들의 (카테고리: OOO) 태그를 바탕으로
반복/증가/감소한 이슈 분석.
호재 또는 악재가 없었으면 섹션 전체 생략.

[누적 성과 vs 벤치마크]
사전계산된 수치를 인용하며 해석.
시장/섹터 대비 초과 또는 부진 여부, 이유를 흐름과 연결.

[종목 vs 섹터 흐름 비교]
이 종목의 주간 sentiment 흐름과 섹터 뉴스의 sentiment 흐름을 비교.
동조/디커플링 구간을 구분해서 설명.
섹터 뉴스가 없는 주는 비교에서 제외.

[중장기 종합 판단]
sentiment: bullish / bearish / neutral
근거: alpha + sentiment 추세를 종합한 한 줄.
"""


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


def parse_midterm_sentiment(summary_text: str) -> str:
    """[중장기 종합 판단] 섹션에서 bullish/bearish/neutral 추출."""
    section = summary_text
    m = re.search(r"\[중장기 종합 판단\](.*)", summary_text, re.DOTALL)
    if m:
        section = m.group(1)
    for kw in ("bullish", "bearish", "neutral"):
        if kw in section.lower():
            return kw
    return "neutral"


def _build_midterm_template(
    ticker: str,
    weekly_reports: list[dict],
) -> dict:
    """
    weekly_reports 1~2개인 경우 LLM 없이 템플릿 반환.
    반환: {"summary_text": ..., "sentiment": None, "price_change_pct": None}
    """
    n = len(weekly_reports)
    dates_str = ", ".join(str(r.get("week_monday", "")) for r in weekly_reports)
    lines = [
        "[중장기 리포트]",
        "",
        f"최근 12주 내 주간 리포트 {n}건 ({dates_str})",
        "",
    ]
    for r in weekly_reports:
        wm = r.get("week_monday", "")
        body = r.get("summary_text") or ""
        lines.append(f"--- {wm} 주간 ---")
        lines.append(body)
        lines.append("")
    lines.append("단편적인 뉴스만 있고 특별한 흐름은 없음.")
    return {
        "summary_text": "\n".join(lines),
        "sentiment": None,
        "price_change_pct": None,
    }


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

    - weekly_reports 0개 → None (완전 스킵)
    - weekly_reports 1~2개 → LLM 없이 템플릿 반환
    - weekly_reports 3개 이상 → Gemini(Flash) 호출

    반환: {"summary_text", "sentiment" (nullable), "price_change_pct" (nullable)}
    """
    if not weekly_reports:
        return None

    if len(weekly_reports) <= 2:
        return _build_midterm_template(ticker, weekly_reports)

    week_count = len(weekly_reports)
    stock_series = [r.get("price_change_pct") for r in weekly_reports]

    stock_cumulative = _calc_cumulative(stock_series)
    sp500_cumulative = _calc_cumulative(sp500_series)
    sector_cumulative = _calc_cumulative(sector_series)
    alpha_vs_market = stock_cumulative - sp500_cumulative
    alpha_vs_sector = stock_cumulative - sector_cumulative

    prompt = PROMPT_MIDTERM.format(
        ticker=ticker,
        week_count=week_count,
        weekly_reports=_format_weekly_reports_for_midterm(weekly_reports),
        stock_cumulative=f"{stock_cumulative:+.2f}",
        sp500_cumulative=f"{sp500_cumulative:+.2f}",
        sector_name=sector_name,
        exchange=exchange,
        sector_cumulative=f"{sector_cumulative:+.2f}",
        alpha_vs_market=f"{alpha_vs_market:+.2f}",
        alpha_vs_sector=f"{alpha_vs_sector:+.2f}",
        sector_news=_format_sector_news_for_midterm(sector_news),
    )

    logger.info(f"[{ticker}] midterm 요약 시작 ({week_count}주 기반)")

    summary_text = _generate_content(prompt, ticker)
    if summary_text is None:
        return None

    return {
        "summary_text": summary_text,
        "sentiment": parse_midterm_sentiment(summary_text),
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

    summary_text = _generate_content(prompt, "SECTOR")
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


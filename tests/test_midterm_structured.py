"""midterm 구조화 출력(render_midterm_report, summarize_midterm) 단위 테스트."""
from unittest.mock import patch

import pytest

from app.summarizer.llm_summarizer import (
    CategorizedItem,
    MidtermReportData,
    render_midterm_report,
    summarize_midterm,
)


def _sample_data(
    *,
    trend_items: list[CategorizedItem] | None = None,
    include_default_trends: bool = True,
    sector_comparison="섹터 전반은 부정적인 분위기지만, 이 종목은 그 영향을 크게 받지 않고 따로 버티는 모습이다.",
) -> MidtermReportData:
    if trend_items is None and include_default_trends:
        trend_items = [
            CategorizedItem(content="기록적 분기 실적", category="실적_재무"),
        ]
    elif trend_items is None:
        trend_items = []
    return MidtermReportData(
        headline="AI 기대와 규제 우려가 공존하는 혼조 흐름",
        flow_narrative="주초 실적 호재 → 중반 규제 이슈 → 주말 관망",
        trend_items=trend_items,
        trend_interpretation="실적 호재가 반복되었으나 규제 이슈도 함께 증가했다.",
        benchmark_interpretation="시장 대비 부진했지만 섹터 대비는 상대적으로 양호했다.",
        sector_comparison=sector_comparison,
        sentiment="mixed",
        sentiment_reason="호재와 악재가 균형을 이루며 중립에 가깝다.",
    )


class TestRenderMidtermReport:
    def test_full_fields(self):
        text = render_midterm_report(
            _sample_data(),
            "Technology",
            "NASDAQ",
            stock_cumulative=-2.37,
            sp500_cumulative=0.03,
            sector_cumulative=-1.35,
            alpha_vs_market=-2.40,
            alpha_vs_sector=-1.02,
        )
        assert "[중장기 핵심 한 줄]" in text
        assert "[호재/악재 추세]" in text
        assert "[종목 vs 섹터 흐름 비교]" in text
        assert "[중장기 종합 판단] 혼조" in text
        assert "이 종목 누적 수익률: -2.37%" in text
        assert "S&P500 누적 수익률: +0.03%" in text
        assert "Technology 섹터(NASDAQ) 누적 수익률: -1.35%" in text
        assert "시장 대비 alpha: -2.40%p" in text
        assert "섹터 대비 alpha: -1.02%p" in text

    def test_empty_trend_items_omits_section(self):
        data = _sample_data(trend_items=[], include_default_trends=False)
        data.trend_interpretation = None
        text = render_midterm_report(
            data, "Technology", "NASDAQ",
            1.0, 2.0, 3.0, -1.0, -2.0,
        )
        assert "[호재/악재 추세]" not in text

    def test_none_sector_comparison_omits_section(self):
        data = _sample_data(sector_comparison=None)
        text = render_midterm_report(
            data, "Technology", "NASDAQ",
            1.0, 2.0, 3.0, -1.0, -2.0,
        )
        assert "[종목 vs 섹터 흐름 비교]" not in text

    def test_benchmark_numbers_from_args_not_llm(self):
        """LLM 해석에 다른 숫자가 있어도 숫자 줄은 함수 인자 값을 사용."""
        data = _sample_data()
        data.benchmark_interpretation = "누적 수익률은 +99.99%로 시장을 크게 상회했다."
        text = render_midterm_report(
            data, "Technology", "NASDAQ",
            stock_cumulative=-2.37,
            sp500_cumulative=0.03,
            sector_cumulative=-1.35,
            alpha_vs_market=-2.40,
            alpha_vs_sector=-1.02,
        )
        assert "이 종목 누적 수익률: -2.37%" in text
        assert "+99.99%" not in text.split("[누적 성과 vs 벤치마크]")[1].split("\n\n")[0]


_WEEKLY = [
    {"week_monday": "2026-01-05", "sentiment": "mixed", "price_change_pct": 1.0,
     "summary_text": "week1"},
    {"week_monday": "2026-01-12", "sentiment": "mixed", "price_change_pct": -0.5,
     "summary_text": "week2"},
    {"week_monday": "2026-01-19", "sentiment": "bearish", "price_change_pct": -1.0,
     "summary_text": "week3"},
]


class TestSummarizeMidterm:
    def test_template_path_unchanged_for_one_or_two_weeks(self):
        result = summarize_midterm(
            "AAPL", _WEEKLY[:2], [0.1, 0.2], [0.3, 0.4],
            "Technology", "NASDAQ", [],
        )
        assert result is not None
        assert result["sentiment"] is None
        assert "[중장기 리포트]" in result["summary_text"]

    @patch("app.summarizer.llm_summarizer._generate_structured")
    def test_structured_ok(self, mock_gen):
        mock_gen.return_value = MidtermReportData(
            headline="핵심 한 줄",
            flow_narrative="흐름 서술",
            trend_items=[],
            benchmark_interpretation="해석 문장",
            sector_comparison=None,
            sentiment="bullish",
            sentiment_reason="호재 우세",
        )
        result = summarize_midterm(
            "AAPL", _WEEKLY, [0.1, 0.2, 0.3], [0.4, 0.5, 0.6],
            "Technology", "NASDAQ",
            [{"week_monday": "2026-01-05", "sentiment": "positive", "summary_text": "sn"}],
        )
        assert result is not None
        assert result["sentiment"] == "bullish"
        assert "[중장기 핵심 한 줄]" in result["summary_text"]
        assert result["price_change_pct"] is not None

    @patch("app.summarizer.llm_summarizer._generate_structured")
    def test_broken_json_returns_none(self, mock_gen):
        mock_gen.return_value = None
        result = summarize_midterm(
            "AAPL", _WEEKLY, [0.1, 0.2, 0.3], [0.4, 0.5, 0.6],
            "Technology", "NASDAQ", [],
        )
        assert result is None

    @patch("app.summarizer.llm_summarizer._generate_structured")
    def test_invalid_sentiment_fallback_neutral(self, mock_gen):
        mock_gen.return_value = MidtermReportData(
            headline="h",
            flow_narrative="f",
            benchmark_interpretation="b",
            sentiment="super_bullish",
            sentiment_reason="r",
        )
        result = summarize_midterm(
            "AAPL", _WEEKLY, [0.1, 0.2, 0.3], [0.4, 0.5, 0.6],
            "Technology", "NASDAQ", [],
        )
        assert result["sentiment"] == "neutral"

"""
should_generate_midterm() 단위 테스트.

실행: pytest tests/test_midterm_trigger.py -v
"""
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from app.scheduler.tasks import should_generate_midterm, MIDTERM_FORCE_INTERVAL_DAYS

# 테스트 기준 날짜 (월요일)
BASE_MONDAY = date(2026, 5, 4)   # 임의의 월요일


def monday(offset_weeks: int = 0) -> date:
    """BASE_MONDAY 기준으로 N주 후(전) 월요일 반환."""
    return BASE_MONDAY + timedelta(weeks=offset_weeks)


# ──────────────────────────────────────────
# 기본 케이스
# ──────────────────────────────────────────

class TestBasicCases:
    def test_no_final_this_week_returns_false(self):
        """이번 주 final 없음 → 무조건 False."""
        assert should_generate_midterm(
            ticker="AAPL",
            this_week_monday=monday(0),
            this_week_has_final=False,
            prev_week_has_final=True,   # prev final 있어도
            last_midterm_date=None,     # midterm 없어도
        ) is False

    def test_both_finals_present_returns_true(self):
        """이번 주 + 직전 주 final 있음 → True (기본 트리거)."""
        assert should_generate_midterm(
            ticker="AAPL",
            this_week_monday=monday(0),
            this_week_has_final=True,
            prev_week_has_final=True,
            last_midterm_date=monday(-4),  # 4주 전 midterm 있어도 기본 트리거
        ) is True

    def test_no_prev_final_no_last_midterm_returns_true(self):
        """직전 주 final 없음 + last_midterm None → True (첫 발행 강제 트리거)."""
        assert should_generate_midterm(
            ticker="AAPL",
            this_week_monday=monday(0),
            this_week_has_final=True,
            prev_week_has_final=False,
            last_midterm_date=None,
        ) is True

    def test_no_prev_final_recent_midterm_returns_false(self):
        """직전 주 final 없음 + last_midterm 3주 전 → False."""
        assert should_generate_midterm(
            ticker="AAPL",
            this_week_monday=monday(0),
            this_week_has_final=True,
            prev_week_has_final=False,
            last_midterm_date=monday(-3),  # 3주 전 = 21일 < 42일
        ) is False

    def test_no_prev_final_old_midterm_exactly_42days_returns_true(self):
        """직전 주 final 없음 + last_midterm 정확히 42일 전 → True (강제 트리거)."""
        last = monday(0) - timedelta(days=MIDTERM_FORCE_INTERVAL_DAYS)
        assert should_generate_midterm(
            ticker="AAPL",
            this_week_monday=monday(0),
            this_week_has_final=True,
            prev_week_has_final=False,
            last_midterm_date=last,
        ) is True

    def test_no_prev_final_midterm_41days_ago_returns_false(self):
        """직전 주 final 없음 + last_midterm 41일 전 → False (1일 부족)."""
        last = monday(0) - timedelta(days=41)
        assert should_generate_midterm(
            ticker="AAPL",
            this_week_monday=monday(0),
            this_week_has_final=True,
            prev_week_has_final=False,
            last_midterm_date=last,
        ) is False


# ──────────────────────────────────────────
# 패턴 시뮬레이션
# ──────────────────────────────────────────

class TestPatternSimulation:
    """
    연속 주차에 걸쳐 should_generate_midterm 이 언제 True 가 되는지 시뮬레이션.
    has_final[i] = i번째 주에 weekly final 이 있었는지 여부.
    """

    @staticmethod
    def simulate(has_final_pattern: list[bool]) -> list[bool]:
        """
        주어진 has_final 패턴으로 각 주 should_generate_midterm 결과를 반환.
        last_midterm_date 는 True 가 반환된 직전 주 monday 로 갱신.
        """
        results = []
        last_midterm: date | None = None
        for i, has_final in enumerate(has_final_pattern):
            this_monday = monday(i)
            prev_has_final = has_final_pattern[i - 1] if i > 0 else False
            trigger = should_generate_midterm(
                ticker="TEST",
                this_week_monday=this_monday,
                this_week_has_final=has_final,
                prev_week_has_final=prev_has_final,
                last_midterm_date=last_midterm,
            )
            results.append(trigger)
            if trigger:
                last_midterm = this_monday
        return results

    def test_oxoxoo_pattern(self):
        """
        O=final 있음, X=없음: O X O X O O
        - 0주: O → first_final, prev=X → 강제(None) → True
        - 1주: X → False
        - 2주: O → prev=X, last=0주(2주 전=14일<42일) → False
        - 3주: X → False
        - 4주: O → prev=X, last=0주(4주 전=28일<42일) → False
        - 5주: O → prev=O → True
        """
        pattern = [True, False, True, False, True, True]
        results = self.simulate(pattern)
        assert results == [True, False, False, False, False, True], \
            f"OXOXOO 결과: {results}"

    def test_xoxox_pattern_force_trigger(self):
        """
        X O X O X O X — 격주 패턴: 강제 트리거가 6주 후에 발동되는지 확인.
        - 0주: X → False
        - 1주: O → prev=X, last=None → True (강제, 첫 발행)
        - 2주: X → False
        - 3주: O → prev=X, last=1주(2주 전=14일<42일) → False
        - 4주: X → False
        - 5주: O → prev=X, last=1주(4주 전=28일<42일) → False
        - 6주: X → False
        - 7주: O → prev=X, last=1주(6주 전=42일) → True (강제)
        """
        pattern = [False, True, False, True, False, True, False, True]
        results = self.simulate(pattern)
        # 1주차와 7주차에만 True
        assert results[0] is False, f"0주: {results[0]}"
        assert results[1] is True,  f"1주: {results[1]}"   # 강제(None)
        assert results[2] is False, f"2주: {results[2]}"
        assert results[3] is False, f"3주: {results[3]}"
        assert results[4] is False, f"4주: {results[4]}"
        assert results[5] is False, f"5주: {results[5]}"
        assert results[6] is False, f"6주: {results[6]}"
        assert results[7] is True,  f"7주: {results[7]}"   # 강제(42일)

    def test_all_true_pattern(self):
        """매주 final 있음 → 2주차부터 매주 True."""
        pattern = [True] * 6
        results = self.simulate(pattern)
        # 0주: prev=False, last=None → True (강제, 첫 발행)
        # 1주~5주: prev=True → True
        assert all(results), f"모두 True여야 함: {results}"

    def test_single_isolated_final(self):
        """final이 1번만 나오고 이후 5주 없음 → 1번만 발행."""
        # has_final: [True, False, False, False, False, False]
        pattern = [True, False, False, False, False, False]
        results = self.simulate(pattern)
        assert results[0] is True   # 첫 발행 (강제, None)
        assert all(r is False for r in results[1:]), f"이후 False여야 함: {results[1:]}"

    def test_no_final_at_all(self):
        """final 없으면 항상 False."""
        pattern = [False] * 8
        results = self.simulate(pattern)
        assert not any(results), f"모두 False여야 함: {results}"


# ──────────────────────────────────────────
# 경계값 테스트
# ──────────────────────────────────────────

class TestEdgeCases:
    def test_force_trigger_exactly_6_weeks(self):
        """6주(42일) 정확히 — 강제 트리거."""
        last = monday(0) - timedelta(weeks=6)
        assert should_generate_midterm(
            ticker="X",
            this_week_monday=monday(0),
            this_week_has_final=True,
            prev_week_has_final=False,
            last_midterm_date=last,
        ) is True

    def test_force_trigger_over_6_weeks(self):
        """7주 지남 — 강제 트리거."""
        last = monday(0) - timedelta(weeks=7)
        assert should_generate_midterm(
            ticker="X",
            this_week_monday=monday(0),
            this_week_has_final=True,
            prev_week_has_final=False,
            last_midterm_date=last,
        ) is True

    def test_prev_final_overrides_no_midterm_needed_check(self):
        """prev_final=True이면 last_midterm이 있어도 (심지어 어제여도) True."""
        assert should_generate_midterm(
            ticker="X",
            this_week_monday=monday(0),
            this_week_has_final=True,
            prev_week_has_final=True,
            last_midterm_date=monday(0) - timedelta(days=1),  # 거의 최근
        ) is True

    def test_no_final_overrides_everything(self):
        """this_week_has_final=False이면 prev=True, last=None이어도 False."""
        assert should_generate_midterm(
            ticker="X",
            this_week_monday=monday(0),
            this_week_has_final=False,
            prev_week_has_final=True,
            last_midterm_date=None,
        ) is False

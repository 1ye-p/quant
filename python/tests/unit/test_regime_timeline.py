"""Unit tests for cquant.backtest_vector.regime_timeline helpers."""

from cquant.backtest_vector.regime_timeline import (
    MIN_REGIME_CYCLES,
    STATE_FLAT,
    STATE_FULL,
    STATE_REDUCED,
    build_regime_intervals,
    compute_contribution,
    count_cycles,
    is_meaningful_scale_history,
    state_label,
)


def _days(n: int) -> list[str]:
    return [f"2025-01-{d:02d}" for d in range(1, n + 1)]


class TestStateLabel:
    def test_thresholds(self):
        assert state_label(1.0) == STATE_FULL
        assert state_label(0.9995) == STATE_FULL
        assert state_label(0.5) == STATE_REDUCED
        assert state_label(0.0) == STATE_FLAT
        assert state_label(0.0005) == STATE_FLAT


class TestBuildRegimeIntervals:
    def test_change_points_merge_consecutive(self):
        dates = _days(10)
        scales = [1.0] * 4 + [0.0] * 3 + [1.0] * 3
        intervals = build_regime_intervals(dates, scales)
        assert len(intervals) == 3
        assert [i.state for i in intervals] == [STATE_FULL, STATE_FLAT, STATE_FULL]
        assert intervals[0].start == "2025-01-01" and intervals[0].end == "2025-01-04"
        assert intervals[1].start == "2025-01-05" and intervals[1].end == "2025-01-07"
        assert intervals[0].days == 4 and intervals[1].days == 3 and intervals[2].days == 3

    def test_distinct_scales_same_state_are_separate_intervals(self):
        dates = _days(6)
        scales = [1.0, 1.0, 0.5, 0.5, 1.0, 1.0]
        intervals = build_regime_intervals(dates, scales)
        # full → reduced → full: 3 intervals even though only 2 labels
        assert len(intervals) == 3
        assert intervals[1].scale == 0.5

    def test_interval_return_from_nav(self):
        dates = _days(4)
        scales = [1.0, 1.0, 0.0, 0.0]
        navs = [1.0, 1.1, 1.2, 1.15]
        intervals = build_regime_intervals(dates, scales, nav_values=navs)
        # full interval covers days 1-2; return = nav[2]/nav[0]-1 = 0.2
        assert abs(intervals[0].interval_return - 0.2) < 1e-9
        # flat interval covers days 3-4 (last): nav[-1]/nav[1]-1
        assert abs(intervals[1].interval_return - (1.15 / 1.1 - 1.0)) < 1e-9

    def test_empty_and_mismatched_inputs(self):
        assert build_regime_intervals([], []) == []
        assert build_regime_intervals(_days(3), [1.0, 1.0]) == []


class TestCountCycles:
    def test_transitions(self):
        dates = _days(12)
        scales = [1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0]
        intervals = build_regime_intervals(dates, scales)
        assert count_cycles(intervals) == 5

    def test_single_interval_zero_cycles(self):
        intervals = build_regime_intervals(_days(5), [1.0] * 5)
        assert count_cycles(intervals) == 0

    def test_min_cycles_constant(self):
        assert MIN_REGIME_CYCLES == 6


class TestComputeContribution:
    def test_benchmark_split(self):
        dates = _days(6)
        scales = [1.0, 1.0, 0.0, 0.0, 1.0, 1.0]
        port = [0.01, 0.02, 0.0, 0.0, 0.01, 0.01]
        bench = [0.01, 0.01, -0.05, 0.03, 0.01, 0.01]
        out = compute_contribution(dates, scales, port, bench)
        assert out["held_days"] == 4 and out["flat_days"] == 2
        # held: compound of the 4 non-flat portfolio days
        expected_held = 1.01 * 1.02 * 1.01 * 1.01 - 1
        assert abs(out["held_return"] - expected_held) < 1e-9
        oom = out["out_of_market"]
        assert oom["benchmark_source"] == "benchmark"
        # avoided: the flat-day benchmark loss dodged (positive magnitude)
        assert abs(oom["avoided"] - 0.05) < 1e-9
        # missed: the flat-day benchmark gain forgone
        assert abs(oom["missed"] - 0.03) < 1e-9

    def test_no_benchmark_uses_mean_approximation(self):
        dates = _days(4)
        scales = [1.0, 1.0, 0.0, 0.0]
        port = [0.01, 0.03, 0.0, 0.0]
        out = compute_contribution(dates, scales, port, None)
        oom = out["out_of_market"]
        assert oom["benchmark_source"] == "mean_return_approx"
        # mean held daily return = 0.02, compounded over the 2 flat days
        assert abs(oom["missed"] - (1.02 * 1.02 - 1)) < 1e-9
        assert oom["avoided"] == 0.0

    def test_all_flat_and_empty(self):
        out = compute_contribution(_days(2), [0.0, 0.0], [0.0, 0.0], None)
        assert out["held_days"] == 0 and out["flat_days"] == 2
        empty = compute_contribution([], [], [])
        assert empty["held_return"] == 0.0
        assert empty["out_of_market"]["benchmark_source"] == "none"


class TestApplicability:
    def test_constant_full_scale_is_not_regime(self):
        assert is_meaningful_scale_history([1.0] * 10) is False

    def test_empty_is_not_regime(self):
        assert is_meaningful_scale_history([]) is False

    def test_any_de_scaling_is_regime(self):
        assert is_meaningful_scale_history([1.0, 1.0, 0.5, 1.0]) is True

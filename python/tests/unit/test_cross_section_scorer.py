"""Tests for CrossSectionScorer neutralization."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import polars as pl
import pytest

from cquant.factorlab.cross_section_scorer import (
    CrossSectionScorer,
    FactorWeight,
    ScoringConfig,
)


@pytest.fixture
def mock_catalog():
    """Create a mock catalog with test data."""
    catalog = MagicMock()
    return catalog


@pytest.fixture
def sample_factor_data():
    """Sample factor data for testing."""
    dates = ["2024-01-01", "2024-01-01", "2024-01-01", "2024-01-02", "2024-01-02", "2024-01-02"]
    assets = ["A", "B", "C", "A", "B", "C"]
    factor1 = [1.0, 2.0, 3.0, 1.5, 2.5, 3.5]
    factor2 = [0.1, 0.2, 0.3, 0.15, 0.25, 0.35]

    return pl.DataFrame(
        {
            "trade_date": dates,
            "asset_id": assets,
            "factor1": factor1,
            "factor2": factor2,
        }
    ).with_columns(pl.col("trade_date").cast(pl.Date))


@pytest.fixture
def sample_mktcap_data():
    """Sample market cap data."""
    dates = ["2024-01-01", "2024-01-01", "2024-01-01", "2024-01-02", "2024-01-02", "2024-01-02"]
    assets = ["A", "B", "C", "A", "B", "C"]
    market_cap = [1e9, 2e9, 3e9, 1.1e9, 2.1e9, 3.1e9]

    return pl.DataFrame(
        {
            "asset_id": assets,
            "trade_date": dates,
            "market_cap": market_cap,
        }
    ).with_columns(
        pl.col("trade_date").cast(pl.Date),
        pl.col("market_cap").log().alias("ln_mktcap"),
    ).drop("market_cap")


@pytest.fixture
def sample_industry_data():
    """Sample industry data."""
    assets = ["A", "B", "C"]
    industry = ["Tech", "Finance", "Tech"]

    return pl.DataFrame(
        {
            "asset_id": assets,
            "industry": industry,
        }
    )


class TestNeutralizeFactors:
    """Test _neutralize_factors method."""

    def test_empty_neutralize_list(self, mock_catalog, sample_factor_data):
        """When neutralize list is empty, return original data."""
        scorer = CrossSectionScorer(mock_catalog)
        config = ScoringConfig(
            name="test",
            factors=[FactorWeight(factor_name="factor1")],
            neutralize=[],
        )

        result = scorer._neutralize_factors(sample_factor_data, config, "2024-01-01", "2024-01-02")
        assert result.equals(sample_factor_data)

    def test_no_factor_columns(self, mock_catalog, sample_factor_data):
        """When factor columns not in DataFrame, return original data."""
        scorer = CrossSectionScorer(mock_catalog)
        config = ScoringConfig(
            name="test",
            factors=[FactorWeight(factor_name="missing_factor")],
            neutralize=["market_cap"],
        )

        result = scorer._neutralize_factors(sample_factor_data, config, "2024-01-01", "2024-01-02")
        assert result.equals(sample_factor_data)

    def test_neutralize_market_cap(self, mock_catalog, sample_factor_data, sample_mktcap_data):
        """Test market cap neutralization produces residuals."""
        scorer = CrossSectionScorer(mock_catalog)

        # Mock the _load_neutralization_data method
        with patch.object(scorer, "_load_neutralization_data", return_value=sample_mktcap_data):
            config = ScoringConfig(
                name="test",
                factors=[FactorWeight(factor_name="factor1"), FactorWeight(factor_name="factor2")],
                neutralize=["market_cap"],
            )

            result = scorer._neutralize_factors(sample_factor_data, config, "2024-01-01", "2024-01-02")

            # Check that result has same columns as input
            assert set(result.columns) == set(sample_factor_data.columns)

            # Check that factor values have been modified (neutralized)
            original = sample_factor_data.sort(["trade_date", "asset_id"])
            neutralized = result.sort(["trade_date", "asset_id"])

            # The values should be different after neutralization
            assert not original["factor1"].equals(neutralized["factor1"])
            assert not original["factor2"].equals(neutralized["factor2"])

    def test_neutralize_industry(self, mock_catalog):
        """Test industry neutralization produces residuals."""
        scorer = CrossSectionScorer(mock_catalog)

        # Use more assets to avoid perfect fit
        factor_data = pl.DataFrame(
            {
                "trade_date": ["2024-01-01"] * 6,
                "asset_id": ["A", "B", "C", "D", "E", "F"],
                "factor1": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
                "factor2": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
            }
        ).with_columns(pl.col("trade_date").cast(pl.Date))

        # Create industry neutralization data with one-hot encoding (3 industries, 6 assets)
        industry_data = pl.DataFrame(
            {
                "asset_id": ["A", "B", "C", "D", "E", "F"],
                "trade_date": ["2024-01-01"] * 6,
                "industry_Finance": [0.0, 1.0, 0.0, 0.0, 1.0, 0.0],
                "industry_Healthcare": [0.0, 0.0, 1.0, 0.0, 0.0, 1.0],
                "industry_Tech": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            }
        ).with_columns(pl.col("trade_date").cast(pl.Date))

        with patch.object(scorer, "_load_neutralization_data", return_value=industry_data):
            config = ScoringConfig(
                name="test",
                factors=[FactorWeight(factor_name="factor1"), FactorWeight(factor_name="factor2")],
                neutralize=["industry"],
            )

            result = scorer._neutralize_factors(factor_data, config, "2024-01-01", "2024-01-01")

            # Check that result has same columns as input (industry dummies should be dropped)
            assert set(result.columns) == set(factor_data.columns)

            # Check that factor values have been modified
            original = factor_data.sort(["trade_date", "asset_id"])
            neutralized = result.sort(["trade_date", "asset_id"])

            assert not original["factor1"].equals(neutralized["factor1"])

    def test_neutralize_both(self, mock_catalog, sample_factor_data, sample_mktcap_data):
        """Test neutralization with both market_cap and industry."""
        scorer = CrossSectionScorer(mock_catalog)

        # Combine market cap and industry data
        combined_data = sample_mktcap_data.with_columns(
            pl.Series("industry_Finance", [0.0, 1.0, 0.0, 0.0, 1.0, 0.0]),
            pl.Series("industry_Tech", [1.0, 0.0, 1.0, 1.0, 0.0, 1.0]),
        )

        with patch.object(scorer, "_load_neutralization_data", return_value=combined_data):
            config = ScoringConfig(
                name="test",
                factors=[FactorWeight(factor_name="factor1")],
                neutralize=["market_cap", "industry"],
            )

            result = scorer._neutralize_factors(sample_factor_data, config, "2024-01-01", "2024-01-02")

            # Check that result has same columns as input
            assert set(result.columns) == set(sample_factor_data.columns)

            # Check that helper columns are dropped
            assert "ln_mktcap" not in result.columns
            assert not any(c.startswith("industry_") for c in result.columns)

    def test_insufficient_observations(self, mock_catalog):
        """When too few observations for regression, keep original values."""
        scorer = CrossSectionScorer(mock_catalog)

        # Only 1 observation per date - not enough for regression
        small_data = pl.DataFrame(
            {
                "trade_date": ["2024-01-01"],
                "asset_id": ["A"],
                "factor1": [1.0],
            }
        ).with_columns(pl.col("trade_date").cast(pl.Date))

        small_mktcap = pl.DataFrame(
            {
                "asset_id": ["A"],
                "trade_date": ["2024-01-01"],
                "ln_mktcap": [20.0],
            }
        ).with_columns(pl.col("trade_date").cast(pl.Date))

        with patch.object(scorer, "_load_neutralization_data", return_value=small_mktcap):
            config = ScoringConfig(
                name="test",
                factors=[FactorWeight(factor_name="factor1")],
                neutralize=["market_cap"],
            )

            result = scorer._neutralize_factors(small_data, config, "2024-01-01", "2024-01-01")

            # Should keep original values since we can't regress with 1 observation
            assert result["factor1"][0] == 1.0

    def test_empty_neutralization_data(self, mock_catalog, sample_factor_data):
        """When neutralization data is empty, return original data."""
        scorer = CrossSectionScorer(mock_catalog)

        with patch.object(scorer, "_load_neutralization_data", return_value=pl.DataFrame()):
            config = ScoringConfig(
                name="test",
                factors=[FactorWeight(factor_name="factor1")],
                neutralize=["market_cap"],
            )

            result = scorer._neutralize_factors(sample_factor_data, config, "2024-01-01", "2024-01-02")
            assert result.equals(sample_factor_data)


class TestLoadNeutralizationData:
    """Test _load_neutralization_data method."""

    def test_load_market_cap(self, mock_catalog):
        """Test loading market cap data from silver_fundamentals."""
        scorer = CrossSectionScorer(mock_catalog)

        mock_catalog.query.return_value = pl.DataFrame(
            {
                "asset_id": ["A", "B"],
                "trade_date": ["2024-01-01", "2024-01-01"],
                "market_cap": [1e9, 2e9],
            }
        ).with_columns(pl.col("trade_date").cast(pl.Date))

        result = scorer._load_neutralization_data(["market_cap"], "2024-01-01", "2024-01-01")

        assert "ln_mktcap" in result.columns
        assert "asset_id" in result.columns
        assert "trade_date" in result.columns

    def test_load_industry(self, mock_catalog):
        """Test loading industry data from silver_assets."""
        scorer = CrossSectionScorer(mock_catalog)

        # Mock two queries: one for industry, one for dates
        mock_catalog.query.side_effect = [
            # First call: industry data
            pl.DataFrame(
                {
                    "asset_id": ["A", "B", "C"],
                    "industry": ["Tech", "Finance", "Tech"],
                }
            ),
            # Second call: distinct dates
            pl.DataFrame(
                {
                    "trade_date": ["2024-01-01", "2024-01-02"],
                }
            ).with_columns(pl.col("trade_date").cast(pl.Date)),
        ]

        result = scorer._load_neutralization_data(["industry"], "2024-01-01", "2024-01-02")

        assert "industry_Tech" in result.columns
        assert "industry_Finance" in result.columns
        assert "asset_id" in result.columns
        assert "trade_date" in result.columns

    def test_load_both(self, mock_catalog):
        """Test loading both market cap and industry data."""
        scorer = CrossSectionScorer(mock_catalog)

        mock_catalog.query.side_effect = [
            # First call: market cap
            pl.DataFrame(
                {
                    "asset_id": ["A", "B"],
                    "trade_date": ["2024-01-01", "2024-01-01"],
                    "market_cap": [1e9, 2e9],
                }
            ).with_columns(pl.col("trade_date").cast(pl.Date)),
            # Second call: industry
            pl.DataFrame(
                {
                    "asset_id": ["A", "B"],
                    "industry": ["Tech", "Finance"],
                }
            ),
            # Third call: distinct dates
            pl.DataFrame(
                {
                    "trade_date": ["2024-01-01"],
                }
            ).with_columns(pl.col("trade_date").cast(pl.Date)),
        ]

        result = scorer._load_neutralization_data(
            ["market_cap", "industry"], "2024-01-01", "2024-01-01"
        )

        assert "ln_mktcap" in result.columns
        assert "industry_Tech" in result.columns
        assert "industry_Finance" in result.columns


class TestScoreIntegration:
    """Test that score() correctly integrates neutralization."""

    def test_score_with_neutralization(self, mock_catalog):
        """Test full scoring pipeline with neutralization."""
        scorer = CrossSectionScorer(mock_catalog)

        # Mock _load_factors
        factor_data = pl.DataFrame(
            {
                "trade_date": ["2024-01-01", "2024-01-01", "2024-01-01"],
                "asset_id": ["A", "B", "C"],
                "factor1": [1.0, 2.0, 3.0],
            }
        ).with_columns(pl.col("trade_date").cast(pl.Date))

        mktcap_data = pl.DataFrame(
            {
                "asset_id": ["A", "B", "C"],
                "trade_date": ["2024-01-01", "2024-01-01", "2024-01-01"],
                "ln_mktcap": [20.0, 21.0, 22.0],
            }
        ).with_columns(pl.col("trade_date").cast(pl.Date))

        with patch.object(scorer, "_load_factors", return_value=factor_data):
            with patch.object(scorer, "_load_neutralization_data", return_value=mktcap_data):
                config = ScoringConfig(
                    name="test",
                    factors=[FactorWeight(factor_name="factor1")],
                    neutralize=["market_cap"],
                )

                result = scorer.score(config, "v1", "2024-01-01", "2024-01-01")

                assert "trade_date" in result.columns
                assert "asset_id" in result.columns
                assert "score" in result.columns
                assert "rank" in result.columns
                assert len(result) == 3

    def test_score_without_neutralization(self, mock_catalog):
        """Test scoring pipeline without neutralization."""
        scorer = CrossSectionScorer(mock_catalog)

        factor_data = pl.DataFrame(
            {
                "trade_date": ["2024-01-01", "2024-01-01", "2024-01-01"],
                "asset_id": ["A", "B", "C"],
                "factor1": [1.0, 2.0, 3.0],
            }
        ).with_columns(pl.col("trade_date").cast(pl.Date))

        with patch.object(scorer, "_load_factors", return_value=factor_data):
            config = ScoringConfig(
                name="test",
                factors=[FactorWeight(factor_name="factor1")],
                neutralize=[],
            )

            result = scorer.score(config, "v1", "2024-01-01", "2024-01-01")

            assert len(result) == 3
            # Without neutralization, scores should be z-scored factor values


# ─── fill_null 枚举（A3：统一 missing 处理）────────────────────────────────


def _fill_null_factor_data() -> pl.DataFrame:
    """4 assets × 3 factors，asset B 的 f2 缺失（唯一 null）。"""
    return pl.DataFrame(
        {
            "trade_date": ["2024-01-01"] * 4,
            "asset_id": ["A", "B", "C", "D"],
            "f1": [1.0, 2.0, 3.0, 4.0],
            "f2": [1.0, None, 3.0, 4.0],
            "f3": [4.0, 3.0, 2.0, 1.0],
        }
    ).with_columns(pl.col("trade_date").cast(pl.Date))


def _z(values: list[float]) -> np.ndarray:
    """Null-skipping population used by both scorer and multi_factor (ddof=1)."""
    arr = np.asarray([v for v in values if v is not None], dtype=float)
    return (arr - arr.mean()) / arr.std(ddof=1)


class TestFillNullModes:
    """ScoringConfig.fill_null 四选项行为。"""

    def _score(self, mock_catalog, data: pl.DataFrame, **config_kwargs):
        scorer = CrossSectionScorer(mock_catalog)
        config = ScoringConfig(
            name="test_fill_null",
            factors=[
                FactorWeight(factor_name="f1"),
                FactorWeight(factor_name="f2"),
                FactorWeight(factor_name="f3"),
            ],
            neutralize=[],
            winsorize=(0.0, 1.0),  # no-op clipping
            **config_kwargs,
        )
        with patch.object(scorer, "_load_factors", return_value=data):
            return scorer.score(config, "v1", "2024-01-01", "2024-01-01")

    def test_fill_modes_produce_no_null_scores(self, mock_catalog):
        """median/mean/zero 回归：填充后所有资产都有有限合成分。"""
        for mode in ("median", "mean", "zero"):
            result = self._score(mock_catalog, _fill_null_factor_data(), fill_null=mode)
            assert len(result) == 4, mode
            assert result["score"].null_count() == 0, mode

    def test_fill_median_hand_calc(self, mock_catalog):
        """median：B 的 f2 用截面中位数 3.0 填充后参与 z-score。"""
        result = self._score(mock_catalog, _fill_null_factor_data(), fill_null="median")
        scores = dict(zip(result["asset_id"].to_list(), result["score"].to_list()))

        z1 = _z([1.0, 2.0, 3.0, 4.0])
        z2_filled = _z([1.0, 3.0, 3.0, 4.0])  # B filled with median 3.0
        z3 = _z([4.0, 3.0, 2.0, 1.0])
        expected_b = z1[1] + z2_filled[1] + z3[1]
        assert scores["B"] == pytest.approx(expected_b, abs=1e-9)

    def test_risk_penalty_hand_calc(self, mock_catalog):
        """risk_penalty：合成分 = Σ(可用因子 z 分) − 缺失数 × penalty（手算对齐）。"""
        result = self._score(
            mock_catalog,
            _fill_null_factor_data(),
            fill_null="risk_penalty",
            penalty_per_missing=0.5,
        )
        scores = dict(zip(result["asset_id"].to_list(), result["score"].to_list()))

        z1 = _z([1.0, 2.0, 3.0, 4.0])
        z2 = _z([1.0, 3.0, 4.0])  # null-skipping: B 不参与统计
        z3 = _z([4.0, 3.0, 2.0, 1.0])

        # B：f2 缺失 → 不填充、不计入合成分，扣 1 × 0.5
        expected_b = z1[1] + z3[1] - 1 * 0.5
        assert scores["B"] == pytest.approx(expected_b, abs=1e-9)

        # A：无缺失 → 无扣减；z2 对 A 的取值 = (1 - mean([1,3,4]))/std
        z2_a = (1.0 - np.mean([1.0, 3.0, 4.0])) / np.std([1.0, 3.0, 4.0], ddof=1)
        expected_a = z1[0] + z2_a + z3[0]
        assert scores["A"] == pytest.approx(expected_a, abs=1e-9)

    def test_risk_penalty_missing_column_not_counted(self, mock_catalog):
        """配置了但数据中整列缺失的因子：不计 per-row 惩罚（对齐 multi_factor 语义）。"""
        data = _fill_null_factor_data().drop("f3")
        result = self._score(
            mock_catalog, data, fill_null="risk_penalty", penalty_per_missing=1.0
        )
        assert len(result) == 4
        # f3 整列缺失不计惩罚；A 无 null → 合成分 = z1(A) + z2(A)，无扣减
        scores = dict(zip(result["asset_id"].to_list(), result["score"].to_list()))
        z1 = _z([1.0, 2.0, 3.0, 4.0])
        z2_a = (1.0 - np.mean([1.0, 3.0, 4.0])) / np.std([1.0, 3.0, 4.0], ddof=1)
        assert scores["A"] == pytest.approx(z1[0] + z2_a, abs=1e-9)

    def test_risk_penalty_with_neutralization_keeps_penalty(self, mock_catalog):
        """risk_penalty + 中性化：numpy 残差（null→NaN）还原为 null，扣罚不丢。"""
        from datetime import date as _date

        data = _fill_null_factor_data()
        scorer = CrossSectionScorer(mock_catalog)
        mktcap = pl.DataFrame(
            {
                "asset_id": ["A", "B", "C", "D"],
                "trade_date": [_date(2024, 1, 1)] * 4,
                "ln_mktcap": [20.0, 21.0, 22.0, 23.0],
            }
        )

        def _run(penalty: float) -> pl.DataFrame:
            config = ScoringConfig(
                name=f"test_rp_neutral_{penalty}",
                factors=[FactorWeight(factor_name="f1"), FactorWeight(factor_name="f2")],
                neutralize=["market_cap"],
                winsorize=(0.0, 1.0),
                fill_null="risk_penalty",
                penalty_per_missing=penalty,
            )
            with patch.object(scorer, "_load_factors", return_value=data):
                with patch.object(
                    scorer, "_load_neutralization_data", return_value=mktcap
                ):
                    return scorer.score(config, "v1", "2024-01-01", "2024-01-01")

        with_pen = _run(0.5)
        without_pen = _run(0.0)

        assert with_pen["score"].null_count() == 0
        scored = dict(zip(with_pen["asset_id"].to_list(), with_pen["score"].to_list()))
        nopen = dict(
            zip(without_pen["asset_id"].to_list(), without_pen["score"].to_list())
        )
        # B（缺 f2）：扣罚 1 × 0.5；A（无缺失）：不受影响
        assert (nopen["B"] - scored["B"]) == pytest.approx(0.5, abs=1e-9)
        assert nopen["A"] == pytest.approx(scored["A"], abs=1e-12)


class TestCrossModuleRiskPenaltyConsistency:
    """A3 核心用例：scorer 与 MultiFactorStrategy 的 risk_penalty 打分一致。"""

    def test_same_cross_section_same_ranking(self, mock_catalog):
        """同截面、同权重：两实现合成分逐资产一致（容差 1e-9）且排序一致。"""
        from datetime import date

        from cquant.backtest_vector.strategies.multi_factor import MultiFactorStrategy
        from cquant.backtest_vector.strategy import StrategyContext

        features = _fill_null_factor_data()
        factor_weights = {"f1": 1.0, "f2": 1.0, "f3": 1.0}

        # scorer 侧（winsorize 关闭、无中性化 → 与 multi_factor 数学口径对齐）
        scorer = CrossSectionScorer(mock_catalog)
        config = ScoringConfig(
            name="consistency",
            factors=[FactorWeight(factor_name=n, weight=w) for n, w in factor_weights.items()],
            neutralize=[],
            winsorize=(0.0, 1.0),
            fill_null="risk_penalty",
            penalty_per_missing=0.5,
        )
        with patch.object(scorer, "_load_factors", return_value=features):
            scored = scorer.score(config, "v1", "2024-01-01", "2024-01-01")

        # multi_factor 侧
        strat = MultiFactorStrategy(
            "mf_consistency",
            factor_weights=factor_weights,
            top_n=4,
            missing_factor_strategy="risk_penalty",
            penalty_per_missing=0.5,
        )
        ctx = StrategyContext(
            as_of_date=date(2024, 1, 1), universe_id="u", features=features
        )
        signals = strat.generate_signals(ctx)

        scorer_scores = dict(zip(scored["asset_id"].to_list(), scored["score"].to_list()))
        mf_scores = dict(zip(signals["asset_id"].to_list(), signals["strength"].to_list()))

        assert set(scorer_scores) == set(mf_scores)
        for asset in scorer_scores:
            assert scorer_scores[asset] == pytest.approx(
                mf_scores[asset], abs=1e-9
            ), f"score divergence at {asset}"

        # 排序一致
        scorer_order = scored.sort("rank")["asset_id"].to_list()
        mf_order = signals["asset_id"].to_list()
        assert scorer_order == mf_order

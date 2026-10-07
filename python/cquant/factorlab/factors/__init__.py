"""Built-in factors: momentum, volatility, turnover, technical, value, quality, growth, size."""

from cquant.factorlab.factors.kbar import (
    KMID, KLEN, KMID2, KUP, KUP2, KLOW, KLOW2, KSFT, KSFT2, KBAR_FACTORS,
)
from cquant.factorlab.factors.alpha158_rolling import (
    ROC5, ROC10, ROC20, ROC30,
    MA5, MA10, MA20, MA30,
    STD5, STD10, STD20, STD30,
    MAX5, MAX20, MIN5, MIN20,
    ALPHA158_ROLLING_FACTORS,
)
from cquant.factorlab.factors.alpha158 import ALPHA158_FACTORS
from cquant.factorlab.factors.momentum import (
    Return1d,
    Return5d,
    Return20d,
    Return60d,
    Return120d,
    Return240d,
    Momentum12_1,
)
from cquant.factorlab.factors.volatility import (
    Vol20d,
    Vol60d,
    Vol120d,
    DownsideVol20d,
    MaxDrawdown20d,
)
from cquant.factorlab.factors.turnover import (
    TurnoverRate20d,
    VolumeRatio5d,
    AmountRatio5d,
)
from cquant.factorlab.factors.technical import (
    ZscoreClose60d,
    MA20dRatio,
    RSI14d,
    BollingerBandWidth20d,
    PriceHigh20dRatio,
)
from cquant.factorlab.factors.value import (
    PETTM,
    PB,
    DividendYield,
)
from cquant.factorlab.factors.quality import (
    ROE,
    ROA,
    GrossMargin,
)
from cquant.factorlab.factors.growth import (
    RevenueGrowth,
    EarningsGrowth,
)
from cquant.factorlab.factors.size import (
    MarketCap,
    LogMarketCap,
)
from cquant.factorlab.factors.dividend import (
    DividendYield12M,
    DividendMomentum,
    DIVIDEND_FACTORS,
)

BUILTIN_FACTORS = [
    # Momentum
    Return1d(), Return5d(), Return20d(), Return60d(), Return120d(), Return240d(),
    Momentum12_1(),
    # Volatility
    Vol20d(), Vol60d(), Vol120d(),
    DownsideVol20d(), MaxDrawdown20d(),
    # Turnover
    TurnoverRate20d(), VolumeRatio5d(), AmountRatio5d(),
    # Technical
    ZscoreClose60d(), MA20dRatio(), RSI14d(),
    BollingerBandWidth20d(), PriceHigh20dRatio(),
    # Value
    PETTM(), PB(), DividendYield(),
    # Quality
    ROE(), ROA(), GrossMargin(),
    # Growth
    RevenueGrowth(), EarningsGrowth(),
    # Size
    MarketCap(), LogMarketCap(),
    # Dividend event factors
    DividendYield12M(), DividendMomentum(),
    # Alpha158 KBAR
    *KBAR_FACTORS,
    # Alpha158 Rolling
    *ALPHA158_ROLLING_FACTORS,
]

# 追加 Alpha158 新增因子（去重，避免与已注册的 KBAR/Rolling 重复）
_existing_names = {f.name for f in BUILTIN_FACTORS}
for _f in ALPHA158_FACTORS:
    if _f.name not in _existing_names:
        BUILTIN_FACTORS.append(_f)
        _existing_names.add(_f.name)

__all__ = [
    "Return1d", "Return5d", "Return20d", "Return60d", "Return120d", "Return240d",
    "Momentum12_1",
    "Vol20d", "Vol60d", "Vol120d", "DownsideVol20d", "MaxDrawdown20d",
    "TurnoverRate20d", "VolumeRatio5d", "AmountRatio5d",
    "ZscoreClose60d", "MA20dRatio", "RSI14d", "BollingerBandWidth20d", "PriceHigh20dRatio",
    "PETTM", "PB", "DividendYield",
    "ROE", "ROA", "GrossMargin",
    "RevenueGrowth", "EarningsGrowth",
    "MarketCap", "LogMarketCap",
    "DividendYield12M", "DividendMomentum", "DIVIDEND_FACTORS",
    "KMID", "KLEN", "KMID2", "KUP", "KUP2", "KLOW", "KLOW2", "KSFT", "KSFT2", "KBAR_FACTORS",
    "ROC5", "ROC10", "ROC20", "ROC30",
    "MA5", "MA10", "MA20", "MA30",
    "STD5", "STD10", "STD20", "STD30",
    "MAX5", "MAX20", "MIN5", "MIN20",
    "ALPHA158_ROLLING_FACTORS",
    "ALPHA158_FACTORS",
    "BUILTIN_FACTORS",
]

# ── Vibe-Trading 因子 zoo（仅在 Vibe-Trading submodule 可用时注册）────────────
import logging as _vibe_logging
from cquant.vibe_bridge._compat import VIBE_AVAILABLE as _VIBE_AVAILABLE

_VIBE_ZOOS = ("qlib158", "alpha101", "gtja191")

# F4 降级可见性：zoo 不可用/加载失败时的记录（source + reason），
# 由 /factors/available 暴露给前端提示。
_DEGRADED_SOURCES: list[dict] = []


def get_degraded_sources() -> list[dict]:
    """返回降级因子源记录的拷贝（防外部修改内部状态）。"""
    return [dict(d) for d in _DEGRADED_SOURCES]


if _VIBE_AVAILABLE:
    for _zoo_name in _VIBE_ZOOS:
        try:
            from cquant.vibe_bridge.alpha_zoo import load_zoo as _load_zoo
            _zoo_factors = _load_zoo(_zoo_name)
            BUILTIN_FACTORS.extend(_zoo_factors)
            _vibe_logging.getLogger(__name__).info(
                "Registered %d %s factors from Vibe-Trading", len(_zoo_factors), _zoo_name
            )
        except Exception as _exc:
            _DEGRADED_SOURCES.append({"source": _zoo_name, "reason": str(_exc)[:200]})
            _vibe_logging.getLogger(__name__).warning(
                "Failed to load %s factors from Vibe-Trading: %s", _zoo_name, _exc
            )
else:
    for _zoo_name in _VIBE_ZOOS:
        _DEGRADED_SOURCES.append({"source": _zoo_name, "reason": "submodule_unavailable"})
        _vibe_logging.getLogger(__name__).info(
            "Vibe-Trading submodule unavailable; skipping %s factor zoo", _zoo_name
        )

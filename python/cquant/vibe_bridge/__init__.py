"""cquant.vibe_bridge — cQuant 对 Vibe-Trading 的封装层（唯一出口）。

所有需要使用 Vibe-Trading 功能的模块，只导入此包，
不直接 import vibe-trading 内部模块。
"""
from __future__ import annotations

from cquant.vibe_bridge._compat import VIBE_AVAILABLE, vibe_or_fallback, require_vibe


def __getattr__(name: str):
    """延迟导入子模块（避免循环导入和启动时依赖错误）。"""
    if name == "load_zoo":
        from cquant.vibe_bridge.alpha_zoo import load_zoo
        return load_zoo
    if name == "VibeFactor":
        from cquant.vibe_bridge.alpha_zoo import VibeFactor
        return VibeFactor
    raise AttributeError(f"module 'cquant.vibe_bridge' has no attribute {name!r}")


__all__ = [
    "VIBE_AVAILABLE",
    "vibe_or_fallback",
    "require_vibe",
    "load_zoo",
    "VibeFactor",
]

# 注：VibSwarmLoader / load_vibe_providers / list_vibe_providers 已移至
# examples/vibe_trading_extras/（未接线、不在主依赖路径，见该目录 README）。

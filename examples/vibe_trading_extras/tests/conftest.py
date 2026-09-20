"""手动运行这些 extras 测试所需的路径引导。

用法（仓库根目录）：
    conda run -n cQuanty python -m pytest examples/vibe_trading_extras/tests/ -v

这些测试不在默认 CI 收集路径（python/tests）下，仅用于手动验证 extras。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# 让 `import cquant` 与顶层 `import swarm / providers` 可用
# （不能用 examples.* 命名空间：site-packages 存在同名 examples 包会遮蔽）
for p in (
    str(_REPO_ROOT / "python"),
    str(_REPO_ROOT / "examples" / "vibe_trading_extras"),
):
    if p not in sys.path:
        sys.path.insert(0, p)

# Vibe-Trading Extras（未接线组件）

本目录存放从 `python/cquant/vibe_bridge/` 移出的 Vibe-Trading 相关组件。它们曾随 Phase 0-B 一起实现，但**从未接入主依赖路径**（生产代码零消费），为开源瘦身已移出主包。

## 内容

| 文件 | 说明 |
|------|------|
| `swarm.py` | `VibSwarmLoader` — Vibe-Trading Swarm 团队预置配置加载器（29 团队 YAML → cQuant 格式） |
| `providers.py` | `load_vibe_providers` / `list_vibe_providers` / `get_provider_config` — LLM 供应商适配（读取 `lib/vibe-trading/agent/src/providers/llm_providers.json` 的 14 供应商配置） |
| `tests/` | 对应的手动测试（不在默认 CI 收集路径下） |

## 不受影响的部分

`python/cquant/vibe_bridge/` 中的 `alpha_zoo.py`、`_compat.py` 保持原位，**526 Alpha 因子（Alpha101/GTJA191/Qlib158 扩展）功能完全不受影响**。`python/cquant/factorlab/factors/` 对 `load_zoo` / `VIBE_AVAILABLE` 的消费路径不变。

## 运行测试（手动）

```bash
conda activate cQuanty
python -m pytest examples/vibe_trading_extras/tests/ -v --no-cov
```

注意：`swarm.py` / `providers.py` 依赖 `lib/vibe-trading` 子模块存在，且继续 `import cquant.vibe_bridge._compat`（extras → bridge 是正常依赖方向）。

## 如何恢复（移回主包）

1. 从 git 历史取回原位置：`git log --follow -- examples/vibe_trading_extras/swarm.py` 找到引入提交，取其父提交中的 `python/cquant/vibe_bridge/swarm.py`（`providers.py` 同理）。
2. `git mv examples/vibe_trading_extras/swarm.py python/cquant/vibe_bridge/swarm.py`（providers.py 同理），并把 `Path(__file__).resolve().parents[2]` 改回 `parents[3]`。
3. 测试移回 `python/tests/unit/`，import 改回 `from cquant.vibe_bridge.swarm import ...`，删除 `examples/vibe_trading_extras/tests/conftest.py`。
4. 在 `python/cquant/vibe_bridge/__init__.py` 的 `__getattr__` / `__all__` 中恢复 `VibSwarmLoader` / `load_vibe_providers` / `list_vibe_providers` 的延迟导出。

# my_strategy — L3 策略插件示例

最小可用的 L3 策略插件：声明 `strategy` capability，入口为
`my_plugin_strategy:create_strategy` 工厂，返回
`cquant.backtest_vector.strategy.Strategy` 子类。

## 文件

| 文件 | 作用 |
|------|------|
| `plugin.json` | 插件 manifest（name / capabilities / entrypoints） |
| `my_plugin_strategy.py` | 入口模块：`MyStrategy` + `create_strategy` 工厂 |

## 使用

```bash
# 1) 让插件目录可导入（或 pip install -e 你的插件包）
export PYTHONPATH="/path/to/cquant/examples/plugins/my_strategy:$PYTHONPATH"

# 2) 指定插件发现路径（Registry.discover 会递归找 plugin.json）
export CQUANT_PLUGIN_PATHS="/path/to/cquant/examples/plugins"

# 3) 在回测配置中把 strategy_type 设为插件名
#    config_json: {"strategy_type": "my_strategy", "top_n": 5, "min_strength": 0.2}
```

`StrategyLoader.load()` 内置 map 找不到 `my_strategy` 时，会经
`Registry().resolve("strategy", "my_strategy")` 解析到
`create_strategy` 工厂并按 config_json 实例化。

也可以在代码里注入已发现的 Registry：

```python
from cquant.registry.registry import Registry
from cquant.execution.strategy_loader import StrategyLoader

registry = Registry()
registry.discover(["examples/plugins"])
loader = StrategyLoader(catalog, registry=registry)
strategy = loader.load("my_backtest_strategy_id")
```

## 编写自己的策略插件

1. 新建目录 + `plugin.json`，`capabilities` 含 `"strategy"`，
   `entrypoints.strategy` 指向 `"模块:工厂函数"`；
2. 工厂返回 `Strategy` 子类（实现 `strategy_id` 与 `generate_signals`）；
3. 构造器参数与回测 `config_json` 的键一一对应（`strategy_id` 由 loader 注入）。

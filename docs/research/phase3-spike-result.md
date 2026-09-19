# Phase 3 Spike — `__MARKET__` 哨兵伪 panel 捷径验证

> 日期：2026-09-06（Task 0 spike）
> 结论：**A — 捷径通**。现有 DSL 求值器直接消费伪 panel，仅需列名注册（+ 可选 2 个便捷函数注册）。T3 = 包装层 1-2 天。

## 验证对象

| 组件 | 文件 | 关键事实 |
|------|------|----------|
| 求值器 | `python/cquant/factorlab/dsl_evaluator.py` | `compile_expression(expr) -> pl.Expr`，纯列表达式，**内部无 `over("asset_id")`、无任何 group 语义** |
| 函数库 | `python/cquant/factorlab/dsl_functions.py` | 16 个函数全部为列级 Polars 表达式（`rolling_*` / `shift` / `ewm_mean`）；`AVAILABLE_COLUMNS` 是模块级 `set` 白名单 |
| PIT loader | `python/cquant/datahub/external_loader.py` | `load_external_series(catalog, key, as_of, asset_id="__MARKET__")`，SQL 层 `available_date <= as_of` 过滤，返回 `(trade_date, value)` 有序 DataFrame |

## 结论 A 的证据（合成数据，30 天，可手算复现）

### 1. 伪 panel 构造与求值 —— 通过

`load_external_series` 输出 rename `value -> active_cap`，加 `asset_id='__MARKET__'` 常量列，排序后直接喂 `compile_expression` —— 所有表达式编译并求值成功。

### 2. 数值表（节选 + 手算对照）

合成序列：`vals[i] = 100 + 2i + (i%7==0 ? 3 : 0) + (i%3)*0.5`

| 表达式 | DSL 写法 | 首个非 null 行 | 手算对照 |
|--------|----------|----------------|----------|
| `pct_change(active_cap, 1)` 等价 | `delta(active_cap, 1) / lag(active_cap, 1)` | idx 1 | idx6: (112-111)/111 = 0.00900901 ✅ 与 DSL 输出一致 |
| `ma(active_cap, 5)` | 原生 | idx 4（前 4 行 null，边界正确） | idx19: mean(vals[15:20]) = 134.4 ✅ |
| `zscore(active_cap, 20)` 等价 | `(active_cap - ma(active_cap,20)) / std(active_cap,20)` | idx 19（std 窗口要求满 20，共 11 个非 null） | 数值合理（~1.5-1.7，上升趋势段） |
| 嵌套 `zscore(pct_change_5, 20)` | 同上复合 `delta(active_cap,5)/lag(active_cap,5)` 再 zscore | idx 24（1 null + 4 滚动预热 + 20 std 窗口 → 6 个非 null） | 链式 null 传播正确 |

完整 30 行输出见下方可复现脚本。

### 3. 分组语义 —— 单资产退化正确，多资产无泄漏

- 函数库**不含** `over("asset_id")`，分组由调用方施加：单资产伪 panel 下 `with_columns(expr)` 直接退化 per-row 时序（无 group 错位，上表已证）。
- 双资产 panel（`__MARKET__` + `000001.SZ`，后者值 ×2）用 `expr.over("asset_id")` 求值：两组 ma5@idx4 分别为 105.0 / 210.0，与各组手算精确一致（<1e-9），**无跨组泄漏**。
- 结论：哨兵行即使混入真实多资产 panel，只要调用方统一 `over("asset_id")`，语义正确；单资产独立求值（regime 引擎的用法）也正确。

### 4. PIT 语义 —— 天然安全

求值器只看传入的 DataFrame。先 `filter(available_date <= as_of)` 再求值（即 `load_external_series` 的行为），结果与全量 panel 在同一日期的值逐位一致（ma5@2025-01-20 = 134.4 两种路径相同）。**滚动函数不存在窗口越过 PIT 边界的可能——过滤发生在求值之前，表达式无法看见未来行。**

## 需要的适配（不影响结论 A）

1. **`AVAILABLE_COLUMNS` 必须扩展**（已实测：不改则 `Unknown column: 'active_cap'` 报错）。白名单是模块级 `set`，spike 用运行时 `dsl_functions.AVAILABLE_COLUMNS.add("active_cap")` 通过。生产方案二选一：
   - 便宜：包装层在加载外部指标列时动态 add（列名 = indicator_key 映射）；
   - 正规：`compile_expression(expr, extra_columns: set[str] | None)` 加可选参数（约 +3 行，不改架构）。
2. **`pct_change` / `zscore` 不在 FUNCTIONS 中**（16 个函数里没有）。两个选择：
   - DSL 里用 `delta/lag` 与 `(x-ma)/std` 组合（本 spike 全部如此，无功能损失）；
   - 在 `dsl_functions.py` 注册两个便捷函数（各 ~2 行：`col.pct_change(n)` Polars 原生支持；zscore = `(col - rolling_mean) / rolling_std`）。建议做，UX 收益明显。
3. 包装层职责（T3 工作量主体）：`load_external_series` → rename 列 → 注册列名 → `compile_expression` → 按日期对齐回策略 panel。1-2 天估计成立。

## 可复现脚本（spike 原样，conda env `cQuanty`）

```python
import sys
sys.path.insert(0, "python")
import polars as pl
from cquant.factorlab import dsl_functions
from cquant.factorlab.dsl_evaluator import compile_expression, DSLError

n = 30
vals = [100.0 + 2.0*i + (3.0 if i % 7 == 0 else 0.0) + (i % 3) * 0.5 for i in range(n)]
panel = pl.DataFrame({
    "trade_date": [f"2025-01-{i+1:02d}" for i in range(n)],
    "available_date": [f"2025-01-{i+1:02d}" for i in range(n)],
    "active_cap": vals,
    "asset_id": ["__MARKET__"] * n,
}).sort("trade_date")

# 白名单实测：未扩展时 DSLError: Unknown column 'active_cap'
dsl_functions.AVAILABLE_COLUMNS.add("active_cap")

exprs = {
    "pct1":   "delta(active_cap, 1) / lag(active_cap, 1)",                      # pct_change 等价
    "ma5":    "ma(active_cap, 5)",
    "z20":    "(active_cap - ma(active_cap, 20)) / std(active_cap, 20)",        # zscore 等价
    "nested": "( (delta(active_cap,5)/lag(active_cap,5)) - ma(delta(active_cap,5)/lag(active_cap,5), 20) ) / std(delta(active_cap,5)/lag(active_cap,5), 20)",
}
out = panel
for name, e in exprs.items():
    out = out.with_columns(compile_expression(e).alias(name))

assert out.with_row_index().filter(pl.col("ma5").is_not_null())["index"][0] == 4          # 窗口边界
assert abs(out["pct1"][6] - (vals[6]-vals[5])/vals[5]) < 1e-12                            # 手算对照
assert abs(out["ma5"][19] - sum(vals[15:20])/5) < 1e-12

# PIT：先过滤再求值 == 全量求值同日值
pit = panel.filter(pl.col("available_date") <= "2025-01-20").with_columns(
    compile_expression("ma(active_cap, 5)").alias("ma5"))
assert pit.tail(1)["ma5"][0] == out.filter(pl.col("trade_date") == "2025-01-20")["ma5"][0]

# 多资产 over("asset_id") 隔离
v10 = vals[:10]
df = pl.DataFrame({
    "asset_id": ["__MARKET__"]*10 + ["000001.SZ"]*10,
    "trade_date": [f"2025-01-{i+1:02d}" for i in range(10)]*2,
    "active_cap": v10 + [v*2 for v in v10],
}).sort("trade_date")
r = df.with_columns(compile_expression("ma(active_cap, 5)").over("asset_id").alias("ma5"))
mkt = r.filter(pl.col("asset_id") == "__MARKET__").sort("trade_date")["ma5"][4]
sz  = r.filter(pl.col("asset_id") == "000001.SZ").sort("trade_date")["ma5"][4]
assert mkt == sum(v10[0:5])/5 and sz == 2*sum(v10[0:5])/5
print("ALL SPIKE ASSERTIONS PASSED — verdict A")
```

## 门控决议

**走分支 A**：T3（外部指标 → DSL 包装层）按 1-2 天排期，内容 = 列名注册机制（建议 `compile_expression` 加 `extra_columns` 参数）+ 可选注册 `pct_change`/`zscore` 两个函数 + `load_external_series` 对齐包装。求值器架构零改动。

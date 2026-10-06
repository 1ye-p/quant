#!/usr/bin/env python3
"""基准防回归门禁共用工具（P6）。

两个基准脚本（``benchmark_backtest.py`` / ``benchmark_fill_simulator.py``）
共用本模块做基线发现与 wall 回归判定，保证口径一致：

- 基线系列：以 ``*_det`` / ``*_fill`` 确定性基线为新基准（T3 确定性
  修复后同参数可复现）；canonical 基线 = ``2026-10-06_post_p2b`` 性能锚点。
- 发现顺序：``--baseline`` 显式路径 > 仓库内 canonical 基线
  ``configs/benchmarks/baseline/``（确定性锚点，随仓库提交——artifacts/
  下的文件名排序不可靠，"post" < "pre" 会选错"最新"）>
  ``artifacts/benchmarks/`` 下最新匹配文件（canonical 缺失时的回退）。
  刷新锚点：全量跑基准后把 JSON 覆盖 canonical 文件并提交。
- 门禁：当前 wall 相对基线回归 > 阈值（默认 30%）→ 脚本 exit 1。
  仅对两侧都存在的维度/参数组合比较（缺项跳过并提示）。

Exit code 语义（两个脚本一致）：
    0 = 通过（或无可用基线，仅告警）
    1 = 检出 wall 回归超阈值（拦截）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

# 仓库根（scripts/ 的上一级）
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ARTIFACTS_DIR = _REPO_ROOT / "artifacts" / "benchmarks"
_CANONICAL_DIR = _REPO_ROOT / "configs" / "benchmarks" / "baseline"

DEFAULT_THRESHOLD_PCT = 30.0

# canonical 基线文件名（configs/benchmarks/baseline/，随仓库提交）
CANONICAL_BACKTEST = "backtest_det.json"
CANONICAL_FILL = "fill_det.json"


def find_baseline(
    kind: str,
    explicit: str | None = None,
    artifacts_dir: Path | None = None,
) -> Path | None:
    """按发现顺序解析基线 JSON 路径。

    Parameters
    ----------
    kind:
        ``"backtest"`` → 匹配 ``artifacts/benchmarks/*_det.json``；
        ``"fill"``     → 匹配 ``artifacts/benchmarks/*_fill.json``。
    explicit:
        ``--baseline`` 显式路径（最高优先级）。
    artifacts_dir:
        覆盖 artifacts 搜索目录（测试用）。
    """
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"--baseline 指定的文件不存在: {p}")
        return p

    # canonical 优先：确定性锚点（artifacts 文件名排序不可靠——
    # "post_p2a" < "pre_p2a" 字典序会把 pre 优化基线当成"最新"）。
    canonical = _CANONICAL_DIR / (
        CANONICAL_BACKTEST if kind == "backtest" else CANONICAL_FILL
    )
    if canonical.is_file():
        return canonical

    ad = Path(artifacts_dir) if artifacts_dir else _ARTIFACTS_DIR
    suffix = "_det.json" if kind == "backtest" else "_fill.json"
    candidates = sorted(ad.glob(f"*{suffix}")) if ad.is_dir() else []
    if candidates:
        return candidates[-1]
    return None


def load_baseline(path: Path) -> dict[str, Any]:
    """读取基线 JSON（backtest: {"results": [...]}; fill: {"result": {...}}）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def evaluate_wall_gate(
    current_entries: list[dict],
    baseline_entries: list[dict],
    key_fn: Callable[[dict], tuple],
    metric_fn: Callable[[dict], float],
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
) -> tuple[bool, list[str]]:
    """逐项比较当前 wall 与基线 wall，回归超阈值即失败。

    仅比较两侧 key 相同的条目；条目级指标缺失（旧基线无 engine_loop 等）
    由 metric_fn 抛 KeyError/TypeError 表示，捕获后跳过该条目。

    Returns
    -------
    (passed, messages)
        passed=False 时 messages 为逐条失败描述（含阈值与两侧数字）。
    """
    baseline_by_key: dict[tuple, dict] = {}
    for entry in baseline_entries:
        try:
            baseline_by_key[key_fn(entry)] = entry
        except (KeyError, TypeError):
            continue

    passed = True
    messages: list[str] = []
    for entry in current_entries:
        try:
            key = key_fn(entry)
            cur = metric_fn(entry)
        except (KeyError, TypeError):
            continue
        base_entry = baseline_by_key.get(key)
        if base_entry is None:
            messages.append(
                f"SKIP {key}: 基线无对应条目（不比较）"
            )
            continue
        try:
            base = metric_fn(base_entry)
        except (KeyError, TypeError):
            messages.append(
                f"SKIP {key}: 基线条目缺少该指标（不比较）"
            )
            continue
        if base <= 0:
            continue
        regression_pct = (cur - base) / base * 100.0
        if regression_pct > threshold_pct:
            passed = False
            messages.append(
                f"REGRESSION {key}: wall {cur:.3f}s vs 基线 {base:.3f}s "
                f"(+{regression_pct:.1f}% > {threshold_pct:.0f}% 阈值)"
            )
        else:
            messages.append(
                f"OK {key}: wall {cur:.3f}s vs 基线 {base:.3f}s "
                f"({regression_pct:+.1f}%)"
            )
    return passed, messages


def print_gate_report(
    passed: bool,
    messages: list[str],
    baseline_path: Path | None,
    threshold_pct: float,
) -> None:
    """打印门禁结论；无基线时打印告警（exit 0）。"""
    if baseline_path is None:
        print(
            "\n[GATE] 无可用基线（artifacts/benchmarks/ 与 "
            "configs/benchmarks/baseline/ 均未找到）——跳过回归判定，仅记录。"
        )
        return
    print(f"\n[GATE] 基线: {baseline_path} | 阈值: wall 回归 > {threshold_pct:.0f}% 拦截")
    for msg in messages:
        print(f"  {msg}")
    print(
        "[GATE] PASS — 无 wall 回归超阈值"
        if passed
        else "[GATE] FAIL — 检出 wall 回归超阈值（exit 1）"
    )

"""P5: job 协作式取消 + 阶段进度 + 真超时（Task 7 TDD）。

四测：
1. test_timeout_marks_failed_and_exits — 挂死（协作检查点循环）→ 短 deadline
   → job failed(timeout) + watcher 线程退出 + 注册表清理；
2. test_artifacts_kept_after_timeout — 超时后已写产物保留（诊断优先）；
3. test_progress_stages_visible — registry stage 推进（≥3 阶段实测）
   + 引擎/fill 检查点 stage_cb 与 cancel_event 语义；
4. test_semaphore_released_on_cancel — 取消/超时后信号量可被下一个 job 获取。

红线回归（等价门）见 test_perf_equivalence_baseline / test_fill_equivalence /
test_regime_engine —— cancel_event=None 直通路径必须零行为差。
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import date, timedelta

import polars as pl
import pytest

from cquant.api_server import deps
from cquant.api_server.deps import (
    get_job_progress,
    run_job_async,
    set_job_stage,
)
from cquant.core.jobs import JobCancelledError


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeCatalog:
    """最小 catalog 桩：记录 _save_job 风格的持久化调用。"""

    def __init__(self) -> None:
        self.saved: list[tuple] = []
        self._lock = threading.Lock()

    def execute(self, sql: str, params: list | None = None) -> None:
        if "_api_jobs" in sql and params:
            with self._lock:
                self.saved.append(tuple(params))

    def query(self, *a, **k):  # pragma: no cover — 未用于本测试
        return pl.DataFrame()

    def last_status(self) -> str | None:
        with self._lock:
            return self.saved[-1][2] if self.saved else None

    def last_error(self) -> str | None:
        with self._lock:
            return self.saved[-1][4] if self.saved else None


def _cooperative_hang(job_id: str, artifact_path=None, after_event=None):
    """模拟一个带协作检查点的挂死 job：写产物 → 循环检查取消。"""

    def _run() -> None:
        if artifact_path is not None:
            artifact_path.write_text("partial results", encoding="utf-8")
        if after_event is not None:
            after_event.set()
        while True:
            deps.check_job_cancel(job_id)
            time.sleep(0.02)

    return _run


# ---------------------------------------------------------------------------
# 1. timeout marks failed and thread exits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_marks_failed_and_exits(monkeypatch, tmp_path):
    monkeypatch.setenv("CQUANT_JOB_TIMEOUT_SEC", "1")
    catalog = FakeCatalog()
    job_id = "job-timeout-1"

    started = threading.Event()
    body = _cooperative_hang(job_id, after_event=started)

    t0 = time.monotonic()
    await run_job_async(body, job_id=job_id, job_type="backtest", catalog=catalog)
    elapsed = time.monotonic() - t0

    # 协作退出应远快于 30s（watcher 在 ~1s 置位 → 检查点抛出）
    assert elapsed < 15
    assert catalog.last_status() == "failed"
    assert "timeout" in (catalog.last_error() or "").lower()
    # 注册表清理（watcher 线程已退出、entry 移除）
    assert get_job_progress(job_id) is None


# ---------------------------------------------------------------------------
# 2. artifacts kept after timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_artifacts_kept_after_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("CQUANT_JOB_TIMEOUT_SEC", "1")
    catalog = FakeCatalog()
    job_id = "job-timeout-2"
    artifact = tmp_path / f"{job_id}.json"

    await run_job_async(
        _cooperative_hang(job_id, artifact_path=artifact),
        job_id=job_id,
        job_type="sensitivity",
        catalog=catalog,
    )

    assert catalog.last_status() == "failed"
    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8") == "partial results"


# ---------------------------------------------------------------------------
# 3. progress stages visible
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_progress_stages_visible(monkeypatch):
    monkeypatch.setenv("CQUANT_JOB_TIMEOUT_SEC", "60")
    catalog = FakeCatalog()
    job_id = "job-stages-1"
    observed: list[str] = []
    lock = threading.Lock()

    def _run() -> None:
        for stage in ("loading", "signals", "persisting"):
            set_job_stage(job_id, stage)
            with lock:
                observed.append(get_job_progress(job_id)["stage"])
            time.sleep(0.01)

    await run_job_async(_run, job_id=job_id, job_type="backtest", catalog=catalog)

    # registry 内实时可见 ≥3 阶段推进（job 体内逐段读取）
    assert observed == ["loading", "signals", "persisting"]
    # 结束后清理
    assert get_job_progress(job_id) is None


def test_engine_checkpoint_and_stages(tmp_path):
    """引擎/fill 检查点两用：stage_cb 推进 + cancel_event 抛 JobCancelledError。

    复用 test_regime_engine 的双资产构造模式；None 事件直通（等价红线）
    由既有等价门覆盖，此处只验证显式传入路径。
    """
    from cquant.backtest_vector.engine import BacktestSpec, VectorBacktestEngine
    from cquant.backtest_vector.fill_simulator import AShareFillSimulator
    from cquant.backtest_vector.strategy import Strategy, StrategyContext

    class _BuyHold(Strategy):
        @property
        def strategy_id(self) -> str:
            return "cancel_test_buy_hold"

        def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
            return pl.DataFrame({
                "asset_id": ["SH600001", "SH600002"],
                "signal_date": [ctx.as_of_date] * 2,
                "direction": ["long"] * 2,
                "strength": [1.0] * 2,
                "confidence": [1.0] * 2,
            })

    start = date(2025, 1, 6)
    rows = []
    for i in range(20):
        d = start + timedelta(days=i)
        for asset, base in (("SH600001", 10.0), ("SH600002", 20.0)):
            p = base * (1 + 0.001 * i)
            rows.append({
                "trade_date": d, "asset_id": asset,
                "open": p, "high": p * 1.01, "low": p * 0.99,
                "close": p, "volume": 1_000_000.0, "amount": p * 1_000_000,
                "is_suspended": False,
            })
    prices = pl.DataFrame(rows)

    stages: list[str] = []
    evt = threading.Event()
    spec = BacktestSpec(
        strategy=_BuyHold(),
        prices=prices,
        start_date=start,
        end_date=start + timedelta(days=19),
        cancel_event=evt,
        stage_cb=stages.append,
    )
    engine = VectorBacktestEngine()
    result = engine.run(spec)
    assert result.error is None
    assert "signals" in stages and "fills" in stages
    assert stages.index("signals") < stages.index("fills")

    # 预置取消 → 引擎日循环顶抛 JobCancelledError
    evt.set()
    with pytest.raises(JobCancelledError):
        engine.run(spec)

    # fill 日循环顶同样抛出
    weights = pl.DataFrame({
        "trade_date": [start, start],
        "asset_id": ["SH600001", "SH600002"],
        "target_weight": [0.5, 0.5],
    })
    evt2 = threading.Event()
    evt2.set()
    sim = AShareFillSimulator()
    with pytest.raises(JobCancelledError):
        sim.simulate(weights, prices, initial_cash="1000000", cancel_event=evt2)
    # None 直通：不抛
    fills, snaps = sim.simulate(weights, prices, initial_cash="1000000")
    assert not fills.is_empty()


# ---------------------------------------------------------------------------
# 4. semaphore released on cancel/timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_semaphore_released_on_cancel(monkeypatch):
    monkeypatch.setenv("CQUANT_JOB_TIMEOUT_SEC", "1")
    catalog = FakeCatalog()
    cap_before = deps.JOB_SEMAPHORE._value
    running_before = deps.job_queue_stats.running

    await run_job_async(
        _cooperative_hang("job-cancel-1"),
        job_id="job-cancel-1",
        job_type="backtest",
        catalog=catalog,
    )
    assert catalog.last_status() == "failed"

    # 信号量与队列计数复位 → 下一个 job 可立即获取
    assert deps.JOB_SEMAPHORE._value == cap_before
    assert deps.job_queue_stats.running == running_before

    ran = threading.Event()

    def _next_job() -> None:
        ran.set()

    await run_job_async(_next_job, job_id="job-cancel-2", catalog=catalog)
    assert ran.is_set()


# ---------------------------------------------------------------------------
# 5. queue-fair deadline (T7 review Finding 2+3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_wait_does_not_burn_deadline(monkeypatch):
    """排队期不烧预算：deadline 从信号量获取后起算。

    占满信号量（容量 1）→ 提交短 deadline(1s) job 排队 → 等待 1.5s（> deadline）
    → 释放 → job 必须正常跑完而非首个检查点被杀；排队期 stage 为 queued。
    """
    monkeypatch.setenv("CQUANT_JOB_TIMEOUT_SEC", "1")
    monkeypatch.setattr(deps, "JOB_SEMAPHORE", asyncio.Semaphore(1))
    catalog = FakeCatalog()
    release = threading.Event()
    ran = threading.Event()

    def _holder() -> None:
        release.wait(10)

    def _queued_body() -> None:
        # 多个检查点：若 deadline 误从注册起算，此处首个检查点即抛出
        for _ in range(10):
            deps.check_job_cancel("job-queued-1")
            time.sleep(0.02)
        ran.set()

    async def _submit_holder():
        # holder 不接 catalog：它自身运行时长（~1.8s）超过全局 1s deadline，
        # 属合法 failed(timeout)，不应混入 queued job 的持久化断言
        await run_job_async(_holder, job_id="job-holder-1")

    async def _submit_queued():
        await run_job_async(
            _queued_body, job_id="job-queued-1", job_type="backtest",
            catalog=catalog,
        )

    holder_task = asyncio.create_task(_submit_holder())
    await asyncio.sleep(0.3)  # holder 获取唯一的槽位
    queued_task = asyncio.create_task(_submit_queued())
    await asyncio.sleep(1.5)  # 排队时长 > 1s deadline

    progress = get_job_progress("job-queued-1")
    assert progress is not None
    assert progress["stage"] == "queued"
    assert progress["stage_history"] == ["queued"]

    release.set()
    await asyncio.wait_for(holder_task, timeout=10)
    await asyncio.wait_for(queued_task, timeout=10)

    assert ran.is_set()  # 正常跑完，未被超时杀
    assert catalog.last_status() is None  # 无 failed(timeout) 持久化
    assert get_job_progress("job-queued-1") is None


@pytest.mark.asyncio
async def test_stage_history_queued_then_running(monkeypatch):
    """注册期 stage=queued；获取信号量后推进为 running。"""
    monkeypatch.setenv("CQUANT_JOB_TIMEOUT_SEC", "60")
    seen: list[str] = []

    def _run() -> None:
        seen.append(get_job_progress("job-stage-q1")["stage"])

    await run_job_async(_run, job_id="job-stage-q1", catalog=FakeCatalog())
    assert seen == ["running"]

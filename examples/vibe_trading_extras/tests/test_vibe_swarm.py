"""Vibe Swarm 加载器测试。"""
from __future__ import annotations

import pytest

from swarm import VibSwarmLoader


def test_list_presets():
    loader = VibSwarmLoader()
    presets = loader.list_presets()
    assert len(presets) >= 25, f"Expected >= 25 presets, got {len(presets)}"
    assert "quant_strategy_desk" in presets


def test_load_quant_strategy_desk():
    loader = VibSwarmLoader()
    config = loader.load("quant_strategy_desk")
    assert config.name
    assert config.title
    assert len(config.agents) >= 2
    for agent in config.agents:
        assert agent.id
        assert agent.role


def test_load_nonexistent_raises():
    loader = VibSwarmLoader()
    with pytest.raises(FileNotFoundError):
        loader.load("nonexistent_team_xyz")


def test_to_agent_specs():
    loader = VibSwarmLoader()
    config = loader.load("quant_strategy_desk")
    specs = config.to_agent_specs()
    assert len(specs) == len(config.agents)
    for spec in specs:
        assert "id" in spec
        assert "role" in spec


def test_load_presets_have_agents():
    """验证所有预置团队至少有 1 个 agent。"""
    loader = VibSwarmLoader()
    presets = loader.list_presets()
    for name in presets[:5]:  # 抽样检查前 5 个
        config = loader.load(name)
        assert len(config.agents) >= 1, f"{name} has no agents"

"""Vibe-Trading Swarm extras (moved from cquant.vibe_bridge.swarm) — Vibe-Trading Swarm 团队配置加载器。

将 Vibe-Trading 的 YAML 预置团队配置转为 cQuant ai_advisor 可用的格式。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cquant.vibe_bridge._compat import require_vibe

logger = logging.getLogger(__name__)

_VIBE_AGENT_ROOT = Path(__file__).resolve().parents[2] / "lib" / "vibe-trading" / "agent"
_VIBE_PRESETS_ROOT = _VIBE_AGENT_ROOT / "src" / "swarm" / "presets"


@dataclass
class SwarmAgentSpec:
    """单个 Agent 规格（从 Swarm YAML 解析）。"""
    id: str
    role: str
    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    max_iterations: int = 5
    timeout_seconds: int = 300
    max_retries: int = 2


@dataclass
class SwarmTeamConfig:
    """Swarm 团队配置（从 YAML 解析）。"""
    name: str
    title: str
    description: str = ""
    agents: list[SwarmAgentSpec] = field(default_factory=list)

    def to_agent_specs(self) -> list[dict[str, Any]]:
        """转为 cQuant AgentSpec 格式的 dict 列表。"""
        return [
            {
                "id": a.id,
                "role": a.role,
                "system_prompt": a.system_prompt,
                "tools": a.tools,
                "max_iterations": a.max_iterations,
                "timeout_seconds": a.timeout_seconds,
            }
            for a in self.agents
        ]


class VibSwarmLoader:
    """加载 Vibe-Trading Swarm 预置团队配置。"""

    def __init__(self, presets_dir: Path | None = None) -> None:
        self._dir = presets_dir or _VIBE_PRESETS_ROOT

    def list_presets(self) -> list[str]:
        """列出所有可用的 Swarm 预置团队名。"""
        if not self._dir.is_dir():
            return []
        return sorted(p.stem for p in self._dir.glob("*.yaml"))

    def load(self, preset_name: str) -> SwarmTeamConfig:
        """加载指定的 Swarm 团队配置。"""
        require_vibe()
        yaml_path = self._dir / f"{preset_name}.yaml"
        if not yaml_path.exists():
            raise FileNotFoundError(
                f"Swarm preset '{preset_name}' not found at {yaml_path}. "
                f"Available: {self.list_presets()}"
            )
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        agents = [
            SwarmAgentSpec(
                id=a.get("id", ""),
                role=a.get("role", ""),
                system_prompt=a.get("system_prompt", ""),
                tools=a.get("tools", []),
                skills=a.get("skills", []),
                max_iterations=a.get("max_iterations", 5),
                timeout_seconds=a.get("timeout_seconds", 300),
                max_retries=a.get("max_retries", 2),
            )
            for a in data.get("agents", [])
        ]

        return SwarmTeamConfig(
            name=data.get("name", preset_name),
            title=data.get("title", preset_name),
            description=data.get("description", ""),
            agents=agents,
        )

    def load_all(self) -> dict[str, SwarmTeamConfig]:
        """加载所有预置团队。"""
        return {name: self.load(name) for name in self.list_presets()}

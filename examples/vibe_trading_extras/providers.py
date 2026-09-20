"""Vibe-Trading LLM provider extras (moved from cquant.vibe_bridge.providers) — Vibe-Trading LLM 供应商适配器。

读取 Vibe-Trading 的 llm_providers.json 配置，转为 cQuant LLMProvider 可用格式。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from cquant.vibe_bridge._compat import require_vibe

logger = logging.getLogger(__name__)

_VIBE_AGENT_ROOT = Path(__file__).resolve().parents[2] / "lib" / "vibe-trading" / "agent"
_VIBE_PROVIDERS_JSON = _VIBE_AGENT_ROOT / "src" / "providers" / "llm_providers.json"


@dataclass
class VibeLLMProviderConfig:
    """Vibe LLM 供应商配置条目。"""
    name: str
    label: str
    api_key_env: str
    base_url_env: str
    default_model: str
    default_base_url: str
    api_key_required: bool = True

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)

    @property
    def base_url(self) -> str:
        return os.environ.get(self.base_url_env, self.default_base_url)

    @property
    def is_available(self) -> bool:
        if not self.api_key_required:
            return True
        return self.api_key is not None and self.api_key != ""


def load_vibe_providers() -> list[VibeLLMProviderConfig]:
    """读取 Vibe-Trading 的 llm_providers.json，返回配置列表。"""
    require_vibe()
    if not _VIBE_PROVIDERS_JSON.exists():
        logger.warning("Vibe llm_providers.json not found at %s", _VIBE_PROVIDERS_JSON)
        return []

    with open(_VIBE_PROVIDERS_JSON, encoding="utf-8") as f:
        data = json.load(f)

    providers = []
    for entry in data:
        providers.append(VibeLLMProviderConfig(
            name=entry.get("name", ""),
            label=entry.get("label", ""),
            api_key_env=entry.get("api_key_env", ""),
            base_url_env=entry.get("base_url_env", ""),
            default_model=entry.get("default_model", ""),
            default_base_url=entry.get("default_base_url", ""),
            api_key_required=entry.get("api_key_required", True),
        ))
    return providers


def list_vibe_providers() -> list[str]:
    """返回所有 Vibe 供应商名称列表。"""
    return [p.name for p in load_vibe_providers()]


def get_provider_config(name: str) -> VibeLLMProviderConfig | None:
    """按名称获取单个供应商配置。"""
    for p in load_vibe_providers():
        if p.name == name:
            return p
    return None

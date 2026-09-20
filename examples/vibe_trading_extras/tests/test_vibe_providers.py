"""Vibe LLM 供应商适配器测试。"""
from __future__ import annotations

import pytest

from providers import (
    get_provider_config,
    list_vibe_providers,
    load_vibe_providers,
)


def test_load_providers():
    providers = load_vibe_providers()
    assert len(providers) >= 10, f"Expected >= 10 providers, got {len(providers)}"


def test_list_provider_names():
    names = list_vibe_providers()
    assert "openai" in names
    assert "deepseek" in names


def test_get_provider_config():
    config = get_provider_config("openai")
    assert config is not None
    assert config.default_model
    assert config.default_base_url
    assert config.api_key_env


def test_provider_is_available_without_key():
    """无 API key 时 api_key_required=True 的 provider 不可用。"""
    ollama = get_provider_config("ollama")
    if ollama:
        assert ollama.is_available


def test_nonexistent_provider():
    config = get_provider_config("nonexistent_xyz")
    assert config is None


def test_provider_has_all_fields():
    """验证每个 provider 配置字段完整。"""
    providers = load_vibe_providers()
    for p in providers:
        assert p.name, f"Provider missing name"
        assert p.default_model, f"{p.name} missing default_model"
        if p.api_key_required:
            assert p.api_key_env, f"{p.name} missing api_key_env"

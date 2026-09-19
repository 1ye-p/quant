"""L3 strategy plugin tests (P3S-2 T5).

Covers: manifest parsing for the ``strategy`` capability, plugin-factory
resolution through a synthetic Registry, and StrategyLoader's plugin-source
load path (built-in map → Registry.resolve fallback).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl
import pytest

from cquant.execution.strategy_loader import StrategyLoader, get_strategy_class
from cquant.registry.manifest import PluginManifest
from cquant.registry.registry import Registry

REPO_ROOT = Path(__file__).resolve().parents[2].parent
EXAMPLE_PLUGIN_DIR = REPO_ROOT / "examples" / "plugins" / "my_strategy"

PLUGIN_MODULE = '''
from cquant.backtest_vector.strategy import Strategy, StrategyContext
import polars as pl


class ToyPluginStrategy(Strategy):
    def __init__(self, strategy_id: str = "toy", top_n: int = 3) -> None:
        self._id = strategy_id
        self.top_n = top_n

    @property
    def strategy_id(self) -> str:
        return self._id

    def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
        return pl.DataFrame()


def create_strategy(**kwargs):
    return ToyPluginStrategy(**kwargs)
'''


class FakeCatalog:
    """Stand-in Catalog serving one gold_backtest_runs config row."""

    def __init__(self, config: dict) -> None:
        self._config = config

    def query(self, sql: str, params: list) -> pl.DataFrame:
        return pl.DataFrame({
            "strategy_id": [params[0]],
            "config_json": [json.dumps(self._config)],
        })


@pytest.fixture()
def plugin_env(tmp_path, monkeypatch):
    """Synthetic plugin: manifest + importable module on sys.path."""
    plugin_dir = tmp_path / "toy_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "toy_plugin_mod.py").write_text(PLUGIN_MODULE, encoding="utf-8")
    manifest = {
        "name": "toy_strategy",
        "version": "0.1.0",
        "capabilities": ["strategy"],
        "entrypoints": {"strategy": "toy_plugin_mod:create_strategy"},
        "description": "synthetic test plugin",
    }
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

    registry = Registry()
    discovered = registry.discover([plugin_dir])
    assert len(discovered) == 1
    monkeypatch.syspath_prepend(str(plugin_dir))
    yield registry, manifest
    sys.path.remove(str(plugin_dir))
    sys.modules.pop("toy_plugin_mod", None)


class TestManifest:
    def test_example_plugin_manifest_parses(self) -> None:
        manifest = PluginManifest.from_file(EXAMPLE_PLUGIN_DIR / "plugin.json")
        assert "strategy" in manifest.capabilities
        assert manifest.entrypoints["strategy"].endswith(":create_strategy")

    def test_strategy_capability_accepted_by_registry(self, plugin_env) -> None:
        _registry, manifest = plugin_env
        assert "strategy" in manifest["capabilities"]  # validated by discover()


class TestLoaderPluginPath:
    def test_get_strategy_class_resolves_plugin_factory(self, plugin_env) -> None:
        registry, _ = plugin_env
        factory = get_strategy_class("toy_strategy", registry=registry)
        instance = factory(strategy_id="x", top_n=5)
        assert instance.strategy_id == "x"
        assert instance.top_n == 5

    def test_builtin_still_wins(self, plugin_env) -> None:
        registry, _ = plugin_env
        cls = get_strategy_class("StaticTopN", registry=registry)
        assert isinstance(cls, type)

    def test_unknown_raises_value_error(self, plugin_env) -> None:
        registry, _ = plugin_env
        with pytest.raises(ValueError, match="Unknown strategy type"):
            get_strategy_class("no_such_strategy", registry=registry)

    def test_loader_loads_from_plugin_source(self, plugin_env) -> None:
        registry, _ = plugin_env
        catalog = FakeCatalog({
            "strategy_type": "toy_strategy",
            "top_n": 7,
        })
        loader = StrategyLoader(catalog, registry=registry)
        strategy = loader.load("my_toy_backtest")
        assert strategy.strategy_id == "my_toy_backtest"
        assert strategy.top_n == 7

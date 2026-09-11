from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def bridge() -> ModuleType:
    path = Path(__file__).parents[1] / "plugins" / "claude-plugins-kit" / "scripts" / "bridge.py"
    return _load_module("codex_kit_bridge", path)


@pytest.fixture(scope="session")
def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "plugins" / "claude-plugins-kit" / "scripts" / "migration.py"
    return _load_module("codex_kit_migration_tests", path)

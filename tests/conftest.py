from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def bridge():
    path = Path(__file__).parents[1] / "plugins" / "claude-plugins-kit" / "scripts" / "bridge.py"
    spec = importlib.util.spec_from_file_location("codex_kit_bridge", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

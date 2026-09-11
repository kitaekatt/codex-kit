from __future__ import annotations

import json
from pathlib import Path

import pytest


def runtime_env(tmp_path: Path):
    root = tmp_path / "installed"
    root.mkdir()
    registry = tmp_path / "claude" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "alpha@test": [
                        {"scope": "user", "installPath": str(root), "installedAt": "2026-01-01"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return root, registry, {
        "CLAUDE_PLUGINS_REGISTRY": str(registry),
        "CLAUDE_SKILLS_ROOT": str(tmp_path / "claude" / "skills"),
        "CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "data"),
    }


def test_runtime_preserves_declared_venv_path_spelling(bridge, tmp_path):
    root, _, env = runtime_env(tmp_path)
    target = tmp_path / "venv" / "bin" / "python-real"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    link = target.parent / "python"
    link.symlink_to(target)
    env["ALPHA_VENV"] = str(link)
    info = bridge.runtime_info("alpha@test", env)
    assert info["source_root"] == str(root.resolve())
    assert info["python"] == str(link.absolute())


def test_runtime_rejects_ambiguous_plugin_and_script_escape(bridge, tmp_path):
    root, registry, env = runtime_env(tmp_path)
    document = json.loads(registry.read_text())
    document["plugins"]["alpha@other"] = document["plugins"]["alpha@test"]
    registry.write_text(json.dumps(document))
    with pytest.raises(bridge.BridgeError, match="ambiguous"):
        bridge.runtime_info("alpha", env)
    outside = tmp_path / "outside.py"
    outside.write_text("pass")
    with pytest.raises(bridge.BridgeError, match="escapes"):
        bridge.resolve_script(root, "../outside.py")


def test_personal_info_rejects_missing_and_symlinked_sources(bridge, tmp_path):
    _, _, env = runtime_env(tmp_path)
    skills = Path(env["CLAUDE_SKILLS_ROOT"])
    skills.mkdir(parents=True)
    with pytest.raises(bridge.BridgeError, match="missing"):
        bridge.personal_info("missing", env)
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: linked\n---\n")
    linked = skills / "linked"
    linked.mkdir()
    (linked / "SKILL.md").symlink_to(outside)
    with pytest.raises(bridge.BridgeError, match="symlink"):
        bridge.personal_info("linked", env)

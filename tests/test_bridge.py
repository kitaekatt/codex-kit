from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


class FakeRunner:
    def __init__(self, installed=None, marketplaces=None, failures=None):
        self.installed = installed or []
        self.marketplaces = marketplaces or []
        self.failures = {tuple(item) for item in (failures or [])}
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments):
        key = tuple(arguments)
        self.calls.append(key)
        if key in self.failures:
            return subprocess.CompletedProcess(arguments, 1, "", "deliberate failure")
        if key == ("plugin", "list", "--json"):
            output = json.dumps({"installed": self.installed, "available": []})
        elif key == ("plugin", "marketplace", "list", "--json"):
            output = json.dumps({"marketplaces": self.marketplaces})
        else:
            output = "{}"
        return subprocess.CompletedProcess(arguments, 0, output, "")


def write_skill(root: Path, folder: str, description="A source description.", body="CANONICAL SECRET BODY"):
    path = root / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {folder}\ndescription: {description}\nrequired_skills: ['md-read', 'skill-write']\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def source_fixture(tmp_path: Path, identities=("alpha@test",)):
    claude = tmp_path / "claude"
    registry = claude / "plugins" / "installed_plugins.json"
    personal = claude / "skills"
    write_skill(personal, "personal-one")
    records = {}
    roots = {}
    for index, identity in enumerate(identities):
        root = tmp_path / f"source-{index}"
        write_skill(root / "skills", "do-work")
        roots[identity] = root
        records[identity] = [
            {
                "scope": "user",
                "installPath": str(root),
                "installedAt": "2026-01-01T00:00:00Z",
            }
        ]
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"version": 2, "plugins": records}), encoding="utf-8")
    env = {
        "CLAUDE_PLUGINS_REGISTRY": str(registry),
        "CLAUDE_SKILLS_ROOT": str(personal),
        "CODEX_KIT_DATA_ROOT": str(tmp_path / "data"),
    }
    return env, registry, roots


def installed_record(name, marketplace, version="1.0.0"):
    return {
        "pluginId": f"{name}@{marketplace}",
        "name": name,
        "marketplaceName": marketplace,
        "version": version,
    }


def generated_records(bridge, env, *names):
    plugins = Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins"
    return [
        installed_record(
            name,
            bridge.GENERATED_MARKETPLACE,
            json.loads((plugins / name / "plugin.json").read_text())["version"],
        )
        for name in names
    ]


def generated_marketplace(bridge, env):
    root = Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE
    return [{"name": bridge.GENERATED_MARKETPLACE, "root": str(root)}]


def initial_sync(bridge, tmp_path):
    env, registry, roots = source_fixture(tmp_path)
    runner = FakeRunner()
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "changed", result
    return env, registry, roots, runner


def test_frontmatter_parser_supports_folded_legacy_and_ignores_complex_fields(bridge, tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: example\ndescription: >-\n  First line: with colon.\n  Second line.\nrequired_skills: ['one', 'two']\nmetadata:\n  nested: true\n---\n",
        encoding="utf-8",
    )
    fields = bridge.frontmatter_fields(path.read_text(), path)
    assert fields == {"name": "example", "description": "First line: with colon. Second line."}


def test_initial_sync_generates_forwarders_and_installs_native_plugins(bridge, tmp_path):
    env, _, _, runner = initial_sync(bridge, tmp_path)
    data = Path(env["CODEX_KIT_DATA_ROOT"])
    wrapper = data / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha" / "skills" / "do-work" / "SKILL.md"
    personal = data / bridge.GENERATED_MARKETPLACE / "plugins" / "claude-user" / "skills" / "personal-one" / "SKILL.md"
    assert wrapper.is_file() and personal.is_file()
    text = wrapper.read_text(encoding="utf-8")
    assert "CANONICAL SECRET BODY" not in text
    assert "claude-plugins-kit:claude-plugins" in text
    assert "/home/" not in text and "DEVROOT" not in text
    assert ("plugin", "marketplace", "add", str(data / bridge.GENERATED_MARKETPLACE), "--json") in runner.calls
    assert ("plugin", "add", f"alpha@{bridge.GENERATED_MARKETPLACE}", "--json") in runner.calls


def test_native_plugin_collision_wins_without_native_mutation(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    native = installed_record("alpha", "native-market")
    runner = FakeRunner(installed=[native])
    result = bridge.sync(env=env, runner=runner, install=True)
    assert "alpha" in result["skipped"]
    assert not (Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha").exists()
    assert not any(call[:2] == ("plugin", "remove") for call in runner.calls)
    assert not any(call[:3] == ("plugin", "add", f"alpha@{bridge.GENERATED_MARKETPLACE}") for call in runner.calls)


def test_two_claude_marketplaces_with_same_plugin_name_skip_both(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path, ("alpha@one", "alpha@two"))
    runner = FakeRunner()
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["skipped"] == ["alpha"]
    assert not any(call[:3] == ("plugin", "add", f"alpha@{bridge.GENERATED_MARKETPLACE}") for call in runner.calls)


def test_invalid_source_fails_closed_without_pruning(bridge, tmp_path):
    env, registry, _, _ = initial_sync(bridge, tmp_path)
    wrapper = Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha"
    before = (wrapper / "skills" / "do-work" / "SKILL.md").read_text(encoding="utf-8")
    registry.write_text("{bad", encoding="utf-8")
    runner = FakeRunner()
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "error"
    assert (wrapper / "skills" / "do-work" / "SKILL.md").read_text(encoding="utf-8") == before
    assert runner.calls == []


def test_failed_update_remains_owned_and_retries_install(bridge, tmp_path):
    env, _, roots, _ = initial_sync(bridge, tmp_path)
    data = Path(env["CODEX_KIT_DATA_ROOT"])
    installed = generated_records(bridge, env, "alpha", "claude-user")
    write_skill(roots["alpha@test"] / "skills", "do-work", "Updated description.")
    failure = ("plugin", "add", f"alpha@{bridge.GENERATED_MARKETPLACE}", "--json")
    failed = FakeRunner(installed=installed, marketplaces=generated_marketplace(bridge, env), failures=[failure])
    result = bridge.sync(env=env, runner=failed, install=True)
    assert result["status"] == "error"
    state = json.loads((data / "state.json").read_text(encoding="utf-8"))
    alpha = state["plugins"]["alpha"]
    assert alpha["source_digest"] != alpha["installed_digest"]

    retry = FakeRunner(installed=installed, marketplaces=generated_marketplace(bridge, env))
    result = bridge.sync(env=env, runner=retry, install=True)
    assert result["status"] == "changed"
    assert failure in retry.calls
    state = json.loads((data / "state.json").read_text(encoding="utf-8"))
    assert state["plugins"]["alpha"]["source_digest"] == state["plugins"]["alpha"]["installed_digest"]


def test_native_wrapper_version_rollback_forces_reinstall(bridge, tmp_path):
    env, _, _, _ = initial_sync(bridge, tmp_path)
    installed = generated_records(bridge, env, "alpha", "claude-user")
    installed[0]["version"] = "0.1.0+codex.rolled-back"
    runner = FakeRunner(installed=installed, marketplaces=generated_marketplace(bridge, env))
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "changed"
    assert ("plugin", "add", f"alpha@{bridge.GENERATED_MARKETPLACE}", "--json") in runner.calls


def test_claude_uninstall_removes_only_owned_wrapper(bridge, tmp_path):
    env, registry, _, _ = initial_sync(bridge, tmp_path)
    document = json.loads(registry.read_text(encoding="utf-8"))
    document["plugins"] = {}
    registry.write_text(json.dumps(document), encoding="utf-8")
    installed = generated_records(bridge, env, "alpha", "claude-user")
    runner = FakeRunner(installed=installed, marketplaces=generated_marketplace(bridge, env))
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "changed"
    assert ("plugin", "remove", f"alpha@{bridge.GENERATED_MARKETPLACE}", "--json") in runner.calls
    root = Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins"
    assert not (root / "alpha").exists()
    assert (root / "claude-user").exists()


def test_extra_file_blocks_update_before_any_native_mutation(bridge, tmp_path):
    env, _, roots, _ = initial_sync(bridge, tmp_path)
    data = Path(env["CODEX_KIT_DATA_ROOT"])
    wrapper = data / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha"
    (wrapper / "user-note.txt").write_text("preserve me", encoding="utf-8")
    write_skill(roots["alpha@test"] / "skills", "do-work", "Changed.")
    runner = FakeRunner(
        installed=generated_records(bridge, env, "alpha", "claude-user"),
        marketplaces=generated_marketplace(bridge, env),
    )
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "error"
    assert (wrapper / "user-note.txt").read_text() == "preserve me"
    assert not any(call[:2] in {("plugin", "add"), ("plugin", "remove")} for call in runner.calls)


def test_malicious_state_key_and_symlink_fail_closed(bridge, tmp_path):
    env, _, _, _ = initial_sync(bridge, tmp_path)
    data = Path(env["CODEX_KIT_DATA_ROOT"])
    state_path = data / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["plugins"]["../escape"] = {"source_identity": "evil", "source_digest": "x"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    runner = FakeRunner()
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "error"
    assert runner.calls == []

    state_path.unlink()
    state_path.symlink_to(tmp_path / "outside-state")
    with pytest.raises(bridge.BridgeError, match="symlink"):
        bridge.paths_for(env)


def test_data_root_inside_git_worktree_is_rejected(bridge, tmp_path):
    (tmp_path / ".git").mkdir()
    env = {"CODEX_KIT_DATA_ROOT": str(tmp_path / "generated")}
    with pytest.raises(bridge.BridgeError, match="Git worktree"):
        bridge.paths_for(env)


def test_malformed_native_listing_fails_before_generation(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)

    class BadRunner(FakeRunner):
        def run(self, arguments):
            self.calls.append(tuple(arguments))
            return subprocess.CompletedProcess(arguments, 0, '{"installed":[{"name":"alpha"}]}', "")

    result = bridge.sync(env=env, runner=BadRunner(), install=True)
    assert result["status"] == "error"
    assert not (Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE).exists()


def test_malformed_user_record_errors_but_project_only_plugin_is_skipped(bridge, tmp_path):
    env, registry, _ = source_fixture(tmp_path)
    document = json.loads(registry.read_text())
    document["plugins"]["bad@test"] = [{"scope": "user"}]
    registry.write_text(json.dumps(document))
    result = bridge.sync(env=env, runner=FakeRunner(), install=True)
    assert result["status"] == "error"

    document["plugins"]["bad@test"] = [
        {"scope": "project", "installPath": "/path/not-read-for-project-scope"}
    ]
    registry.write_text(json.dumps(document))
    runner = FakeRunner()
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "changed"
    assert not any("bad@" in " ".join(call) for call in runner.calls)


@pytest.mark.parametrize("manifest", [[], None, "not-an-object"])
def test_non_object_claude_manifest_is_invalid(bridge, tmp_path, manifest):
    root = tmp_path / "plugin"
    write_skill(root / "skills", "do-work")
    path = root / ".claude-plugin" / "plugin.json"
    path.parent.mkdir()
    path.write_text(json.dumps(manifest))
    with pytest.raises(bridge.BridgeError, match="must be an object"):
        bridge.skill_sources(root, "plugin")


def test_runtime_rejects_marketplace_path_traversal_before_data_join(bridge, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "alpha@../../escape": [
                        {"scope": "user", "installPath": str(root), "installedAt": "2026"}
                    ]
                },
            }
        )
    )
    env = {
        "CLAUDE_PLUGINS_REGISTRY": str(registry),
        "CLAUDE_SKILLS_ROOT": str(tmp_path / "skills"),
        "CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "data"),
    }
    with pytest.raises(bridge.BridgeError, match="identity"):
        bridge.runtime_info("alpha@../../escape", env)

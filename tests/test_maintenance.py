from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_bridge import FakeRunner, generated_marketplace, generated_records, initial_sync, installed_record, source_fixture, write_skill


def authored_record(enabled: bool = True) -> dict:
    manifest = Path(__file__).parents[1] / "plugins/claude-plugins-kit/.codex-plugin/plugin.json"
    record = installed_record("claude-plugins-kit", "codex-kit", json.loads(manifest.read_text())["version"])
    record["enabled"] = enabled
    return record


def test_maintenance_initial_catalog_needs_no_bootstrap_or_devroot(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    runner = FakeRunner(installed=[authored_record()], marketplaces=[])
    result = bridge.maintain(env=env, runner=runner)
    assert result["status"] == "changed", result
    assert ("plugin", "add", "alpha@claude-plugins-kit-generated", "--json") in runner.calls


def test_disabled_or_removed_bridge_performs_no_maintenance(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    for records in ([], [authored_record(False)]):
        runner = FakeRunner(installed=records)
        result = bridge.maintain(env=env, runner=runner)
        assert result["status"] == "unchanged", result
        assert runner.calls == [("plugin", "list", "--json")]
        assert not Path(env["CODEX_KIT_DATA_ROOT"]).exists()


def test_disabled_generated_plugin_is_not_reenabled_when_source_changes(bridge, tmp_path):
    env, _, roots, _ = initial_sync(bridge, tmp_path)
    records = generated_records(bridge, env, "alpha", "claude-user")
    records[0]["enabled"] = False
    old = Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins/alpha/skills/do-work/SKILL.md"
    before = old.read_bytes()
    write_skill(roots["alpha@test"] / "skills", "do-work", "New description")
    runner = FakeRunner(installed=records, marketplaces=generated_marketplace(bridge, env))
    result = bridge.sync(env=env, runner=runner, install=True, respect_disabled=True)
    assert result["status"] == "unchanged", result
    assert result["disabled"] == ["alpha"]
    assert old.read_bytes() == before
    assert not any(call[:3] == ("plugin", "add", "alpha@claude-plugins-kit-generated") for call in runner.calls)


def test_offline_release_check_does_not_prevent_local_catalog_sync(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    marketplace = {"name": "codex-kit", "root": str(tmp_path / "marketplace"), "marketplaceSource": {"sourceType": "git", "source": "https://github.com/kitaekatt/codex-kit.git"}}
    failure = ("plugin", "marketplace", "upgrade", "codex-kit", "--json")
    runner = FakeRunner(installed=[authored_record()], marketplaces=[marketplace], failures=[failure])
    result = bridge.maintain(env=env, runner=runner)
    assert result["status"] == "changed", result
    assert result["warnings"] and "release" in result["warnings"][0].lower()
    assert failure in runner.calls
    assert ("plugin", "add", "alpha@claude-plugins-kit-generated", "--json") in runner.calls
    assert json.loads((Path(env["CODEX_KIT_DATA_ROOT"]) / "maintenance.json").read_text())["last_attempt"] > 0


def test_nonofficial_marketplace_is_never_upgraded(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    marketplace = {"name": "codex-kit", "root": str(tmp_path / "foreign"), "marketplaceSource": {"sourceType": "git", "source": "https://example.com/foreign.git"}}
    runner = FakeRunner(installed=[authored_record()], marketplaces=[marketplace])
    result = bridge.maintain(env=env, runner=runner)
    assert result["warnings"]
    assert not any(call[:3] == ("plugin", "marketplace", "upgrade") for call in runner.calls)


def official_fixture(tmp_path):
    root = tmp_path / "official"
    repository = Path(__file__).parents[1]
    shutil.copytree(repository / "plugins/claude-plugins-kit", root / "plugins/claude-plugins-kit")
    (root / ".agents/plugins").mkdir(parents=True)
    shutil.copyfile(repository / ".agents/plugins/marketplace.json", root / ".agents/plugins/marketplace.json")
    return {"name": "codex-kit", "root": str(root), "marketplaceSource": {"sourceType": "git", "source": "https://github.com/kitaekatt/codex-kit.git"}}


def test_local_marketplace_is_never_upgraded(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    marketplace = {"name": "codex-kit", "root": str(tmp_path), "marketplaceSource": {"sourceType": "local"}}
    runner = FakeRunner(installed=[authored_record()], marketplaces=[marketplace])
    result = bridge.maintain(env=env, runner=runner)
    assert result["status"] == "changed"
    assert not result["warnings"]
    assert not any(call[:3] == ("plugin", "marketplace", "upgrade") for call in runner.calls)


@pytest.mark.parametrize("state", [[], {"schema": 2}, {"schema": 1, "last_attempt": float("nan"), "last_success": 0}])
def test_malformed_state_preserves_local_convergence(bridge, tmp_path, state):
    env, _, _ = source_fixture(tmp_path)
    root = Path(env["CODEX_KIT_DATA_ROOT"])
    root.mkdir()
    state_path = root / "maintenance.json"
    state_path.write_text(json.dumps(state))
    runner = FakeRunner(installed=[authored_record()], marketplaces=[official_fixture(tmp_path)])
    result = bridge.maintain(env=env, runner=runner)
    assert result["status"] == "changed"
    assert result["warnings"]
    assert state_path.read_text() == json.dumps(state)


def test_successful_release_check_is_throttled_and_can_be_forced(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    runner = FakeRunner(installed=[authored_record()], marketplaces=[official_fixture(tmp_path)])
    first = bridge.maintain(env=env, runner=runner)
    assert not first["warnings"], first
    check = ("plugin", "marketplace", "upgrade", "codex-kit", "--json")
    assert runner.calls.count(check) == 1
    bridge.maintain(env=env, runner=runner)
    assert runner.calls.count(check) == 1
    bridge.maintain(env=env, runner=runner, check_updates=True)
    assert runner.calls.count(check) == 2


@pytest.mark.parametrize("kind", ["escape", "shadow", "symlink", "duplicate", "missing"])
def test_invalid_candidate_never_installs(bridge, tmp_path, kind):
    env, _, _ = source_fixture(tmp_path)
    marketplace = official_fixture(tmp_path)
    root = Path(marketplace["root"])
    document_path = root / ".agents/plugins/marketplace.json"
    document = json.loads(document_path.read_text())
    candidate = root / "plugins/claude-plugins-kit"
    if kind == "escape":
        document["plugins"][0]["source"]["path"] = "../foreign"
    elif kind == "duplicate":
        document["plugins"].append(document["plugins"][0])
    elif kind == "shadow":
        (candidate / "plugin.json").write_text("{}")
    elif kind == "missing":
        (candidate / "scripts/maintenance.py").unlink()
    else:
        entry = candidate / "scripts/maintenance.py"
        entry.unlink()
        entry.symlink_to(Path(__file__))
    document_path.write_text(json.dumps(document))
    runner = FakeRunner(installed=[authored_record()], marketplaces=[marketplace])
    result = bridge.maintain(env=env, runner=runner)
    assert result["warnings"], result
    assert ("plugin", "add", "claude-plugins-kit@codex-kit", "--json") not in runner.calls


def test_bridge_disable_while_waiting_for_lock_stops_sync(bridge, tmp_path, monkeypatch):
    env, _, _ = source_fixture(tmp_path)
    runner = FakeRunner(installed=[authored_record()])
    original = bridge.FileLock
    class DisableOnLock(original):
        def __enter__(self):
            value = super().__enter__()
            runner.installed[0]["enabled"] = False
            return value
    monkeypatch.setattr(bridge, "FileLock", DisableOnLock)
    result = bridge.maintain(env=env, runner=runner)
    assert result["status"] == "unchanged"
    assert not any(call[:2] == ("plugin", "add") for call in runner.calls)


def test_lock_contention_leaves_catalog_untouched(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    runner = FakeRunner(installed=[authored_record()])
    with bridge.FileLock(Path(env["CODEX_KIT_DATA_ROOT"]) / ".maintenance.lock"):
        result = bridge.maintain(env=env, runner=runner)
    assert result["status"] == "error"
    assert not any(call[:2] == ("plugin", "add") for call in runner.calls)


@pytest.mark.parametrize("behavior", ["success", "failure", "disabled", "removed"])
def test_official_new_release_install_and_opt_out(bridge, tmp_path, monkeypatch, behavior):
    env, _, _ = source_fixture(tmp_path)
    env["CODEX_HOME"] = str(tmp_path / "codex-home")
    marketplace = official_fixture(tmp_path)
    candidate = Path(marketplace["root"]) / "plugins/claude-plugins-kit"
    manifest_path = candidate / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "9.0.0"
    manifest_path.write_text(json.dumps(manifest))
    installed_root = Path(env["CODEX_HOME"]) / "plugins/cache/codex-kit/claude-plugins-kit/9.0.0"
    shutil.copytree(candidate, installed_root)
    install = ("plugin", "add", "claude-plugins-kit@codex-kit", "--json")
    class ReleaseRunner(FakeRunner):
        def run(self, arguments):
            result = super().run(arguments)
            if tuple(arguments) == ("plugin", "marketplace", "upgrade", "codex-kit", "--json"):
                if behavior == "disabled":
                    self.installed[0]["enabled"] = False
                if behavior == "removed":
                    self.installed = []
            if tuple(arguments) == install:
                if behavior == "failure":
                    return subprocess.CompletedProcess(arguments, 1, "", "offline install")
                return subprocess.CompletedProcess(arguments, 0, json.dumps({"pluginId": "claude-plugins-kit@codex-kit", "version": "9.0.0", "installedPath": str(installed_root)}), "")
            return result
    runner = ReleaseRunner(installed=[authored_record()], marketplaces=[marketplace])
    original_run = subprocess.run
    calls = []
    def refresh(arguments, **kwargs):
        if str(installed_root) in str(arguments[0]):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"status": "unchanged", "changed": False, "message": "Current."}), "")
        return original_run(arguments, **kwargs)
    monkeypatch.setattr(subprocess, "run", refresh)
    result = bridge.maintain(env=env, runner=runner)
    if behavior == "success":
        assert result["bridge_updated"] is True, result
        assert calls and "--respect-disabled" in calls[0]
        assert not result["warnings"]
    elif behavior == "failure":
        assert result["warnings"]
        assert not calls
        state = json.loads((Path(env["CODEX_KIT_DATA_ROOT"]) / "maintenance.json").read_text())
        assert state["last_success"] == 0
    else:
        assert install not in runner.calls
        assert not calls


def test_failed_release_check_uses_retry_cooldown(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    check = ("plugin", "marketplace", "upgrade", "codex-kit", "--json")
    runner = FakeRunner(installed=[authored_record()], marketplaces=[official_fixture(tmp_path)], failures=[check])
    first = bridge.maintain(env=env, runner=runner)
    assert first["warnings"]
    bridge.maintain(env=env, runner=runner)
    assert runner.calls.count(check) == 1
    state_path = Path(env["CODEX_KIT_DATA_ROOT"]) / "maintenance.json"
    state = json.loads(state_path.read_text())
    state["last_attempt"] -= 16 * 60
    state_path.write_text(json.dumps(state))
    bridge.maintain(env=env, runner=runner)
    assert runner.calls.count(check) == 2

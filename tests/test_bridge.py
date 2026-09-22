from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest


def test_runner_preserves_explicit_path_and_arguments_with_spaces(bridge, tmp_path):
    executable = tmp_path / "path with spaces" / "codex.exe"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")
    observed = {}

    def fake_run(arguments, **kwargs):
        observed["arguments"] = arguments
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(arguments, 0, "ok", "")

    original = subprocess.run
    subprocess.run = fake_run
    try:
        result = bridge.Runner(str(executable)).run(["--flag", "value with spaces"])
    finally:
        subprocess.run = original
    assert result.stdout == "ok"
    assert observed["arguments"] == [str(executable), "--flag", "value with spaces"]
    assert observed["kwargs"]["shell"] is False


def test_runner_missing_executable_is_actionable(bridge):
    with pytest.raises(bridge.BridgeError, match="CODEX_KIT_CODEX_BIN"):
        bridge.Runner("does-not-exist-codex").run(["--version"])


def test_default_discovery_prefers_environment_override(bridge, monkeypatch):
    monkeypatch.setenv("CODEX_KIT_CODEX_BIN", r"C:\Path With Spaces\override.exe")
    monkeypatch.setattr(bridge.shutil, "which", lambda _: pytest.fail("PATH must not win"))
    observed = {}
    monkeypatch.setattr(bridge.subprocess, "run", lambda arguments, **kwargs: observed.update(arguments=arguments, kwargs=kwargs) or subprocess.CompletedProcess(arguments, 0, "", ""))
    bridge.Runner().run(["--flag", "value with spaces"])
    assert observed["arguments"] == [r"C:\Path With Spaces\override.exe", "--flag", "value with spaces"]
    assert observed["kwargs"]["shell"] is False


def test_default_discovery_uses_path_result(bridge, monkeypatch):
    monkeypatch.delenv("CODEX_KIT_CODEX_BIN", raising=False)
    monkeypatch.setattr(bridge.shutil, "which", lambda name: r"C:\Tools\codex.exe")
    observed = {}
    monkeypatch.setattr(bridge.subprocess, "run", lambda arguments, **kwargs: observed.update(arguments=arguments, kwargs=kwargs) or subprocess.CompletedProcess(arguments, 0, "", ""))
    bridge.Runner().run(["plugin", "list", "--json"])
    assert observed["arguments"] == [r"C:\Tools\codex.exe", "plugin", "list", "--json"]
    assert observed["kwargs"]["shell"] is False


def test_windows_discovery_selects_newest_install(bridge, monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_KIT_CODEX_BIN", raising=False)
    monkeypatch.setattr(bridge.shutil, "which", lambda _: None)
    root = tmp_path / "Local App Data" / "OpenAI" / "Codex" / "bin"
    older = root / "older" / "codex.exe"
    newer = root / "newer" / "codex.exe"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_text("")
    newer.write_text("")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local App Data"))
    monkeypatch.setattr(bridge, "os", SimpleNamespace(name="nt", environ=dict(os.environ)))
    assert bridge.Runner()._executable() == str(newer)


@pytest.mark.parametrize("local_app_data", [None, ""])
def test_windows_discovery_without_local_appdata_is_actionable(bridge, monkeypatch, local_app_data):
    monkeypatch.delenv("CODEX_KIT_CODEX_BIN", raising=False)
    monkeypatch.setattr(bridge.shutil, "which", lambda _: None)
    if local_app_data is None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    else:
        monkeypatch.setenv("LOCALAPPDATA", local_app_data)
    monkeypatch.setattr(bridge, "os", SimpleNamespace(name="nt", environ=dict(os.environ)))
    with pytest.raises(bridge.BridgeError, match="CODEX_KIT_CODEX_BIN"):
        bridge.Runner()._executable()


def test_windows_discovery_without_install_directory_is_actionable(bridge, monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_KIT_CODEX_BIN", raising=False)
    monkeypatch.setattr(bridge.shutil, "which", lambda _: None)
    monkeypatch.setattr(bridge, "os", SimpleNamespace(
        name="nt", environ={**os.environ, "LOCALAPPDATA": str(tmp_path / "missing")}
    ))
    with pytest.raises(bridge.BridgeError, match="CODEX_KIT_CODEX_BIN"):
        bridge.Runner()._executable()


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
    assert "info alpha@test do-work" in text
    assert "/home/" not in text and "DEVROOT" not in text
    report = json.loads((wrapper.parent.parent.parent / "compatibility" / "do-work.json").read_text())
    assert report["schema"] == 1
    assert report["source"]["identity"] == "alpha@test"
    assert report["generation"] in text
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


def test_source_change_during_scaffold_does_not_publish_a_mixed_generation(bridge, tmp_path, monkeypatch):
    env, _, roots, runner = initial_sync(bridge, tmp_path)
    wrapper = Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha"
    prior_wrapper = (wrapper / "skills/do-work/SKILL.md").read_text()
    prior_report = (wrapper / "compatibility/do-work.json").read_text()
    write_skill(roots["alpha@test"] / "skills", "do-work", "Changed before refresh.")
    original_check = bridge.check_owned_tree
    changed = False

    def race_after_validation(path):
        nonlocal changed
        original_check(path)
        if not changed and path.name == "alpha":
            write_skill(roots["alpha@test"] / "skills", "do-work", "Changed during refresh.")
            changed = True

    monkeypatch.setattr(bridge, "check_owned_tree", race_after_validation)
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "error"
    assert "source changed during scaffold refresh" in result["message"]
    assert (wrapper / "skills/do-work/SKILL.md").read_text() == prior_wrapper
    assert (wrapper / "compatibility/do-work.json").read_text() == prior_report


def test_source_change_between_discovery_and_analysis_is_rejected(bridge, tmp_path, monkeypatch):
    env, _, roots = source_fixture(tmp_path)
    original_discovery = bridge.installed_sources

    def mutate_after_discovery(registry, personal):
        sources, duplicates = original_discovery(registry, personal)
        write_skill(roots["alpha@test"] / "skills", "do-work", "Changed after discovery.")
        return sources, duplicates

    monkeypatch.setattr(bridge, "installed_sources", mutate_after_discovery)
    result = bridge.sync(env=env, runner=FakeRunner(), install=True)
    assert result["status"] == "error"
    assert "source changed during scaffold discovery" in result["message"]
    generated = Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha"
    assert not generated.exists()


def test_invalid_local_inference_override_keeps_deterministic_report(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    data_root = Path(env["CODEX_KIT_DATA_ROOT"])
    data_root.mkdir(parents=True)
    (data_root / "inference-config.json").write_text(json.dumps({
        "inference": {"model": "gpt-5.6-sol", "effort": "unsupported"},
    }))
    registry = Path(env["CLAUDE_PLUGINS_REGISTRY"])
    source_root = Path(json.loads(registry.read_text())["plugins"]["alpha@test"][0]["installPath"])
    skill_path = source_root / "skills/do-work/SKILL.md"
    skill_path.write_text(
        "\n".join([
            "---", "name: do-work", "description: test", "required_capabilities:",
            "  - terminal.run", "---", "Requires a browser session to inspect pages.", "",
        ]),
        encoding="utf-8",
    )
    result = bridge.sync(env=env, runner=FakeRunner(), install=True)
    assert result["status"] == "changed"
    report_path = data_root / bridge.GENERATED_MARKETPLACE / "plugins/alpha/compatibility/do-work.json"
    report = json.loads(report_path.read_text())
    assert report["requirements"][0]["id"] == "terminal.run"
    assert report["inference"]["status"] == "deferred"
    assert report["inference"]["requested_model"] == "gpt-5.6-sol"
    assert report["inference"]["requested_effort"] == "unsupported"


def test_malformed_local_inference_config_keeps_deterministic_report(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    data_root = Path(env["CODEX_KIT_DATA_ROOT"])
    data_root.mkdir(parents=True)
    (data_root / "inference-config.json").write_text("{malformed", encoding="utf-8")
    registry = Path(env["CLAUDE_PLUGINS_REGISTRY"])
    source_root = Path(json.loads(registry.read_text())["plugins"]["alpha@test"][0]["installPath"])
    (source_root / "skills/do-work/SKILL.md").write_text(
        "\n".join([
            "---", "name: do-work", "required_capabilities:", "  - terminal.run", "---",
            "Requires a browser session to inspect pages.", "",
        ]), encoding="utf-8",
    )

    result = bridge.sync(env=env, runner=FakeRunner(), install=True)

    assert result["status"] == "changed"
    report_path = data_root / bridge.GENERATED_MARKETPLACE / "plugins/alpha/compatibility/do-work.json"
    report = json.loads(report_path.read_text())
    assert report["requirements"][0]["id"] == "terminal.run"
    assert report["inference"]["status"] == "deferred"
    assert "invalid compatibility JSON" in report["inference"]["reason"]


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

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
    (claude / "settings.json").write_text(json.dumps({"enabledPlugins": dict.fromkeys(identities, True)}))
    env = {
        "CLAUDE_CONFIG_DIR": str(claude),
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


def test_enabled_user_plugins_only_and_disabled_stale_paths_are_ignored(bridge, tmp_path):
    env, registry, roots = source_fixture(tmp_path, ("alpha@test", "disabled@test", "project@test"))
    settings = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings.write_text(json.dumps({"enabledPlugins": {
        "alpha@test": True, "disabled@test": False, "project@test": True,
    }}))
    document = json.loads(registry.read_text())
    document["plugins"]["disabled@test"][0]["installPath"] = str(tmp_path / "missing")
    document["plugins"]["project@test"][0]["scope"] = "project"
    registry.write_text(json.dumps(document))
    sources, duplicates = bridge.installed_sources(registry, Path(env["CLAUDE_SKILLS_ROOT"]), settings)
    assert [source.identity for source in sources] == ["alpha@test", "claude-user"]
    assert sources[0].root == roots["alpha@test"]
    assert duplicates == []


def test_registry_override_keeps_selected_config_enablement(bridge, tmp_path):
    env, registry, _ = source_fixture(tmp_path)
    moved = tmp_path / "override" / "registry.json"
    moved.parent.mkdir()
    moved.write_text(registry.read_text())
    env["CLAUDE_PLUGINS_REGISTRY"] = str(moved)
    (moved.parent / "settings.json").write_text(json.dumps({"enabledPlugins": {"alpha@test": False}}))
    result = bridge.sync(env=env, runner=FakeRunner(), install=True)
    assert result["status"] == "changed", result
    assert "alpha" in json.loads((Path(env["CODEX_KIT_DATA_ROOT"]) / "state.json").read_text())["plugins"]


@pytest.mark.parametrize("identity, records", [
    ("invalid-identity", [{"scope": "user", "installPath": "/missing"}]),
    ("disabled@test", []),
    ("disabled@test", [{"scope": "user"}]),
])
def test_disabled_registry_records_still_require_valid_structure(bridge, tmp_path, identity, records):
    env, registry, _ = source_fixture(tmp_path)
    document = json.loads(registry.read_text())
    document["plugins"][identity] = records
    registry.write_text(json.dumps(document))
    result = bridge.sync(env=env, runner=FakeRunner(), install=True)
    assert result["status"] == "error"
    assert "invalid" in result["message"]


@pytest.mark.parametrize("missing_record", [True, False])
def test_enabled_missing_sources_fail_without_pruning(bridge, tmp_path, missing_record):
    env, registry, roots, _ = initial_sync(bridge, tmp_path)
    if missing_record:
        registry.write_text(json.dumps({"version": 2, "plugins": {}}))
    else:
        roots["alpha@test"].rename(tmp_path / "moved-source")
    runner = FakeRunner(installed=generated_records(bridge, env, "alpha", "claude-user"))
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "error"
    assert ("no installed record" if missing_record else "source is missing") in result["message"]
    assert not any(call[:2] == ("plugin", "remove") for call in runner.calls)


def test_disabling_claude_plugin_removes_owned_native_wrapper(bridge, tmp_path):
    env, _, _, _ = initial_sync(bridge, tmp_path)
    settings = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings.write_text(json.dumps({"enabledPlugins": {"alpha@test": False}}))
    runner = FakeRunner(
        installed=generated_records(bridge, env, "alpha", "claude-user"),
        marketplaces=generated_marketplace(bridge, env),
    )
    result = bridge.sync(env=env, runner=runner, install=True, respect_disabled=True)
    assert result["status"] == "changed", result
    assert ("plugin", "remove", "alpha@claude-plugins-kit-generated", "--json") in runner.calls
    assert not (Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha").exists()


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


def test_user_config_excludes_plugin_before_wrapper_generation(bridge, tmp_path):
    env, _, roots = source_fixture(tmp_path, ("claude-ui-kit@plugins-kit",))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "claude-plugins-kit.json").write_text(
        json.dumps({"excluded_plugins": ["claude-ui-kit@plugins-kit"]}), encoding="utf-8"
    )
    env["CODEX_HOME"] = str(codex_home)
    result = bridge.sync(env=env, runner=FakeRunner(), install=True)
    assert result["status"] == "changed", result
    assert result["skipped"] == ["claude-ui-kit"]
    assert not (Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "claude-ui-kit").exists()
    assert roots["claude-ui-kit@plugins-kit"].exists()


def test_plugin_metadata_excludes_wrapper_without_user_config(bridge, tmp_path):
    env, _, roots = source_fixture(tmp_path, ("claude-ui-kit@plugins-kit",))
    (roots["claude-ui-kit@plugins-kit"] / ".claude-plugin").mkdir()
    (roots["claude-ui-kit@plugins-kit"] / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "claude-ui-kit", "codex-kit": {"wrap": False}}), encoding="utf-8"
    )
    env["CODEX_HOME"] = str(tmp_path / "codex-home-without-guidance")
    result = bridge.sync(env=env, runner=FakeRunner(), install=True)
    assert result["status"] == "changed", result
    assert result["skipped"] == ["claude-ui-kit"]
    assert not (Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "claude-ui-kit").exists()


def native_fixture(env: dict[str, str], tmp_path: Path, *, skills: tuple[str, ...] = ("do-work",),
                   enabled: bool = True, version: str = "1.0.0") -> tuple[dict[str, object], Path]:
    home = tmp_path / "codex-home"
    env["CODEX_HOME"] = str(home)
    root = home / "plugins" / "cache" / "native-market" / "alpha" / version
    manifest = root / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": "alpha", "version": version, "skills": "./skills/"}))
    (root / "skills").mkdir(exist_ok=True)
    for name in skills:
        write_skill(root / "skills", name)
    return {**installed_record("alpha", "native-market", version), "enabled": enabled}, root


def test_native_plugin_covers_only_actual_skills_without_native_mutation(bridge, tmp_path):
    env, _, roots = source_fixture(tmp_path)
    native, native_root = native_fixture(env, tmp_path)
    write_skill(roots["alpha@test"] / "skills", "new-skill")
    runner = FakeRunner(installed=[native])
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "changed", result
    assert "alpha" not in result["skipped"]
    wrapper = Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha"
    assert (wrapper / "skills" / "new-skill" / "SKILL.md").is_file()
    assert not (wrapper / "skills" / "do-work").exists()
    assert result["native_skills"]["alpha"][0]["skills"] == ["do-work"]
    assert (native_root / "skills" / "do-work" / "SKILL.md").is_file()
    assert not any(call[:2] == ("plugin", "remove") for call in runner.calls)
    assert ("plugin", "add", f"alpha@{bridge.GENERATED_MARKETPLACE}", "--json") in runner.calls


def test_fully_native_covered_plugin_is_reported_without_empty_wrapper(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    native, _ = native_fixture(env, tmp_path)
    result = bridge.sync(env=env, runner=FakeRunner(installed=[native]), install=True)
    assert result["status"] == "changed", result
    assert "alpha" not in result["skipped"]
    assert result["forwarded_skills"]["alpha"] == []
    assert result["native_skills"]["alpha"][0]["enabled"] is True
    assert not (Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha").exists()


def test_disabled_native_reserves_its_names_but_does_not_hide_other_skills(bridge, tmp_path):
    env, _, roots = source_fixture(tmp_path)
    native, _ = native_fixture(env, tmp_path, enabled=False)
    write_skill(roots["alpha@test"] / "skills", "new-skill")
    result = bridge.sync(env=env, runner=FakeRunner(installed=[native]), install=True)
    assert result["status"] == "changed", result
    assert result["native_skills"]["alpha"][0]["enabled"] is False
    assert result["forwarded_skills"]["alpha"] == ["new-skill"]
    assert "disabled native" in result["message"].lower()


def test_native_version_or_skill_content_change_refreshes_complement(bridge, tmp_path):
    env, _, roots = source_fixture(tmp_path)
    native, native_root = native_fixture(env, tmp_path)
    write_skill(roots["alpha@test"] / "skills", "new-skill")
    first = bridge.sync(env=env, runner=FakeRunner(installed=[native]), install=True)
    assert first["status"] == "changed", first
    installed = [native, *generated_records(bridge, env, "alpha", "claude-user")]
    write_skill(native_root / "skills", "do-work", "Changed installed native instructions.")
    runner = FakeRunner(installed=installed, marketplaces=generated_marketplace(bridge, env))
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["catalog_changed"] is True, result
    assert ("plugin", "add", f"alpha@{bridge.GENERATED_MARKETPLACE}", "--json") in runner.calls


@pytest.mark.parametrize("corruption", ["missing-root", "bad-manifest", "wrong-version", "escape", "missing-skill-name"])
def test_invalid_native_metadata_fails_without_pruning(bridge, tmp_path, corruption):
    env, _, roots, _ = initial_sync(bridge, tmp_path)
    native, root = native_fixture(env, tmp_path)
    manifest = root / ".codex-plugin" / "plugin.json"
    if corruption == "missing-root":
        native["version"] = "missing"
    elif corruption == "bad-manifest":
        manifest.write_text("{bad")
    elif corruption == "wrong-version":
        manifest.write_text(json.dumps({"name": "alpha", "version": "wrong"}))
    elif corruption == "escape":
        manifest.write_text(json.dumps({"name": "alpha", "version": "1.0.0", "skills": "../../../../"}))
    else:
        (root / "skills" / "do-work" / "SKILL.md").write_text("---\ndescription: no name\n---\n")
    wrapper = Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha"
    before = (wrapper / "skills" / "do-work" / "SKILL.md").read_bytes()
    runner = FakeRunner(installed=[native, *generated_records(bridge, env, "alpha", "claude-user")],
                        marketplaces=generated_marketplace(bridge, env))
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "error", result
    assert (wrapper / "skills" / "do-work" / "SKILL.md").read_bytes() == before
    assert not any(call[:2] in {("plugin", "add"), ("plugin", "remove")} for call in runner.calls)


def test_native_frontmatter_name_not_folder_determines_precedence(bridge, tmp_path):
    env, _, roots = source_fixture(tmp_path)
    native, root = native_fixture(env, tmp_path)
    write_skill(roots["alpha@test"] / "skills", "actual-native-name")
    path = root / "skills" / "do-work" / "SKILL.md"
    path.write_text(path.read_text().replace("name: do-work", "name: actual-native-name"))
    result = bridge.sync(env=env, runner=FakeRunner(installed=[native]), install=True)
    assert result["forwarded_skills"]["alpha"] == ["do-work"]
    assert result["native_skills"]["alpha"][0]["reserved_skills"] == ["actual-native-name"]


def test_partial_native_coverage_tracks_new_moved_and_deleted_claude_skills(bridge, tmp_path):
    env, registry, roots = source_fixture(tmp_path, ("alpha@test", "beta@test"))
    native, _ = native_fixture(env, tmp_path)
    source = roots["alpha@test"] / "skills"
    write_skill(source, "new-skill")
    first = bridge.sync(env=env, runner=FakeRunner(installed=[native]), install=True)
    assert first["forwarded_skills"]["alpha"] == ["new-skill"]
    installed = [native, *generated_records(bridge, env, "alpha", "beta", "claude-user")]
    (source / "new-skill" / "SKILL.md").unlink()
    write_skill(roots["beta@test"] / "skills", "new-skill")
    runner = FakeRunner(installed=installed, marketplaces=generated_marketplace(bridge, env))
    moved = bridge.sync(env=env, runner=runner, install=True)
    assert moved["catalog_changed"] is True
    assert moved["forwarded_skills"]["alpha"] == []
    assert moved["forwarded_skills"]["beta"] == ["do-work", "new-skill"]
    assert ("plugin", "remove", f"alpha@{bridge.GENERATED_MARKETPLACE}", "--json") in runner.calls
    assert not any(call[:3] == ("plugin", "remove", "alpha@native-market") for call in runner.calls)


@pytest.mark.parametrize("native_enabled", [True, False])
def test_disabled_generated_complement_is_never_reenabled(bridge, tmp_path, native_enabled):
    env, _, roots = source_fixture(tmp_path)
    native, _ = native_fixture(env, tmp_path, enabled=native_enabled)
    write_skill(roots["alpha@test"] / "skills", "new-skill")
    bridge.sync(env=env, runner=FakeRunner(installed=[native]), install=True)
    installed = [native, *generated_records(bridge, env, "alpha", "claude-user")]
    installed[1]["enabled"] = False
    runner = FakeRunner(installed=installed, marketplaces=generated_marketplace(bridge, env))
    result = bridge.sync(env=env, runner=runner, install=True, respect_disabled=True)
    assert result["disabled"] == ["alpha"]
    assert not any(call[:3] == ("plugin", "add", f"alpha@{bridge.GENERATED_MARKETPLACE}") for call in runner.calls)


def test_disabled_native_does_not_disable_enabled_generated_complement(bridge, tmp_path):
    env, _, roots = source_fixture(tmp_path)
    native, _ = native_fixture(env, tmp_path, enabled=False)
    write_skill(roots["alpha@test"] / "skills", "new-skill")
    bridge.sync(env=env, runner=FakeRunner(installed=[native]), install=True)
    installed = [native, *generated_records(bridge, env, "alpha", "claude-user")]
    result = bridge.sync(env=env, runner=FakeRunner(installed=installed,
                         marketplaces=generated_marketplace(bridge, env)), install=True, respect_disabled=True)
    assert result["disabled"] == []
    assert result["forwarded_skills"]["alpha"] == ["new-skill"]


def test_moved_override_warns_without_guessing_or_inheriting(bridge, tmp_path):
    env, _, roots = source_fixture(tmp_path)
    write_skill(roots["alpha@test"] / "skills", "moved")
    sources, _ = bridge.installed_sources(Path(env["CLAUDE_PLUGINS_REGISTRY"]),
                                       Path(env["CLAUDE_SKILLS_ROOT"]), Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json")
    old = {("old@test", "moved"): {"status": "claude-only", "instructions": "old boundary"}}
    warnings = bridge.moved_override_warnings(sources, old)
    assert warnings == ["Compatibility mapping may have moved: old@test/moved -> alpha@test/moved."]
    target = next(source for source in sources if source.name == "alpha")
    assert bridge.override_for(old, target, "moved") is None
    old[("alpha@test", "moved")] = {"status": "adapted", "instructions": "mapped explicitly"}
    assert bridge.moved_override_warnings(sources, old) == []
    assert bridge.moved_override_warnings(sources, {("old@test", "absent"): old[("old@test", "moved")]}) == []


def test_preview_reports_disabled_native_and_native_only_skills(bridge, tmp_path):
    env, _, _ = source_fixture(tmp_path)
    native, _ = native_fixture(env, tmp_path, skills=("do-work", "native-only"), enabled=False)
    result = bridge.sync(env=env, runner=FakeRunner(installed=[native]), install=False)
    assert "disabled native" in result["message"].lower()
    assert "Native-only skills" in result["message"]
    coverage = result["native_skills"]["alpha"][0]
    assert coverage["source_identity"] == "alpha@test"
    assert coverage["reserved_skills"] == ["do-work"]
    assert coverage["extra_skills"] == ["native-only"]


def test_native_coverage_race_does_not_publish_a_stale_complement(bridge, tmp_path):
    env, _, roots = source_fixture(tmp_path)
    native, root = native_fixture(env, tmp_path)
    write_skill(roots["alpha@test"] / "skills", "new-skill")
    bridge.sync(env=env, runner=FakeRunner(installed=[native]), install=True)
    installed = [native, *generated_records(bridge, env, "alpha", "claude-user")]
    write_skill(roots["alpha@test"] / "skills", "new-skill", "Changed Claude source.")
    class ChangingNative(FakeRunner):
        listings = 0
        def run(self, arguments):
            if tuple(arguments) == ("plugin", "list", "--json"):
                self.listings += 1
                if self.listings == 2:
                    write_skill(root / "skills", "new-skill", "Native gained this skill.")
            return super().run(arguments)
    runner = ChangingNative(installed=installed, marketplaces=generated_marketplace(bridge, env))
    wrapper = Path(env["CODEX_KIT_DATA_ROOT"]) / bridge.GENERATED_MARKETPLACE / "plugins" / "alpha"
    before = (wrapper / "plugin.json").read_bytes()
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["status"] == "error", result
    assert "native skill coverage changed" in result["message"]
    assert (wrapper / "plugin.json").read_bytes() == before
    assert not any(call[:2] in {("plugin", "add"), ("plugin", "remove")} for call in runner.calls)


def test_ambiguous_moved_override_reports_candidates_without_inheritance(bridge, tmp_path):
    env, _, roots = source_fixture(tmp_path, ("alpha@test", "beta@test"))
    for root in roots.values():
        write_skill(root / "skills", "moved")
    sources, _ = bridge.installed_sources(Path(env["CLAUDE_PLUGINS_REGISTRY"]),
                                       Path(env["CLAUDE_SKILLS_ROOT"]), Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json")
    old = {("old@test", "moved"): {"status": "claude-only", "instructions": "old boundary"}}
    warnings = bridge.moved_override_warnings(sources, old)
    assert len(warnings) == 1
    assert "ambiguous destinations" in warnings[0]
    assert "alpha@test/moved" in warnings[0] and "beta@test/moved" in warnings[0]
    assert all(bridge.override_for(old, source, "moved") is None for source in sources)


def test_manual_sync_defaults_to_preserving_disabled_generated_plugin(bridge, tmp_path):
    env, _, roots, _ = initial_sync(bridge, tmp_path)
    installed = generated_records(bridge, env, "alpha", "claude-user")
    installed[0]["enabled"] = False
    write_skill(roots["alpha@test"] / "skills", "do-work", "Changed while disabled.")
    runner = FakeRunner(installed=installed, marketplaces=generated_marketplace(bridge, env))
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["disabled"] == ["alpha"]
    assert result["status"] == "unchanged"
    assert not any(call[:3] == ("plugin", "add", f"alpha@{bridge.GENERATED_MARKETPLACE}") for call in runner.calls)


def test_unflagged_sync_command_preserves_disabled_by_default(bridge, monkeypatch):
    observed = {}
    def sync(**kwargs):
        observed.update(kwargs)
        return {"status": "unchanged", "message": "Current."}
    monkeypatch.setattr(bridge, "sync", sync)
    assert bridge.main(["sync", "--install"]) == 0
    assert observed["respect_disabled"] is True


def test_concurrent_user_disable_is_rechecked_before_install(bridge, tmp_path):
    env, _, roots, _ = initial_sync(bridge, tmp_path)
    installed = generated_records(bridge, env, "alpha", "claude-user")
    write_skill(roots["alpha@test"] / "skills", "do-work", "Changed source.")
    class DisablingRunner(FakeRunner):
        listings = 0
        def run(self, arguments):
            if tuple(arguments) == ("plugin", "list", "--json"):
                self.listings += 1
                if self.listings == 3:
                    self.installed[0]["enabled"] = False
            return super().run(arguments)
    runner = DisablingRunner(installed=installed, marketplaces=generated_marketplace(bridge, env))
    result = bridge.sync(env=env, runner=runner, install=True)
    assert result["disabled"] == ["alpha"]
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

    def mutate_after_discovery(registry, personal, settings):
        sources, duplicates = original_discovery(registry, personal, settings)
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
    (Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text(json.dumps({"enabledPlugins": {}}))
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
    (Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"alpha@test": True, "bad@test": True}})
    )
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

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest


LEGACY_GENERATOR = "plugins-kit/sync_plugins_kit.py"


class InstallFailureRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        call = tuple(arguments)
        self.calls.append(call)
        if call == ("plugin", "list", "--json"):
            return subprocess.CompletedProcess(arguments, 0, '{"installed":[],"available":[]}', "")
        if call == ("plugin", "marketplace", "list", "--json"):
            return subprocess.CompletedProcess(arguments, 0, '{"marketplaces":[]}', "")
        if call[:3] == ("plugin", "marketplace", "add"):
            return subprocess.CompletedProcess(arguments, 0, '{"marketplaceName":"codex-kit"}', "")
        if call[:2] == ("plugin", "add"):
            return subprocess.CompletedProcess(arguments, 1, "", "deliberate install failure")
        raise AssertionError(f"unexpected call: {call!r}")


def legacy_fixture(tmp_path: Path) -> tuple[dict[str, str], list[Path]]:
    codex_home = tmp_path / "codex-home"
    skill = codex_home / "skills" / "plugins-kit-awesome-do-work" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\n"
        "name: plugins-kit-awesome-do-work\n"
        "description: Legacy forwarder.\n"
        "metadata:\n"
        "  plugins-kit-stub: '1'\n"
        f"  generator: {LEGACY_GENERATOR}\n"
        "  source-kind: plugin\n"
        "  source-identity: awesome@plugins-kit/do-work\n"
        "---\n\n"
        "Read deleted plugins-kit-runtime.md.\n",
        encoding="utf-8",
    )
    hook = codex_home / "hooks.json"
    hook.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "^(startup|resume)$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f'python "{codex_home / "scripts" / "startup_hook.py"}"',
                                    "statusMessage": "Synchronizing shared skills",
                                    "timeout": 30,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    state = codex_home / ".plugins-kit-sync-state.json"
    state.write_text(
        json.dumps(
            {
                "content": hashlib.sha256(b"content").hexdigest(),
                "registry": hashlib.sha256(b"registry").hexdigest(),
                "claude_head": "a" * 40,
            }
        ),
        encoding="utf-8",
    )
    env = {
        "CODEX_HOME": str(codex_home),
        "CODEX_KIT_DATA_ROOT": str(tmp_path / "data"),
    }
    return env, [hook, skill, state]


def add_claude_source(env: dict[str, str], tmp_path: Path) -> None:
    source = tmp_path / "claude-cache" / "awesome" / "1.0.0"
    skill = source / "skills" / "do-work" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: do-work\ndescription: Do work.\n---\n\nCanonical.\n",
        encoding="utf-8",
    )
    registry = tmp_path / "claude" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "awesome@plugins-kit": [
                        {
                            "scope": "user",
                            "installPath": str(source),
                            "installedAt": "2026-01-01T00:00:00Z",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    personal = tmp_path / "claude" / "skills"
    personal.mkdir(parents=True)
    env["CLAUDE_PLUGINS_REGISTRY"] = str(registry)
    env["CLAUDE_SKILLS_ROOT"] = str(personal)


class SuccessfulRunner:
    executable = "codex"

    def __init__(self, repository: Path, installed_root: Path) -> None:
        self.repository = repository
        self.installed_root = installed_root
        self.installed: list[dict[str, object]] = []
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        call = tuple(arguments)
        self.calls.append(call)
        if call == ("plugin", "marketplace", "list", "--json"):
            output = {
                "marketplaces": [
                    {"name": "codex-kit", "root": str(self.repository)}
                ]
            }
        elif call == ("plugin", "list", "--json"):
            output = {"installed": self.installed, "available": []}
        elif call == ("plugin", "add", "claude-plugins-kit@codex-kit", "--json"):
            manifest = json.loads(
                (self.installed_root / ".codex-plugin" / "plugin.json").read_text()
            )
            self.installed = [
                {
                    "pluginId": "claude-plugins-kit@codex-kit",
                    "name": "claude-plugins-kit",
                    "marketplaceName": "codex-kit",
                    "version": manifest["version"],
                    "installed": True,
                    "enabled": True,
                    "source": {"source": "local", "path": str(self.repository / "plugins" / "claude-plugins-kit")},
                }
            ]
            output = {
                "pluginId": "claude-plugins-kit@codex-kit",
                "installedPath": str(self.installed_root),
            }
        else:
            raise AssertionError(f"unexpected call: {call!r}")
        return subprocess.CompletedProcess(arguments, 0, json.dumps(output), "")


def successful_native(
    bridge, tmp_path: Path, env: dict[str, str]
) -> tuple[SuccessfulRunner, object, object]:
    repository = Path(__file__).parents[1]
    authored = repository / "plugins" / "claude-plugins-kit"
    codex_home = Path(env["CODEX_HOME"])
    version = json.loads(
        (authored / ".codex-plugin" / "plugin.json").read_text()
    )["version"]
    installed_root = codex_home / "plugins" / "cache" / "codex-kit" / "claude-plugins-kit" / version
    shutil.copytree(authored, installed_root)
    runner = SuccessfulRunner(repository, installed_root)

    def syncer(_installed: Path, _env: object) -> dict[str, object]:
        sources, duplicates = bridge.installed_sources(
            Path(env["CLAUDE_PLUGINS_REGISTRY"]), Path(env["CLAUDE_SKILLS_ROOT"])
        )
        assert duplicates == [] and len(sources) == 1
        files, _ = bridge.rendered_plugin(sources[0], {})
        generated = tmp_path / "data" / bridge.GENERATED_MARKETPLACE / "plugins" / "awesome"
        for relative, content in files.items():
            path = generated / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        generated_version = json.loads(files["plugin.json"])["version"]
        runner.installed.append(
            {
                "pluginId": f"awesome@{bridge.GENERATED_MARKETPLACE}",
                "name": "awesome",
                "marketplaceName": bridge.GENERATED_MARKETPLACE,
                "version": generated_version,
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": str(generated)},
            }
        )
        cache_skill = (
            codex_home
            / "plugins"
            / "cache"
            / bridge.GENERATED_MARKETPLACE
            / "awesome"
            / generated_version
            / "skills"
            / "do-work"
            / "SKILL.md"
        )
        cache_skill.parent.mkdir(parents=True, exist_ok=True)
        cache_skill.write_text(files["skills/do-work/SKILL.md"], encoding="utf-8")
        syncer.skill_path = cache_skill
        return {"status": "changed", "skipped": []}

    syncer.skill_path = None

    def discoverer(_executable: str, _env: object, _cwd: Path) -> list[dict[str, object]]:
        assert syncer.skill_path is not None
        return [
            {
                "name": "claude-plugins-kit:claude-plugins",
                "pluginId": "claude-plugins-kit@codex-kit",
                "path": str(installed_root / "skills" / "claude-plugins" / "SKILL.md"),
            },
            {
                "name": "awesome:do-work",
                "pluginId": f"awesome@{bridge.GENERATED_MARKETPLACE}",
                "path": str(syncer.skill_path),
            },
        ]

    return runner, syncer, discoverer


def test_install_failure_preserves_every_legacy_artifact_byte_for_byte(
    bridge, tmp_path: Path
) -> None:
    env, legacy_files = legacy_fixture(tmp_path)
    before = {path: path.read_bytes() for path in legacy_files}

    result = bridge.migrate(env=env, runner=InstallFailureRunner(), install=True)

    assert result["status"] == "error"
    assert result["phase"] == "install"
    assert result["backup"]
    assert all(path.read_bytes() == content for path, content in before.items())
    assert (Path(result["backup"]) / "journal.json").is_file()


def test_preview_is_read_only_and_matches_windows_quoted_hook_with_bom(
    migration, tmp_path: Path
) -> None:
    env, files = legacy_fixture(tmp_path)
    hook = files[0]
    document = json.loads(hook.read_text())
    handler = document["hooks"]["SessionStart"][0]["hooks"][0]
    windows_home = r"C:\Users\Truff User\.codex"
    command = subprocess.list2cmdline(
        [r"C:\Program Files\Python312\python.exe", windows_home + r"\scripts\startup_hook.py"]
    )
    assert migration._owned_hook(
        {**handler, "command": command},
        "^(startup|resume)$",
        {migration._path_token(windows_home + r"\scripts\startup_hook.py")},
    )
    hook.write_bytes(b"\xef\xbb\xbf" + hook.read_bytes().replace(b"\n", b"\r\n"))
    before_tree = {path: path.read_bytes() for path in files}
    found = migration.inventory(env)
    assert found.hook_count == found.stub_count == found.state_count == 1
    assert {path: path.read_bytes() for path in files} == before_tree
    assert not Path(env["CODEX_KIT_DATA_ROOT"]).exists()


def test_malformed_or_duplicate_stub_ownership_fails_closed(
    migration, tmp_path: Path
) -> None:
    env, files = legacy_fixture(tmp_path)
    stub = files[1]
    stub.write_text(stub.read_text().replace("  generator:", "  generator: wrong\n  generator:"))
    with pytest.raises(migration.MigrationError, match="duplicate"):
        migration.inventory(env)
    assert stub.exists()


def test_symlinked_skill_descendant_is_refused(migration, tmp_path: Path) -> None:
    env, _ = legacy_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = Path(env["CODEX_HOME"]) / "skills" / "linked"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(migration.MigrationError, match="symlink or reparse"):
        migration.inventory(env)


def test_missing_expected_generated_plugin_blocks_cleanup(
    bridge, tmp_path: Path
) -> None:
    env, legacy_files = legacy_fixture(tmp_path)
    add_claude_source(env, tmp_path)
    runner, _, _ = successful_native(bridge, tmp_path, env)

    result = bridge.migrate(
        env=env,
        runner=runner,
        install=True,
        sync_launcher=lambda *_: {"status": "changed", "skipped": []},
        discoverer=lambda *_: [
            {"name": "claude-plugins-kit:claude-plugins", "pluginId": "claude-plugins-kit@codex-kit"}
        ],
    )

    assert result["status"] == "error"
    assert result["phase"] == "verify"
    assert all(path.exists() for path in legacy_files)


def test_verified_apply_preserves_foreign_files_and_is_idempotent(
    bridge, tmp_path: Path
) -> None:
    env, legacy_files = legacy_fixture(tmp_path)
    add_claude_source(env, tmp_path)
    codex_home = Path(env["CODEX_HOME"])
    foreign_skill = codex_home / "skills" / "foreign" / "SKILL.md"
    foreign_skill.parent.mkdir()
    foreign_skill.write_text("foreign", encoding="utf-8")
    extra = legacy_files[1].parent / "note.txt"
    extra.write_text("preserve", encoding="utf-8")
    hooks = json.loads(legacy_files[0].read_text())
    hooks["hooks"]["SessionStart"][0]["hooks"].append(
        {"type": "command", "command": "python foreign.py"}
    )
    legacy_files[0].write_text(json.dumps(hooks), encoding="utf-8")
    runner, syncer, discoverer = successful_native(bridge, tmp_path, env)

    result = bridge.migrate(
        env=env,
        runner=runner,
        install=True,
        sync_launcher=syncer,
        discoverer=discoverer,
    )

    assert result["status"] == "changed"
    assert result["phase"] == "complete"
    assert result["restart_required"] is True
    assert not legacy_files[1].exists() and not legacy_files[2].exists()
    assert extra.read_text() == "preserve" and foreign_skill.read_text() == "foreign"
    remaining = json.loads(legacy_files[0].read_text())
    assert remaining["hooks"]["SessionStart"][0]["hooks"] == [
        {"type": "command", "command": "python foreign.py"}
    ]
    backup = Path(result["backup"])
    assert backup.stat().st_mode & 0o777 == 0o700
    assert (backup / "journal.json").is_file()

    rerun = bridge.migrate(env=env, runner=runner, install=True)
    assert rerun["status"] == "unchanged"
    assert rerun["changed"] is False
    assert rerun["migration_needed"] is False
    assert rerun["backup"] is None


def test_interrupted_cleanup_retries_postimage_but_rejects_drift(
    bridge, tmp_path: Path
) -> None:
    env, legacy_files = legacy_fixture(tmp_path)
    add_claude_source(env, tmp_path)
    runner, syncer, discoverer = successful_native(bridge, tmp_path, env)
    raised = False

    def interrupt(_phase: str, _path: Path) -> None:
        nonlocal raised
        if not raised:
            raised = True
            raise OSError("simulated interruption")

    first = bridge.migrate(
        env=env,
        runner=runner,
        install=True,
        sync_launcher=syncer,
        discoverer=discoverer,
        fault=interrupt,
    )
    assert first["status"] == "error" and first["phase"] == "cleanup"
    assert first["partial_cleanup"] is True
    remaining = next(path for path in legacy_files if path.exists())
    remaining.write_bytes(remaining.read_bytes() + b"drift")

    second = bridge.migrate(
        env=env,
        runner=runner,
        install=True,
        sync_launcher=syncer,
        discoverer=discoverer,
    )
    assert second["status"] == "error"
    assert "drift" in second["message"]


@pytest.mark.parametrize("tamper", ["corrupt", "escape"])
def test_unfinished_journal_is_validated_before_resume(
    bridge, tmp_path: Path, tamper: str
) -> None:
    env, _ = legacy_fixture(tmp_path)
    failed = bridge.migrate(env=env, runner=InstallFailureRunner(), install=True)
    journal_path = Path(failed["backup"]) / "journal.json"
    if tamper == "corrupt":
        journal_path.write_text("{bad", encoding="utf-8")
    else:
        journal = json.loads(journal_path.read_text())
        journal["artifacts"][0]["path"] = str(tmp_path / "outside")
        journal["artifacts"][0]["relative"] = "../outside"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

    result = bridge.migrate(env=env, runner=InstallFailureRunner(), install=False)

    assert result["status"] == "error"
    assert result["phase"] == "inventory"
    assert "journal" in result["message"]


def test_older_release_journal_is_revalidated_and_recovered(bridge, tmp_path: Path) -> None:
    env, legacy_files = legacy_fixture(tmp_path)
    add_claude_source(env, tmp_path)
    failed = bridge.migrate(env=env, runner=InstallFailureRunner(), install=True)
    journal_path = Path(failed["backup"]) / "journal.json"
    journal = json.loads(journal_path.read_text())
    journal["expected_version"] = "0.1.0"
    journal_path.write_text(json.dumps(journal))
    before = journal_path.read_bytes()
    preview = bridge.migrate(env=env, runner=InstallFailureRunner(), install=False)
    assert preview["status"] == "changed", preview
    assert journal_path.read_bytes() == before
    runner, syncer, discoverer = successful_native(bridge, tmp_path, env)
    result = bridge.migrate(env=env, runner=runner, install=True, sync_launcher=syncer, discoverer=discoverer)
    assert result["status"] == "changed", result
    assert result["phase"] == "complete"
    assert result["backup"] == failed["backup"]
    completed = json.loads(journal_path.read_text())
    assert completed["previous_version"] == "0.1.0"
    assert completed["expected_version"] != "0.1.0"


def test_newer_release_journal_is_never_downgraded(bridge, tmp_path: Path) -> None:
    env, legacy_files = legacy_fixture(tmp_path)
    failed = bridge.migrate(env=env, runner=InstallFailureRunner(), install=True)
    journal_path = Path(failed["backup"]) / "journal.json"
    journal = json.loads(journal_path.read_text())
    journal["expected_version"] = "99.0.0"
    journal_path.write_text(json.dumps(journal))
    before = journal_path.read_bytes()
    result = bridge.migrate(env=env, runner=InstallFailureRunner(), install=True)
    assert result["status"] == "error"
    assert "newer plugin version" in result["message"]
    assert journal_path.read_bytes() == before
    assert all(path.exists() for path in legacy_files)


def test_resume_rejects_dangling_symlink_replacing_cleanup_target(
    bridge, tmp_path: Path
) -> None:
    env, legacy_files = legacy_fixture(tmp_path)
    failed = bridge.migrate(env=env, runner=InstallFailureRunner(), install=True)
    assert failed["status"] == "error"
    state = legacy_files[2]
    state.unlink()
    missing = tmp_path / "missing-state-target"
    state.symlink_to(missing)

    result = bridge.migrate(env=env, runner=InstallFailureRunner(), install=False)

    assert result["status"] == "error"
    assert result["phase"] == "inventory"
    assert "symlink or reparse" in result["message"]
    assert state.is_symlink()
    assert not missing.exists()


def test_managed_path_guards_reject_dangling_intermediate_symlink(
    migration, tmp_path: Path
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    dangling = managed / "dangling"
    dangling.symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(migration.MigrationError, match="symlink or reparse"):
        migration._safe_descendant(dangling / "artifact", managed, "cleanup target")
    with pytest.raises(migration.MigrationError, match="symlink or reparse"):
        migration._data_root({"CODEX_KIT_DATA_ROOT": str(dangling / "data")})

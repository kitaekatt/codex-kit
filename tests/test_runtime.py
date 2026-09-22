from __future__ import annotations

import json
import hashlib
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


def test_missing_and_stale_reports_are_advisory_at_resolution_seam(bridge, tmp_path, capsys):
    root, _, env = runtime_env(tmp_path)
    env["CODEX_KIT_DATA_ROOT"] = str(tmp_path / "codex-data")
    skill_root = root / "skills" / "work"
    skill_root.mkdir(parents=True)
    source = skill_root / "SKILL.md"
    source.write_text("---\nname: work\ndescription: sample\n---\nDo work.\n", encoding="utf-8")

    first = bridge.runtime_info("alpha@test", env)
    assert first["compatibility"]["reports"][0]["findings"][0].startswith("Compatibility report is missing")
    assert "normal skill execution continues" in capsys.readouterr().err

    paths = bridge.paths_for(env)
    report_path = paths.plugins_root / "alpha" / "compatibility" / "work.json"
    report_path.parent.mkdir(parents=True)
    compatibility = bridge._compatibility_module()
    stale_report = compatibility.make_report(
        source_identity="alpha@test", skill_path="skills/work/SKILL.md", source_digest="old",
        requirements=[], inference={"status": "not-needed"},
        profile={"id": "codex-host", "version": "1"}, mappings={"version": "1"},
    )
    report_path.write_text(json.dumps(stale_report))
    second = bridge.runtime_info("alpha@test", env)
    assert second["compatibility"]["reports"][0]["findings"][0].startswith("Compatibility report is stale")
    assert second["source_exists"] is True

    stale_report["generation"] = "tampered"
    report_path.write_text(json.dumps(stale_report))
    invalid = bridge.runtime_info("alpha@test", env)
    assert invalid["compatibility"]["reports"][0]["findings"][0].startswith("Compatibility report is invalid")


def test_runtime_info_rejects_selected_skill_missing_from_plugin_source(bridge, tmp_path):
    _, _, env = runtime_env(tmp_path)

    with pytest.raises(bridge.BridgeError, match="skill source is missing"):
        bridge.runtime_info("alpha@test", env, "missing")


def test_runtime_info_discloses_plugin_skill_scan_failure(bridge, tmp_path):
    root, _, env = runtime_env(tmp_path)
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{malformed", encoding="utf-8")

    info = bridge.runtime_info("alpha@test", env)

    findings = [finding for report in info["compatibility"]["reports"] for finding in report["findings"]]
    assert any("Skill source scan failed" in finding and "invalid Claude plugin manifest" in finding
               for finding in findings)


@pytest.mark.parametrize("broken_source", ["sibling", "manifest"])
def test_runtime_info_resolves_valid_selected_skill_despite_unrelated_scan_failure(
    bridge, tmp_path, broken_source,
):
    root, _, env = runtime_env(tmp_path)
    selected = root / "skills" / "good" / "SKILL.md"
    selected.parent.mkdir(parents=True)
    selected.write_text("\n".join(["---", "name: good", "description: usable", "---", ""]), encoding="utf-8")
    if broken_source == "sibling":
        sibling = root / "skills" / "broken" / "SKILL.md"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("not valid frontmatter\n", encoding="utf-8")
    else:
        manifest = root / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{malformed", encoding="utf-8")

    info = bridge.runtime_info("alpha@test", env, "good")

    assert info["source_exists"] is True
    assert info["compatibility"]["reports"][0]["skill"] == "good"
    assert any("Skill source scan failed" in finding for report in info["compatibility"]["reports"]
               for finding in report["findings"])


def test_runtime_disclosures_deduplicate_within_exported_session(bridge, tmp_path, capsys):
    root, _, env = runtime_env(tmp_path)
    env["CODEX_KIT_DATA_ROOT"] = str(tmp_path / "codex-data")
    env["CODEX_SESSION_ID"] = "test-session-123"
    skill_root = root / "skills" / "work"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("---\nname: work\n---\n", encoding="utf-8")
    first = bridge.runtime_info("alpha@test", env)
    assert first["compatibility"]["advisory"] is True
    assert capsys.readouterr().err
    second = bridge.runtime_info("alpha@test", env)
    assert second["compatibility"]["reports"][0]["findings"]
    assert second["compatibility"]["advisory"] is False
    assert capsys.readouterr().err == ""


def test_malformed_session_disclosure_state_is_best_effort(bridge, tmp_path, capsys):
    root, _, env = runtime_env(tmp_path)
    env["CODEX_KIT_DATA_ROOT"] = str(tmp_path / "codex-data")
    env["CODEX_SESSION_ID"] = "test-session-malformed"
    skill_root = root / "skills" / "work"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("---\nname: work\n---\n", encoding="utf-8")
    data_root = Path(env["CODEX_KIT_DATA_ROOT"])
    data_root.mkdir(parents=True)
    session_key = hashlib.sha256(env["CODEX_SESSION_ID"].encode()).hexdigest()
    (data_root / "compatibility-disclosures.json").write_text(json.dumps({
        "schema": 1, "sessions": {session_key: 17},
    }), encoding="utf-8")

    info = bridge.runtime_info("alpha@test", env)

    assert info["compatibility"]["advisory"] is True
    assert "normal skill execution continues" in capsys.readouterr().err


def test_advisory_runtime_check_preserves_underlying_script_failure(bridge, tmp_path, monkeypatch):
    root, _, env = runtime_env(tmp_path)
    env["CODEX_KIT_DATA_ROOT"] = str(tmp_path / "codex-data")
    skill = root / "skills" / "work"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: work\n---\n", encoding="utf-8")
    script = root / "scripts" / "run.py"
    script.parent.mkdir()
    script.write_text("raise SystemExit(23)\n", encoding="utf-8")
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")
    env["ALPHA_VENV"] = str(interpreter)

    def failed_child(arguments, **kwargs):
        return __import__("subprocess").CompletedProcess(arguments, 23, "", "script error")

    monkeypatch.setattr(bridge.subprocess, "run", failed_child)
    assert bridge.run_python("alpha@test", "scripts/run.py", None, [], env) == 23

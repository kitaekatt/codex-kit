from __future__ import annotations

import json
from pathlib import Path

from test_bridge import FakeRunner, generated_marketplace, generated_records, initial_sync, installed_record, source_fixture, write_skill


def authored_record(enabled: bool = True) -> dict:
    record = installed_record("claude-plugins-kit", "codex-kit", "0.1.2")
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

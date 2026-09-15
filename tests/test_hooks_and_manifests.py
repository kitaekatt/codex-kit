from __future__ import annotations

import json
from pathlib import Path


def test_hook_runs_maintenance_cross_platform():
    root = Path(__file__).parents[1] / "plugins" / "claude-plugins-kit"
    hooks = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    handler = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["command"].endswith('launch.sh\" maintain --startup')
    assert 'call "%CLAUDE_PLUGIN_ROOT%\\scripts\\launch.cmd" maintain --startup' in handler["commandWindows"]
    assert 'call "%PLUGIN_ROOT%\\scripts\\launch.cmd" maintain --startup' in handler["commandWindows"]
    assert "${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:?" in handler["command"]
    assert "$root/scripts/launch.sh" in handler["command"]
    assert "if defined CLAUDE_PLUGIN_ROOT" in handler["commandWindows"]
    assert "if defined PLUGIN_ROOT" in handler["commandWindows"]
    assert "exit /b 2" in handler["commandWindows"]


def test_authored_manifests_and_marketplace_names_match():
    root = Path(__file__).parents[1]
    plugin = root / "plugins" / "claude-plugins-kit"
    compatibility = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
    marketplace = json.loads((root / ".agents" / "plugins" / "marketplace.json").read_text())
    assert compatibility["name"] == marketplace["plugins"][0]["name"] == "claude-plugins-kit"
    # A root agent-plugin manifest shadows .codex-plugin and disables hooks in 0.154.
    assert not (plugin / "plugin.json").exists()


def test_startup_output_has_native_session_start_shape(bridge, capsys):
    bridge.print_sync_result({"message": "Bridge current."}, startup=True)
    output = json.loads(capsys.readouterr().out)
    assert output["systemMessage"] == "Bridge current."
    assert output["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": "Bridge current.",
    }

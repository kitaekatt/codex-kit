from __future__ import annotations

import json
from pathlib import Path


def test_hook_runs_installing_sync_cross_platform():
    root = Path(__file__).parents[1] / "plugins" / "claude-plugins-kit"
    hooks = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    handler = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["command"].endswith('launch.sh\" sync --install --startup')
    assert handler["commandWindows"].endswith('launch.cmd\" sync --install --startup')
    assert "$PLUGIN_ROOT" in handler["command"]
    assert "%PLUGIN_ROOT%" in handler["commandWindows"]


def test_authored_manifests_and_marketplace_names_match():
    root = Path(__file__).parents[1]
    plugin = root / "plugins" / "claude-plugins-kit"
    portable = json.loads((plugin / "plugin.json").read_text())
    compatibility = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
    marketplace = json.loads((root / ".agents" / "plugins" / "marketplace.json").read_text())
    assert portable["name"] == compatibility["name"] == marketplace["plugins"][0]["name"] == "claude-plugins-kit"
    assert portable["extensions"]["com.openai"]["hooks"] == "./hooks/hooks.json"


def test_startup_output_has_native_session_start_shape(bridge, capsys):
    bridge.print_sync_result({"message": "Bridge current."}, startup=True)
    output = json.loads(capsys.readouterr().out)
    assert output["systemMessage"] == "Bridge current."
    assert output["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": "Bridge current.",
    }

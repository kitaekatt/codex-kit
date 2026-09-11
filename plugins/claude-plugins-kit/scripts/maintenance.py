"""Consumer startup convergence, owned by the installed native Codex plugin.

Only native plugin commands perform installation. No hook trust, shell profiles,
Claude data, or fleet setup are changed here.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

PLUGIN_ID = "claude-plugins-kit@codex-kit"
OFFICIAL_SOURCES = {"https://github.com/kitaekatt/codex-kit.git", "https://github.com/kitaekatt/codex-kit"}
CHECK_INTERVAL = 6 * 60 * 60
RETRY_INTERVAL = 15 * 60


def release_version(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"not a stable bridge release version: {value!r}")
    return tuple(int(part) for part in value.split("."))


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": 1, "last_attempt": 0, "last_success": 0}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError(f"invalid bridge maintenance state: {path}")
    for key in ("last_attempt", "last_success"):
        if type(value.get(key)) not in (int, float) or value[key] < 0:
            raise ValueError(f"invalid bridge maintenance timestamp: {path}")
    return value


def update_release(*, bridge: Any, env: Mapping[str, str], runner: Any,
                   paths: Any, current: Mapping[str, Any], check_updates: bool) -> Path | None:
    """Return a verified newly installed root, or leave the current release running."""
    if env.get("CODEX_KIT_AUTO_UPDATE") == "0":
        return None
    marketplaces = [entry for entry in bridge.configured_marketplaces(runner) if entry["name"] == "codex-kit"]
    if not marketplaces:
        return None
    if len(marketplaces) != 1:
        raise ValueError("multiple marketplaces named codex-kit")
    marketplace = marketplaces[0]
    source = marketplace.get("marketplaceSource", {})
    # Local native installs are a supported development workflow, never git-pull them.
    if source.get("sourceType") == "local":
        return None
    if source.get("sourceType") != "git" or source.get("source") not in OFFICIAL_SOURCES:
        raise ValueError("codex-kit is not registered from the official Git source; automatic release update skipped")
    state_path = paths.data_root / "maintenance.json"
    if state_path.is_symlink():
        raise ValueError(f"maintenance state cannot be a symlink: {state_path}")
    state = read_state(state_path)
    now = time.time()
    interval = CHECK_INTERVAL if state["last_success"] >= state["last_attempt"] else RETRY_INTERVAL
    elapsed = now - state["last_attempt"]
    if not check_updates and 0 <= elapsed < interval:
        return None
    state["last_attempt"] = now
    bridge.atomic_write(state_path, json.dumps(state, sort_keys=True) + "\n")
    result = bridge.json_command(runner, ["plugin", "marketplace", "upgrade", "codex-kit", "--json"], "bridge release check")
    if result.get("errors"):
        raise ValueError(f"bridge marketplace upgrade failed: {result['errors']}")
    root = Path(marketplace["root"])
    candidate_root = root / "plugins" / "claude-plugins-kit"
    manifest = json.loads((candidate_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    if manifest.get("name") != "claude-plugins-kit" or (candidate_root / "plugin.json").exists():
        raise ValueError("bridge release has an invalid or hook-shadowing manifest")
    new_root = None
    if release_version(manifest.get("version")) > release_version(current.get("version")):
        # Recheck disable/removal after the network wait; never resurrect an opt-out.
        active = [item for item in bridge.installed_plugins(runner) if item["pluginId"] == PLUGIN_ID]
        if not active or active[0].get("enabled") is False:
            return None
        installed = bridge.json_command(runner, ["plugin", "add", PLUGIN_ID, "--json"], "bridge release install")
        if installed.get("pluginId") != PLUGIN_ID or installed.get("version") != manifest["version"]:
            raise ValueError("native bridge install did not return the selected release")
        migration = bridge._migration_module()
        _, codex_home = migration._resolve_codex_home(env)
        new_root = migration._verify_installed_root(Path(installed["installedPath"]), codex_home, manifest["version"])
    state["last_success"] = now
    bridge.atomic_write(state_path, json.dumps(state, sort_keys=True) + "\n")
    return new_root


def maintain(*, bridge: Any, env: Mapping[str, str], runner: Any,
             script: Path | None = None, check_updates: bool = False) -> dict[str, Any]:
    try:
        active = [entry for entry in bridge.installed_plugins(runner) if entry["pluginId"] == PLUGIN_ID]
        if not active or active[0].get("enabled") is False:
            return {"status": "unchanged", "changed": False, "message": "Claude Plugins Kit is removed or disabled; maintenance skipped."}
        paths = bridge.paths_for(env, script)
        lock_path = paths.data_root / ".maintenance.lock"
        if lock_path.is_symlink():
            raise ValueError(f"maintenance lock cannot be a symlink: {lock_path}")
        with bridge.FileLock(lock_path, timeout=1):
            # Local convergence remains available when release downloads fail.
            result = bridge.sync(env=env, runner=runner, script=script, install=True, respect_disabled=True)
            warnings: list[str] = []
            if result["status"] == "error":
                return result
            try:
                new_root = update_release(bridge=bridge, env=env, runner=runner, paths=paths,
                                          current=active[0], check_updates=check_updates)
                if new_root is not None:
                    # A fresh process uses the new release's compatibility rules and runtime.
                    command = ([env.get("COMSPEC", "cmd.exe"), "/d", "/c", str(new_root / "scripts/launch.cmd")]
                               if sys.platform == "win32" else [str(new_root / "scripts/launch.sh")])
                    completed = subprocess.run([*command, "sync", "--install", "--respect-disabled"],
                                               env=dict(env), text=True, capture_output=True, timeout=120, shell=False)
                    if completed.returncode:
                        raise ValueError("new bridge installed but catalog refresh failed: " + (completed.stderr or completed.stdout)[-2000:])
                    refreshed = json.loads(completed.stdout)
                    if refreshed.get("status") == "error":
                        raise ValueError(refreshed.get("message", "new bridge refresh failed"))
                    result = refreshed
                    result["status"] = "changed"
                    result["changed"] = True
                    result["bridge_updated"] = True
                    result["message"] = "Claude Plugins Kit updated through Codex. " + result["message"]
            except (ValueError, OSError, subprocess.SubprocessError, bridge.BridgeError) as exc:
                warnings.append(f"Automatic bridge release update incomplete; it will retry: {exc}")
            result["warnings"] = warnings
            if warnings:
                result["message"] += " " + " ".join(warnings)
            return result
    except (ValueError, OSError, subprocess.SubprocessError, bridge.BridgeError) as exc:
        return {"status": "error", "changed": False, "message": str(exc)}

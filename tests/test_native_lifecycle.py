from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Sequence, TextIO

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
CODEX = shutil.which("codex")
GENERATED_MARKETPLACE = "claude-plugins-kit-generated"


def detected_codex_version() -> str | None:
    if CODEX is None:
        return None
    result = subprocess.run(
        [CODEX, "--version"],
        text=True,
        capture_output=True,
        timeout=10,
        shell=False,
    )
    if result.returncode:
        return None
    match = re.search(r"\b\d+\.\d+\.\d+\b", result.stdout)
    return match.group(0) if match else None


CODEX_VERSION = detected_codex_version()

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("CODEX_KIT_NATIVE_TESTS") != "1",
        reason="set CODEX_KIT_NATIVE_TESTS=1 to run native Codex lifecycle tests",
    ),
    pytest.mark.skipif(CODEX is None, reason="codex executable is unavailable"),
]


def write_skill(root: Path, name: str, description: str) -> Path:
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nSynthetic canonical body.\n",
        encoding="utf-8",
    )
    return path


def write_registry(path: Path, plugin_root: Path, installed: bool = True) -> None:
    plugins: dict[str, list[dict[str, str]]] = {}
    if installed:
        plugins["awesome-kit@plugins-kit"] = [
            {
                "scope": "user",
                "installPath": str(plugin_root),
                "installedAt": "2026-01-01T00:00:00Z",
            }
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8")


def isolated_environment(tmp_path: Path, registry: Path, personal_skills: Path) -> dict[str, str]:
    home = tmp_path / "user-home"
    codex_home = tmp_path / "codex-home"
    data_root = tmp_path / "codex-kit-data"
    for directory in (home, codex_home, data_root, personal_skills):
        directory.mkdir(parents=True, exist_ok=True)
    path = os.environ.get("PATH")
    if not path:
        raise AssertionError("PATH is required for the native lifecycle test")
    assert CODEX is not None
    return {
        "PATH": path,
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "CODEX_KIT_DATA_ROOT": str(data_root),
        "CODEX_KIT_CODEX_BIN": CODEX,
        "CODEX_KIT_PYTHON": sys.executable,
        "CLAUDE_PLUGINS_REGISTRY": str(registry),
        "CLAUDE_SKILLS_ROOT": str(personal_skills),
        "NO_COLOR": "1",
    }


def json_command(
    arguments: Sequence[str], env: dict[str, str], cwd: Path, timeout: float = 30.0
) -> dict[str, Any]:
    result = subprocess.run(
        list(arguments),
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        shell=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout)[-4000:]
        raise AssertionError(f"command failed ({result.returncode}): {arguments!r}\n{detail}")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"command returned invalid JSON: {arguments!r}\n{result.stdout[-4000:]}"
        ) from exc
    if not isinstance(document, dict):
        raise AssertionError(f"command did not return a JSON object: {arguments!r}")
    return document


def launcher_command(installed_path: Path) -> list[str]:
    if os.name == "nt":
        launcher = installed_path / "scripts" / "launch.cmd"
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(launcher)]
    launcher = installed_path / "scripts" / "launch.sh"
    return [str(launcher)]


def send_request(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def read_messages(stream: TextIO, messages: queue.Queue[dict[str, Any] | None]) -> None:
    for line in stream:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            messages.put(value)
    messages.put(None)


def response(
    messages: queue.Queue[dict[str, Any] | None], request_id: int, deadline: float
) -> dict[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for app-server response {request_id}")
        try:
            message = messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError(
                f"timed out waiting for app-server response {request_id}"
            ) from exc
        if message is None:
            raise RuntimeError(f"app-server exited before response {request_id}")
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise RuntimeError(f"app-server error for response {request_id}: {message['error']}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"app-server response {request_id} has no result object")
        return result


def app_server_records(
    env: dict[str, str],
    cwd: Path,
    method: str,
    record_field: str,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    assert CODEX is not None
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            [CODEX, "app-server", "--stdio"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        reader = threading.Thread(target=read_messages, args=(process.stdout, messages), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        failure: Exception | None = None
        result: dict[str, Any] = {}
        try:
            send_request(
                process,
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {"name": "codex-kit-native-test", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                },
            )
            response(messages, 1, deadline)
            send_request(process, {"method": "initialized", "params": {}})
            send_request(
                process,
                {
                    "method": method,
                    "id": 2,
                    "params": (
                        {"cwds": [str(cwd)], "forceReload": True}
                        if method == "skills/list"
                        else {"cwds": [str(cwd)]}
                    ),
                },
            )
            result = response(messages, 2, deadline)
        except Exception as exc:
            failure = exc
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if failure is None and process.returncode:
            failure = RuntimeError(f"app-server exited with status {process.returncode}")
        if failure is not None:
            stderr.seek(0)
            detail = stderr.read()[-4000:]
            raise AssertionError(f"app-server {method} failed: {failure}\n{detail}") from failure
        entries = result.get("data")
        if not isinstance(entries, list):
            raise AssertionError(f"app-server {method} response has no data array")
        records_found: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("cwd") != str(cwd):
                continue
            errors = entry.get("errors")
            if errors:
                raise AssertionError(f"app-server {method} discovery errors: {errors!r}")
            records = entry.get(record_field)
            if isinstance(records, list):
                records_found.extend(record for record in records if isinstance(record, dict))
        return records_found


def app_server_skills(
    env: dict[str, str], cwd: Path, timeout: float = 30.0
) -> list[dict[str, Any]]:
    return app_server_records(env, cwd, "skills/list", "skills", timeout)


def app_server_hooks(
    env: dict[str, str], cwd: Path, timeout: float = 30.0
) -> list[dict[str, Any]]:
    return app_server_records(env, cwd, "hooks/list", "hooks", timeout)


def skills_by_name(skills: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(skill["name"]): skill
        for skill in skills
        if isinstance(skill.get("name"), str)
    }


def sync_installed_bridge(
    launcher: Sequence[str], env: dict[str, str], cwd: Path
) -> dict[str, Any]:
    return json_command([*launcher, "sync", "--install"], env, cwd, timeout=60.0)


def install_bridge(env: dict[str, str], cwd: Path) -> Path:
    marketplace = json_command(
        [CODEX or "codex", "plugin", "marketplace", "add", str(REPOSITORY), "--json"],
        env,
        cwd,
    )
    assert marketplace["marketplaceName"] == "codex-kit"
    installed = json_command(
        [CODEX or "codex", "plugin", "add", "claude-plugins-kit@codex-kit", "--json"],
        env,
        cwd,
    )
    assert installed["pluginId"] == "claude-plugins-kit@codex-kit"
    installed_path = Path(str(installed["installedPath"]))
    assert installed_path.is_dir()
    assert installed_path.is_relative_to(Path(env["CODEX_HOME"]) / "plugins" / "cache")
    return installed_path


def test_installed_bridge_drives_native_skill_catalog_lifecycle(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".git").mkdir()
    plugin_root = tmp_path / "claude-cache" / "awesome-kit" / "1.0.0"
    orchestrate = write_skill(plugin_root, "orchestrate", "Initial orchestration description.")
    registry = tmp_path / "claude" / "plugins" / "installed_plugins.json"
    personal_skills = tmp_path / "claude" / "skills"
    write_registry(registry, plugin_root)
    env = isolated_environment(tmp_path, registry, personal_skills)

    installed_path = install_bridge(env, cwd)
    launcher = launcher_command(installed_path)

    first_sync = sync_installed_bridge(launcher, env, cwd)
    assert first_sync["status"] == "changed", first_sync
    initial = skills_by_name(app_server_skills(env, cwd))
    assert initial["claude-plugins-kit:claude-plugins"]["pluginId"] == (
        "claude-plugins-kit@codex-kit"
    )
    assert Path(initial["claude-plugins-kit:claude-plugins"]["path"]).is_relative_to(
        installed_path
    )
    assert initial["awesome-kit:orchestrate"]["pluginId"] == (
        f"awesome-kit@{GENERATED_MARKETPLACE}"
    )
    assert initial["awesome-kit:orchestrate"]["description"] == (
        "Initial orchestration description."
    )

    orchestrate.write_text(
        "---\nname: orchestrate\ndescription: Updated orchestration description.\n---\n\n"
        "Synthetic canonical body.\n",
        encoding="utf-8",
    )
    debug_context = write_skill(plugin_root, "debug-context", "Synthetic debugging description.")
    update_sync = sync_installed_bridge(launcher, env, cwd)
    assert update_sync["status"] == "changed", update_sync
    updated = skills_by_name(app_server_skills(env, cwd))
    assert updated["awesome-kit:orchestrate"]["description"] == (
        "Updated orchestration description."
    )
    assert updated["awesome-kit:debug-context"]["pluginId"] == (
        f"awesome-kit@{GENERATED_MARKETPLACE}"
    )

    debug_context.unlink()
    remove_skill_sync = sync_installed_bridge(launcher, env, cwd)
    assert remove_skill_sync["status"] == "changed", remove_skill_sync
    skill_removed = skills_by_name(app_server_skills(env, cwd))
    assert "awesome-kit:debug-context" not in skill_removed
    assert "awesome-kit:orchestrate" in skill_removed

    write_registry(registry, plugin_root, installed=False)
    uninstall_sync = sync_installed_bridge(launcher, env, cwd)
    assert uninstall_sync["status"] == "changed", uninstall_sync
    final = skills_by_name(app_server_skills(env, cwd))
    assert "awesome-kit:orchestrate" not in final
    assert final["claude-plugins-kit:claude-plugins"]["pluginId"] == (
        "claude-plugins-kit@codex-kit"
    )
    installed_plugins = json_command(
        [CODEX or "codex", "plugin", "list", "--json"], env, cwd
    )["installed"]
    assert not any(
        isinstance(record, dict)
        and record.get("pluginId") == f"awesome-kit@{GENERATED_MARKETPLACE}"
        for record in installed_plugins
    )


@pytest.mark.xfail(
    CODEX_VERSION == "0.154.0",
    reason="Codex CLI 0.154.0 does not expose bundled plugin hooks",
    strict=True,
)
def test_installed_bridge_exposes_bundled_session_start_hook(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".git").mkdir()
    registry = tmp_path / "claude" / "plugins" / "installed_plugins.json"
    personal_skills = tmp_path / "claude" / "skills"
    write_registry(registry, tmp_path / "claude-cache" / "unused", installed=False)
    env = isolated_environment(tmp_path, registry, personal_skills)
    install_bridge(env, cwd)

    plugin_hooks = [
        hook
        for hook in app_server_hooks(env, cwd)
        if hook.get("pluginId") == "claude-plugins-kit@codex-kit"
    ]
    assert len(plugin_hooks) == 1, plugin_hooks
    session_start = plugin_hooks[0]
    assert session_start["eventName"] == "sessionStart"
    assert session_start["handlerType"] == "command"
    assert session_start["trustStatus"] == "untrusted"
    expected_launcher = "launch.cmd" if os.name == "nt" else "launch.sh"
    assert expected_launcher in session_start["command"]

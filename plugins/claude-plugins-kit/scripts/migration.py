#!/usr/bin/env python3
"""Recover the retired standalone bridge into the native Codex plugin."""
from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.util
import json
import ntpath
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping, Protocol, Sequence, TextIO


AUTHORED_ID = "claude-plugins-kit@codex-kit"
AUTHORED_NAME = "claude-plugins-kit"
AUTHORED_MARKETPLACE = "codex-kit"
AUTHORED_MARKETPLACE_SOURCE = "kitaekatt/codex-kit"
GENERATED_MARKETPLACE = "claude-plugins-kit-generated"
LEGACY_GENERATOR = "plugins-kit/sync_plugins_kit.py"
LEGACY_HOOK_STATUS = "Synchronizing shared skills"
LEGACY_HOOK_MATCHER = "^(startup|resume)$"
LEGACY_STATE = ".plugins-kit-sync-state.json"
JOURNAL_SCHEMA = 1
MIGRATION_ID = "standalone-bridge-to-native-v1"
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
GIT_HEAD = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
PYTHON_EXE = re.compile(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?\Z", re.IGNORECASE)

# Exact normalized contents retired by codex-settings d0186d7. A matching file
# is safe to remove independently; modified or additional gateway files remain.
LEGACY_GATEWAY_HASHES = {
    "skills/claude-plugins/SKILL.md": "b62e971fb17cf267891626cef6c13eae10025569fdfcf16a0f456117c3358a76",
    "skills/claude-plugins/references/maintenance.md": "6ce688fad5ecc5886f7edb709fa2673b8d23403b99df3a8bab2fca0996fe60be",
    "skills/claude-plugins/references/plugin-distribution.md": "97416cceb4a2876aba733fd0646153138241d4b6540a36de839a4f8ed0840a9c",
    "skills/claude-plugins/references/troubleshooting.md": "e086232a191b58c46a90436fd1a77f0e9cc7bfc5f961fc2bd7be712aa34b4e8a",
}


class MigrationError(RuntimeError):
    """A migration invariant failed without authorizing legacy cleanup."""


class CommandRunner(Protocol):
    executable: str

    def run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class Artifact:
    path: Path
    relative: str
    kind: str
    pre_hash: str
    post: bytes | None
    identity: str | None = None


@dataclass(frozen=True)
class Inventory:
    codex_home: Path
    artifacts: tuple[Artifact, ...]
    hook_count: int
    stub_count: int
    state_count: int
    gateway_count: int
    identities: tuple[str, ...]
    preserved_stub_directories: tuple[str, ...]
    hook_warnings: tuple[str, ...]


SyncLauncher = Callable[[Path, Mapping[str, str]], Mapping[str, Any]]
SkillDiscoverer = Callable[[str, Mapping[str, str], Path], list[dict[str, Any]]]
FaultInjector = Callable[[str, Path], None]


class MigrationLock:
    """Serialize migrations without making preview write machine state."""

    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self.handle: Any = None

    def __enter__(self) -> "MigrationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    if self.handle.read(1) == b"":
                        self.handle.write(b"0")
                        self.handle.flush()
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise MigrationError("another bridge migration is still running")
                time.sleep(0.1)

    def __exit__(self, *_: object) -> None:
        if self.handle is None:
            return
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def _require_regular(path: Path, label: str) -> None:
    if _is_reparse(path) or not path.is_file():
        raise MigrationError(f"{label} is not a regular file: {path}")


def _safe_descendant(path: Path, root: Path, label: str) -> Path:
    absolute = path.expanduser().absolute()
    base = root.expanduser().absolute()
    try:
        relative = absolute.relative_to(base)
    except ValueError as exc:
        raise MigrationError(f"{label} escapes its managed root: {path}") from exc
    probe = base
    for part in relative.parts:
        probe /= part
        if _is_reparse(probe):
            raise MigrationError(f"{label} traverses a symlink or reparse point: {probe}")
    return absolute


def _resolve_codex_home(env: Mapping[str, str]) -> tuple[Path, Path]:
    raw = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().absolute()
    root = raw.resolve(strict=False)
    if not root.is_absolute():
        raise MigrationError("CODEX_HOME must resolve to an absolute path")
    return raw, root


def _data_root(env: Mapping[str, str]) -> Path:
    configured = env.get("CODEX_KIT_DATA_ROOT")
    if configured:
        result = Path(configured).expanduser().absolute()
    elif os.name == "nt":
        local = env.get("LOCALAPPDATA")
        if not local:
            raise MigrationError("LOCALAPPDATA is unset; set CODEX_KIT_DATA_ROOT")
        result = Path(local).expanduser().absolute() / "codex-kit"
    elif sys.platform == "darwin":
        result = Path.home() / "Library" / "Application Support" / "codex-kit"
    else:
        result = Path(env.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "codex-kit"
    probe = Path(result.anchor)
    for part in result.parts[1:]:
        probe /= part
        if _is_reparse(probe):
            raise MigrationError(f"migration data root traverses a symlink or reparse point: {probe}")
    for candidate in (result, *result.parents):
        if (candidate / ".git").exists():
            raise MigrationError(f"migration backups cannot be inside a Git worktree: {result}")
    return result


def _decode_scalar(raw: str) -> str:
    value = raw.strip()
    if not value or value[:1] in "[{&*!|>":
        raise MigrationError("legacy ownership scalar is empty or complex")
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MigrationError("invalid double-quoted legacy ownership scalar") from exc
        if not isinstance(decoded, str):
            raise MigrationError("legacy ownership scalar must be text")
        return decoded
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise MigrationError("invalid single-quoted legacy ownership scalar")
        return value[1:-1].replace("''", "'")
    comment = re.search(r"\s+#", value)
    return value[: comment.start()].rstrip() if comment else value


def _legacy_frontmatter(content: bytes, path: Path) -> dict[str, Any]:
    try:
        text = content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as exc:
        raise MigrationError(f"legacy ownership marker is not UTF-8: {path}") from exc
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise MigrationError(f"legacy ownership marker has no frontmatter: {path}")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise MigrationError(f"legacy ownership marker has unterminated frontmatter: {path}") from exc
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for line_number, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise MigrationError(f"legacy ownership frontmatter uses tabs: {path}:{line_number}")
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2:
            raise MigrationError(f"legacy ownership frontmatter has unsafe indentation: {path}:{line_number}")
        match = re.fullmatch(r"([A-Za-z0-9_-]+):(?:[ ]*(.*))", line.strip())
        if not match:
            raise MigrationError(f"legacy ownership frontmatter is too complex: {path}:{line_number}")
        key, raw = match.groups()
        while stack[-1][0] >= indent:
            stack.pop()
        if indent > stack[-1][0] + 2:
            raise MigrationError(f"legacy ownership frontmatter skips indentation: {path}:{line_number}")
        parent = stack[-1][1]
        if key in parent:
            raise MigrationError(f"duplicate legacy ownership key {key!r}: {path}:{line_number}")
        if raw == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _decode_scalar(raw)
    return root


def _owned_stub(content: bytes, path: Path) -> tuple[bool, str | None]:
    try:
        text = content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        return False, None
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return False, None
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        if "plugins-kit-stub" in text or "plugins-kit-forwarder" in text:
            raise MigrationError(f"legacy ownership marker has unterminated frontmatter: {path}")
        return False, None
    header = "\n".join(lines[1:end])
    if "plugins-kit-stub" not in header and "plugins-kit-forwarder" not in header:
        return False, None
    document = _legacy_frontmatter(content, path)
    metadata = document.get("metadata")
    owner: object = metadata
    if isinstance(metadata, dict) and isinstance(metadata.get("plugins-kit-forwarder"), dict):
        owner = {"plugins-kit-stub": "1", **metadata["plugins-kit-forwarder"]}
    if not isinstance(owner, dict):
        raise MigrationError(f"malformed legacy ownership metadata: {path}")
    required = {
        "plugins-kit-stub": "1",
        "generator": LEGACY_GENERATOR,
    }
    exact = all(owner.get(key) == value for key, value in required.items())
    kind = owner.get("source-kind")
    identity = owner.get("source-identity")
    exact = exact and document.get("name") == path.parent.name
    exact = exact and kind in {"plugin", "claude-user"}
    exact = exact and isinstance(identity, str) and bool(identity.strip())
    if not exact:
        raise MigrationError(f"malformed or unowned legacy forwarding marker: {path}")
    return True, str(identity)


def _windows_argv(command: str) -> list[str]:
    """Parse the CommandLineToArgvW quoting subset on every platform."""
    result: list[str] = []
    index = 0
    while index < len(command):
        while index < len(command) and command[index] in " \t":
            index += 1
        if index == len(command):
            break
        argument: list[str] = []
        quoted = False
        while index < len(command):
            if command[index] in " \t" and not quoted:
                break
            slashes = 0
            while index < len(command) and command[index] == "\\":
                slashes += 1
                index += 1
            if index < len(command) and command[index] == '"':
                argument.extend("\\" * (slashes // 2))
                if slashes % 2:
                    argument.append('"')
                else:
                    quoted = not quoted
                index += 1
                continue
            argument.extend("\\" * slashes)
            if index < len(command):
                argument.append(command[index])
                index += 1
        if quoted:
            raise MigrationError("unterminated quote in legacy hook command")
        result.append("".join(argument))
    return result


def _command_argv(command: str) -> list[str]:
    if re.search(r"(?:[A-Za-z]:[\\/]|\\\\)", command):
        return _windows_argv(command)
    try:
        return shlex.split(command, posix=True)
    except ValueError as exc:
        raise MigrationError("invalid quoting in legacy hook command") from exc


def _path_token(value: str) -> str:
    if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", value):
        clean = value[4:] if value.startswith("\\\\?\\") else value
        return ntpath.normcase(ntpath.normpath(clean.replace("/", "\\")))
    return str(Path(value).expanduser().absolute().resolve(strict=False))


def _owned_hook(handler: object, matcher: object, startup_paths: set[str]) -> bool:
    if matcher != LEGACY_HOOK_MATCHER or not isinstance(handler, dict):
        return False
    if (
        handler.get("type") != "command"
        or handler.get("statusMessage") != LEGACY_HOOK_STATUS
        or handler.get("timeout") != 30
        or not isinstance(handler.get("command"), str)
    ):
        return False
    argv = _command_argv(handler["command"])
    if len(argv) != 2 or not PYTHON_EXE.fullmatch(ntpath.basename(argv[0])):
        return False
    return _path_token(argv[1]) in startup_paths


def _hook_artifact(
    path: Path,
    startup_paths: set[str],
    root: Path,
    original: bytes | None = None,
    warnings: list[str] | None = None,
) -> tuple[Artifact | None, int]:
    if original is None:
        if _is_reparse(path):
            raise MigrationError(f"hooks file is a symlink or reparse point: {path}")
        if not path.exists():
            return None, 0
        _require_regular(path, "hooks file")
        original = path.read_bytes()
    bom = original.startswith(b"\xef\xbb\xbf")
    try:
        document = json.loads(original.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"cannot safely parse hooks file: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise MigrationError(f"hooks file must contain a JSON object: {path}")
    hooks = document.get("hooks")
    if hooks is None:
        return None, 0
    if not isinstance(hooks, dict):
        raise MigrationError(f"hooks member must be a JSON object: {path}")
    session = hooks.get("SessionStart")
    if session is None:
        return None, 0
    if not isinstance(session, list):
        raise MigrationError(f"SessionStart hooks must be a JSON array: {path}")
    preserved: list[object] = []
    removed = 0
    for entry in session:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            preserved.append(entry)
            continue
        remaining: list[object] = []
        for handler in entry["hooks"]:
            looks_legacy = isinstance(handler, dict) and (
                handler.get("statusMessage") == LEGACY_HOOK_STATUS
                or "startup_hook.py" in str(handler.get("command", "")).lower()
            )
            try:
                owned = _owned_hook(handler, entry.get("matcher"), startup_paths)
            except MigrationError as exc:
                owned = False
                if warnings is not None and looks_legacy:
                    warnings.append(f"preserved legacy-looking SessionStart hook: {exc}")
            if owned:
                removed += 1
            else:
                remaining.append(handler)
                if warnings is not None and looks_legacy:
                    warnings.append("preserved nonexact legacy-looking SessionStart hook")
        if remaining:
            copied = dict(entry)
            copied["hooks"] = remaining
            preserved.append(copied)
    if not removed:
        return None, 0
    updated = dict(document)
    updated_hooks = dict(hooks)
    if preserved:
        updated_hooks["SessionStart"] = preserved
    else:
        updated_hooks.pop("SessionStart", None)
    updated["hooks"] = updated_hooks
    post: bytes | None
    if updated == {"hooks": {}}:
        post = None
    else:
        encoded = (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        post = (b"\xef\xbb\xbf" + encoded) if bom else encoded
    return Artifact(path, path.relative_to(root).as_posix(), "hook", _sha256(original), post), removed


def _validate_state(content: bytes, path: Path) -> None:
    try:
        value = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"invalid legacy sync state: {path}: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"content", "registry", "claude_head"}:
        raise MigrationError(f"invalid legacy sync state schema: {path}")
    if not isinstance(value["content"], str) or not HEX_64.fullmatch(value["content"]):
        raise MigrationError(f"invalid legacy content fingerprint: {path}")
    if not isinstance(value["registry"], str) or not HEX_64.fullmatch(value["registry"]):
        raise MigrationError(f"invalid legacy registry fingerprint: {path}")
    head = value["claude_head"]
    if head is not None and (not isinstance(head, str) or not GIT_HEAD.fullmatch(head)):
        raise MigrationError(f"invalid legacy Claude head fingerprint: {path}")


def inventory(env: Mapping[str, str]) -> Inventory:
    """Inventory only artifacts with complete historical ownership fingerprints."""
    raw_home, root = _resolve_codex_home(env)
    if not root.exists() or not root.is_dir():
        raise MigrationError(f"CODEX_HOME is not a directory: {root}")
    artifacts: list[Artifact] = []
    identities: list[str] = []
    preserved: list[str] = []
    hook_warnings: list[str] = []
    skills = root / "skills"
    if _is_reparse(skills):
        raise MigrationError(f"skills destination is unsafe: {skills}")
    if skills.exists():
        if not skills.is_dir():
            raise MigrationError(f"skills destination is unsafe: {skills}")
        for child in sorted(skills.iterdir()):
            if _is_reparse(child):
                raise MigrationError(f"skills destination has a symlink or reparse descendant: {child}")
            if not child.is_dir():
                continue
            skill = child / "SKILL.md"
            if _is_reparse(skill):
                raise MigrationError(f"skill file is a symlink or reparse point: {skill}")
            if not skill.exists():
                continue
            _require_regular(skill, "skill file")
            content = skill.read_bytes()
            owned, identity = _owned_stub(content, skill)
            if not owned:
                continue
            if identity is None:  # Defensive invariant after the ownership predicate.
                raise MigrationError(f"owned legacy stub has no source identity: {skill}")
            artifacts.append(
                Artifact(skill, skill.relative_to(root).as_posix(), "stub", _sha256(content), None, identity)
            )
            identities.append(identity)
            if any(item.name != "SKILL.md" for item in child.iterdir()):
                preserved.append(str(child))
    state = root / LEGACY_STATE
    state_count = 0
    if _is_reparse(state):
        raise MigrationError(f"legacy sync state is a symlink or reparse point: {state}")
    if state.exists():
        _require_regular(state, "legacy sync state")
        content = state.read_bytes()
        _validate_state(content, state)
        artifacts.append(Artifact(state, state.relative_to(root).as_posix(), "state", _sha256(content), None))
        state_count = 1
    startup_paths = {
        _path_token(str(raw_home / "scripts" / "startup_hook.py")),
        _path_token(str(root / "scripts" / "startup_hook.py")),
    }
    hook, hook_count = _hook_artifact(
        root / "hooks.json", startup_paths, root, warnings=hook_warnings
    )
    if hook:
        artifacts.append(hook)
    gateway_count = 0
    for relative, expected_hash in LEGACY_GATEWAY_HASHES.items():
        path = root / relative
        if _is_reparse(path):
            raise MigrationError(f"legacy gateway file is a symlink or reparse point: {path}")
        if not path.exists():
            continue
        _require_regular(path, "legacy gateway file")
        content = path.read_bytes()
        normalized = content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        if _sha256(normalized) != expected_hash:
            continue
        artifacts.append(Artifact(path, relative, "old_gateway", _sha256(content), None))
        gateway_count += 1
    artifacts.sort(key=lambda item: item.relative)
    return Inventory(
        codex_home=root,
        artifacts=tuple(artifacts),
        hook_count=hook_count,
        stub_count=sum(item.kind == "stub" for item in artifacts),
        state_count=state_count,
        gateway_count=gateway_count,
        identities=tuple(sorted(set(identities))),
        preserved_stub_directories=tuple(sorted(preserved)),
        hook_warnings=tuple(sorted(set(hook_warnings))),
    )


def _json_command(runner: CommandRunner, arguments: Sequence[str], label: str) -> Any:
    try:
        result = runner.run(arguments)
    except subprocess.TimeoutExpired as exc:
        raise MigrationError(f"{label} timed out") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise MigrationError(f"{label} failed: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"{label} returned invalid JSON") from exc


def _catalog(runner: CommandRunner) -> dict[str, list[dict[str, Any]]]:
    value = _json_command(runner, ["plugin", "list", "--json"], "Codex plugin list")
    if not isinstance(value, dict):
        raise MigrationError("Codex plugin list must be an object")
    result: dict[str, list[dict[str, Any]]] = {}
    for field in ("installed", "available"):
        records = value.get(field, [])
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise MigrationError(f"Codex plugin list has an invalid {field} array")
        result[field] = records
    return result


def _marketplaces(runner: CommandRunner) -> list[dict[str, Any]]:
    value = _json_command(
        runner, ["plugin", "marketplace", "list", "--json"], "Codex marketplace list"
    )
    records = value.get("marketplaces") if isinstance(value, dict) else None
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise MigrationError("Codex marketplace list has no valid marketplaces array")
    return records


def _read_manifest(path: Path, label: str) -> dict[str, Any]:
    _require_regular(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"{label} must be an object: {path}")
    return value


def _source_contract(bridge_root: Path) -> tuple[str, Path]:
    compatibility = _read_manifest(
        bridge_root / ".codex-plugin" / "plugin.json", "authored compatibility manifest"
    )
    version = compatibility.get("version")
    if (
        compatibility.get("name") != AUTHORED_NAME
        or not isinstance(version, str)
        or compatibility.get("version") != version
    ):
        raise MigrationError("running authored plugin manifests disagree on name or version")
    _require_regular(
        bridge_root / "skills" / "claude-plugins" / "SKILL.md", "authored gateway skill"
    )
    return version, bridge_root


def _verify_installed_root(path: Path, codex_home: Path, expected_version: str) -> Path:
    cache = codex_home / "plugins" / "cache"
    root = _safe_descendant(path, cache, "installedPath")
    compatibility = _read_manifest(
        root / ".codex-plugin" / "plugin.json", "installed authored compatibility manifest"
    )
    if (
        compatibility.get("name") != AUTHORED_NAME
        or compatibility.get("version") != expected_version
    ):
        raise MigrationError(
            f"installed authored plugin does not match running version {expected_version}: {root}"
        )
    _require_regular(root / "skills" / "claude-plugins" / "SKILL.md", "installed gateway skill")
    _require_regular(root / "scripts" / "bridge.py", "installed bridge entrypoint")
    _require_regular(root / "scripts" / "migration.py", "installed migration module")
    return root


def _official_git_source(record: Mapping[str, Any]) -> bool:
    source = record.get("marketplaceSource")
    if not isinstance(source, dict) or source.get("sourceType") != "git":
        return False
    value = str(source.get("source", "")).lower().rstrip("/")
    return value.removesuffix(".git") == "https://github.com/kitaekatt/codex-kit"


def _validate_existing_marketplace(record: Mapping[str, Any], repository: Path) -> bool:
    if _official_git_source(record):
        return True
    root = record.get("root")
    if not isinstance(root, str):
        raise MigrationError("existing codex-kit marketplace has no source root")
    if Path(root).expanduser().resolve(strict=False) != repository.resolve(strict=False):
        raise MigrationError(
            f"refusing to replace unrelated marketplace named codex-kit: {root}"
        )
    return False


def _default_sync(installed_root: Path, env: Mapping[str, str]) -> Mapping[str, Any]:
    if os.name == "nt":
        command = [env.get("COMSPEC", "cmd.exe"), "/d", "/c", str(installed_root / "scripts" / "launch.cmd")]
    else:
        command = [str(installed_root / "scripts" / "launch.sh")]
    try:
        result = subprocess.run(
            [*command, "sync", "--install"],
            text=True,
            capture_output=True,
            shell=False,
            timeout=120,
            env=dict(env),
        )
    except subprocess.TimeoutExpired as exc:
        raise MigrationError("installed gateway sync timed out") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise MigrationError(f"installed gateway sync failed: {detail}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MigrationError("installed gateway sync returned invalid JSON") from exc
    if not isinstance(value, dict) or value.get("status") == "error":
        raise MigrationError(f"installed gateway sync failed: {value!r}")
    return value


def _send(process: subprocess.Popen[str], message: Mapping[str, Any]) -> None:
    if process.stdin is None:
        raise MigrationError("Codex app-server has no stdin")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_messages(stream: TextIO, messages: queue.Queue[dict[str, Any] | None]) -> None:
    for line in stream:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            messages.put(value)
    messages.put(None)


def _response(
    messages: queue.Queue[dict[str, Any] | None], request_id: int, deadline: float
) -> dict[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MigrationError(f"Codex app-server response {request_id} timed out")
        try:
            message = messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise MigrationError(f"Codex app-server response {request_id} timed out") from exc
        if message is None:
            raise MigrationError("Codex app-server exited before catalog verification")
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise MigrationError(f"Codex app-server returned an error: {message['error']}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise MigrationError("Codex app-server returned no result object")
        return result


def discover_codex_skills(
    executable: str, env: Mapping[str, str], cwd: Path
) -> list[dict[str, Any]]:
    """Read the live Codex skill catalog through app-server skills/list."""
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
        )
        if process.stdout is None:
            process.terminate()
            raise MigrationError("Codex app-server has no stdout")
        messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        reader = threading.Thread(target=_read_messages, args=(process.stdout, messages), daemon=True)
        reader.start()
        deadline = time.monotonic() + 45.0
        try:
            _send(
                process,
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {"name": "codex-kit-migration", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                },
            )
            _response(messages, 1, deadline)
            _send(process, {"method": "initialized", "params": {}})
            _send(
                process,
                {
                    "method": "skills/list",
                    "id": 2,
                    "params": {"cwds": [str(cwd)], "forceReload": True},
                },
            )
            result = _response(messages, 2, deadline)
        finally:
            if process.stdin:
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
        if process.returncode:
            stderr.seek(0)
            raise MigrationError(
                f"Codex app-server exited with status {process.returncode}: {stderr.read()[-2000:]}"
            )
        entries = result.get("data")
        if not isinstance(entries, list):
            raise MigrationError("Codex skills/list response has no data array")
        found: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("cwd") != str(cwd):
                continue
            if entry.get("errors"):
                raise MigrationError(f"Codex skill discovery errors: {entry['errors']!r}")
            records = entry.get("skills")
            if isinstance(records, list):
                found.extend(item for item in records if isinstance(item, dict))
        return found


def _load_installed_bridge(installed_root: Path) -> ModuleType:
    path = installed_root / "scripts" / "bridge.py"
    spec = importlib.util.spec_from_file_location(
        f"codex_kit_installed_bridge_{secrets.token_hex(6)}", path
    )
    if spec is None or spec.loader is None:
        raise MigrationError(f"cannot load installed bridge contract: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _expected_generated(
    installed_root: Path, env: Mapping[str, str], skipped: Sequence[str]
) -> tuple[dict[str, tuple[str, dict[str, str], Path]], set[str]]:
    bridge = _load_installed_bridge(installed_root)
    try:
        paths = bridge.paths_for(env, installed_root / "scripts" / "bridge.py")
        registry, personal = bridge.claude_paths(env)
        overrides = bridge.load_overrides(paths.overrides_file)
        sources, duplicates = bridge.installed_sources(registry, personal)
    except Exception as exc:
        raise MigrationError(f"cannot derive native wrapper catalog from current Claude sources: {exc}") from exc
    skipped_set = set(skipped)
    if not set(duplicates).issubset(skipped_set):
        raise MigrationError(f"gateway sync omitted duplicate-source diagnostics: {duplicates}")
    expected: dict[str, tuple[str, dict[str, str], Path]] = {}
    skills: set[str] = set()
    for source in sources:
        if source.name in skipped_set:
            continue
        files, _ = bridge.rendered_plugin(source, overrides)
        version = json.loads(files["plugin.json"])["version"]
        root = paths.plugins_root / source.name
        expected[source.name] = (version, files, root)
        skills.update(f"{source.name}:{skill.folder}" for skill in source.skills)
    return expected, skills


def _verify_catalog(
    runner: CommandRunner,
    installed_root: Path,
    expected_version: str,
    env: Mapping[str, str],
    discoverer: SkillDiscoverer,
    skipped: Sequence[str],
) -> tuple[dict[str, str], set[str], list[dict[str, Any]]]:
    catalog = _catalog(runner)
    authored = [item for item in catalog["installed"] if item.get("pluginId") == AUTHORED_ID]
    if len(authored) != 1:
        raise MigrationError("native claude-plugins-kit is not installed exactly once")
    record = authored[0]
    if (
        record.get("version") != expected_version
        or record.get("installed") is not True
        or record.get("enabled") is not True
    ):
        raise MigrationError(
            f"native claude-plugins-kit must be installed and enabled at version {expected_version}"
        )
    _verify_installed_root(installed_root, _resolve_codex_home(env)[1], expected_version)
    expected_plugins, expected_skills = _expected_generated(installed_root, env, skipped)
    generated_records = [
        item for item in catalog["installed"] if item.get("marketplaceName") == GENERATED_MARKETPLACE
    ]
    records_by_name: dict[str, list[dict[str, Any]]] = {}
    for item in generated_records:
        name = item.get("name")
        if isinstance(name, str):
            records_by_name.setdefault(name, []).append(item)
    if set(records_by_name) != set(expected_plugins):
        raise MigrationError(
            "installed generated plugins do not match the current Claude source catalog: "
            f"expected {sorted(expected_plugins)}, found {sorted(records_by_name)}"
        )
    versions: dict[str, str] = {}
    for name, (version, files, source_root) in expected_plugins.items():
        records = records_by_name[name]
        if len(records) != 1:
            raise MigrationError(f"generated plugin is not installed exactly once: {name}")
        generated = records[0]
        source = generated.get("source")
        if (
            generated.get("pluginId") != f"{name}@{GENERATED_MARKETPLACE}"
            or generated.get("version") != version
            or generated.get("installed") is not True
            or generated.get("enabled") is not True
            or not isinstance(source, dict)
            or not isinstance(source.get("path"), str)
            or Path(source["path"]).expanduser().resolve(strict=False) != source_root.resolve(strict=False)
        ):
            raise MigrationError(f"generated plugin is missing, disabled, stale, or malformed: {name}")
        current = {
            path.relative_to(source_root).as_posix(): path.read_text(encoding="utf-8")
            for path in source_root.rglob("*")
            if path.is_file() and path.name != ".claude-plugins-kit-owner.json"
        }
        expected_files = {key: value for key, value in files.items() if key != ".claude-plugins-kit-owner.json"}
        if current != expected_files:
            raise MigrationError(f"generated plugin source does not match current Claude sources: {name}")
        versions[name] = version
    discovered = discoverer(runner.executable, env, Path.cwd())
    by_name: dict[str, dict[str, Any]] = {}
    for item in discovered:
        name = item.get("name")
        if not isinstance(name, str):
            continue
        if name in by_name:
            raise MigrationError(f"Codex skills/list returned duplicate skill name: {name}")
        by_name[name] = item
    gateway = by_name.get("claude-plugins-kit:claude-plugins")
    gateway_path = installed_root / "skills" / "claude-plugins" / "SKILL.md"
    if (
        not gateway
        or gateway.get("pluginId") != AUTHORED_ID
        or not isinstance(gateway.get("path"), str)
        or Path(gateway["path"]).resolve(strict=False) != gateway_path.resolve(strict=False)
    ):
        raise MigrationError("native claude-plugins-kit gateway is absent from Codex skills/list")
    missing = sorted(expected_skills - set(by_name))
    if missing:
        raise MigrationError(f"generated skills are absent from Codex skills/list: {missing}")
    cache = _resolve_codex_home(env)[1] / "plugins" / "cache"
    for qualified in sorted(expected_skills):
        record = by_name[qualified]
        plugin = qualified.split(":", 1)[0]
        if (
            record.get("pluginId") != f"{plugin}@{GENERATED_MARKETPLACE}"
            or record.get("enabled") is False
        ):
            raise MigrationError(f"generated skill has the wrong pluginId: {qualified}")
        skill_path = record.get("path")
        if not isinstance(skill_path, str):
            raise MigrationError(f"generated skill has no path: {qualified}")
        path = _safe_descendant(Path(skill_path), cache, f"generated skill {qualified}")
        _require_regular(path, f"generated skill {qualified}")
        folder = qualified.split(":", 1)[1]
        expected_content = expected_plugins[plugin][1][f"skills/{folder}/SKILL.md"]
        if path.read_text(encoding="utf-8") != expected_content:
            raise MigrationError(f"generated skill content is stale: {qualified}")
    return versions, expected_skills, discovered


def _artifact_record(artifact: Artifact) -> dict[str, Any]:
    return {
        "path": str(artifact.path),
        "relative": artifact.relative,
        "kind": artifact.kind,
        "identity": artifact.identity,
        "pre_sha256": artifact.pre_hash,
        "post_sha256": _sha256(artifact.post) if artifact.post is not None else None,
        "post_base64": base64.b64encode(artifact.post).decode("ascii") if artifact.post is not None else None,
        "state": "pending",
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_journal(path: Path, journal: dict[str, Any]) -> None:
    journal["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(path, journal)


def _backup(inventory_value: Inventory, data_root: Path, expected_version: str) -> tuple[Path, dict[str, Any]]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = data_root / "migration-backups" / f"{stamp}-{secrets.token_hex(4)}"
    backup.mkdir(parents=True, exist_ok=False)
    backup.parent.chmod(0o700)
    backup.chmod(0o700)
    records = [_artifact_record(item) for item in inventory_value.artifacts]
    for artifact, record in zip(inventory_value.artifacts, records):
        _require_regular(artifact.path, "legacy artifact selected for backup")
        if _sha256(artifact.path.read_bytes()) != artifact.pre_hash:
            raise MigrationError(f"legacy artifact changed during inventory: {artifact.path}")
        destination = backup / "codex-home" / artifact.relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact.path, destination)
        if _sha256(destination.read_bytes()) != artifact.pre_hash:
            raise MigrationError(f"backup verification failed: {destination}")
        record["backup"] = str(destination)
    journal: dict[str, Any] = {
        "schema": JOURNAL_SCHEMA,
        "migration": MIGRATION_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "codex_home": str(inventory_value.codex_home),
        "expected_version": expected_version,
        "phase": "backup",
        "error": None,
        "legacy": _legacy_summary(inventory_value),
        "artifacts": records,
        "operations": [],
        "native": {},
    }
    _write_journal(backup / "journal.json", journal)
    return backup, journal


def _validate_journal(
    backup: Path,
    value: dict[str, Any],
    raw_home: Path,
    codex_home: Path,
    expected_version: str,
) -> None:
    if value.get("schema") != JOURNAL_SCHEMA or value.get("migration") != MIGRATION_ID:
        raise MigrationError(f"invalid migration journal ownership: {backup / 'journal.json'}")
    previous_version = value.get("expected_version")
    def stable_version(version: object) -> tuple[int, ...]:
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise MigrationError(f"invalid migration journal release version: {version!r}")
        return tuple(int(part) for part in version.split("."))
    if value.get("codex_home") != str(codex_home) or stable_version(previous_version) > stable_version(expected_version):
        raise MigrationError(
            f"unfinished migration journal targets another home or a newer plugin version: {backup / 'journal.json'}"
        )
    artifacts = value.get("artifacts")
    legacy = value.get("legacy")
    if not isinstance(artifacts, list) or not isinstance(legacy, dict):
        raise MigrationError(f"invalid migration journal schema: {backup / 'journal.json'}")
    if value.get("phase") not in {"backup", "install", "sync", "verify", "cleanup"}:
        raise MigrationError(f"invalid unfinished migration journal phase: {backup / 'journal.json'}")
    seen: set[str] = set()
    owned_hook_count = 0
    if raw_home.resolve(strict=False) != codex_home:
        raise MigrationError("current CODEX_HOME alias no longer resolves to the journal home")
    startup_paths = {
        _path_token(str(raw_home / "scripts" / "startup_hook.py")),
        _path_token(str(codex_home / "scripts" / "startup_hook.py")),
    }
    for record in artifacts:
        if not isinstance(record, dict):
            raise MigrationError(f"invalid migration journal artifact: {backup / 'journal.json'}")
        relative = record.get("relative")
        raw_path = record.get("path")
        kind = record.get("kind")
        if (
            not isinstance(relative, str)
            or not isinstance(raw_path, str)
            or kind not in {"hook", "stub", "state", "old_gateway"}
            or relative in seen
        ):
            raise MigrationError(f"invalid migration journal artifact identity: {backup / 'journal.json'}")
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or ".." in parts:
            raise MigrationError(f"migration journal artifact escapes CODEX_HOME: {relative}")
        expected_path = codex_home.joinpath(*parts).absolute()
        if Path(raw_path).expanduser().absolute() != expected_path:
            raise MigrationError(f"migration journal path does not match its relative target: {raw_path}")
        _safe_descendant(expected_path, codex_home, "migration journal artifact")
        if kind == "hook" and relative != "hooks.json":
            raise MigrationError(f"invalid hook target in migration journal: {relative}")
        if kind == "state" and relative != LEGACY_STATE:
            raise MigrationError(f"invalid state target in migration journal: {relative}")
        if kind == "stub" and not (
            len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md"
        ):
            raise MigrationError(f"invalid legacy stub target in migration journal: {relative}")
        if kind == "old_gateway" and relative not in LEGACY_GATEWAY_HASHES:
            raise MigrationError(f"invalid legacy gateway target in migration journal: {relative}")
        pre = record.get("pre_sha256")
        post_hash = record.get("post_sha256")
        encoded = record.get("post_base64")
        if not isinstance(pre, str) or not HEX_64.fullmatch(pre):
            raise MigrationError(f"invalid migration journal preimage hash: {relative}")
        backup_path = backup / "codex-home" / relative
        if record.get("backup") != str(backup_path):
            raise MigrationError(f"migration journal backup path mismatch: {relative}")
        _safe_descendant(backup_path, backup, "migration backup")
        _require_regular(backup_path, "migration backup")
        backup_content = backup_path.read_bytes()
        if _sha256(backup_content) != pre:
            raise MigrationError(f"migration backup hash mismatch: {backup_path}")
        expected_post: bytes | None = None
        if kind == "stub":
            owned, identity = _owned_stub(backup_content, expected_path)
            if not owned or identity != record.get("identity"):
                raise MigrationError(f"migration backup is not the recorded owned stub: {backup_path}")
        elif kind == "state":
            _validate_state(backup_content, backup_path)
        elif kind == "old_gateway":
            normalized = backup_content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
            if _sha256(normalized) != LEGACY_GATEWAY_HASHES[relative]:
                raise MigrationError(f"migration backup is not the retired gateway file: {backup_path}")
        else:
            hook, count = _hook_artifact(expected_path, startup_paths, codex_home, backup_content)
            if hook is None or count < 1:
                raise MigrationError(f"migration backup has no exact-owned hook: {backup_path}")
            owned_hook_count += count
            expected_post = hook.post
        try:
            actual_post = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else None
        except (binascii.Error, ValueError) as exc:
            raise MigrationError(f"migration journal has invalid postimage encoding: {relative}") from exc
        if actual_post != expected_post or post_hash != (
            _sha256(expected_post) if expected_post is not None else None
        ):
            raise MigrationError(f"migration journal has an invalid cleanup postimage: {relative}")
        current_hash = None
        if expected_path.exists():
            _require_regular(expected_path, "migration artifact")
            current_hash = _sha256(expected_path.read_bytes())
        if current_hash not in {pre, post_hash} or (current_hash is None and post_hash is not None):
            raise MigrationError(
                f"legacy artifact drift at {expected_path}; expected its backup preimage or applied postimage"
            )
        seen.add(relative)
    expected_counts = {
        "hooks": owned_hook_count,
        "stubs": sum(item.get("kind") == "stub" for item in artifacts),
        "state": sum(item.get("kind") == "state" for item in artifacts),
        "old_gateway": sum(item.get("kind") == "old_gateway" for item in artifacts),
    }
    identities = sorted(
        {str(item["identity"]) for item in artifacts if item.get("kind") == "stub"}
    )
    if any(legacy.get(key) != count for key, count in expected_counts.items()) or legacy.get(
        "identities"
    ) != identities:
        raise MigrationError(f"migration journal summary does not match its artifacts: {backup / 'journal.json'}")


def _load_active_journal(
    backup_base: Path, raw_home: Path, codex_home: Path, expected_version: str
) -> tuple[Path, dict[str, Any]] | None:
    if _is_reparse(backup_base):
        raise MigrationError(f"migration backup directory is unsafe: {backup_base}")
    if not backup_base.exists():
        return None
    if not backup_base.is_dir():
        raise MigrationError(f"migration backup directory is unsafe: {backup_base}")
    matches: list[tuple[Path, dict[str, Any]]] = []
    for child in backup_base.iterdir():
        if _is_reparse(child):
            raise MigrationError(f"migration backup entry is a symlink or reparse point: {child}")
        if not child.is_dir():
            continue
        journal_path = child / "journal.json"
        if _is_reparse(journal_path):
            raise MigrationError(f"migration journal is a symlink or reparse point: {journal_path}")
        if not journal_path.exists():
            continue
        _require_regular(journal_path, "migration journal")
        try:
            value = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MigrationError(f"invalid migration journal: {journal_path}: {exc}") from exc
        if not isinstance(value, dict):
            raise MigrationError(f"invalid migration journal: {journal_path}")
        if value.get("migration") != MIGRATION_ID:
            continue
        if value.get("codex_home") != str(codex_home):
            continue
        if value.get("phase") == "complete":
            continue
        _validate_journal(child, value, raw_home, codex_home, expected_version)
        matches.append((child, value))
    if len(matches) > 1:
        raise MigrationError(
            "multiple unfinished migration journals exist; inspect data_root/migration-backups"
        )
    return matches[0] if matches else None


def _legacy_summary(value: Inventory | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Inventory):
        return {
            "hooks": value.hook_count,
            "stubs": value.stub_count,
            "state": value.state_count,
            "old_gateway": value.gateway_count,
            "identities": list(value.identities),
            "preserved_stub_directories": list(value.preserved_stub_directories),
            "hook_warnings": list(value.hook_warnings),
            "trust_tables_preserved": value.hook_count > 0,
        }
    return dict(value)


def _result(
    *,
    status: str,
    changed: bool,
    needed: bool,
    phase: str,
    message: str,
    legacy: Mapping[str, Any],
    expected_version: str,
    backup: Path | None = None,
    restart: bool = False,
    native: Mapping[str, Any] | None = None,
    partial_cleanup: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "changed": changed,
        "migration_needed": needed,
        "phase": phase,
        "message": message,
        "legacy": dict(legacy),
        "native": {
            "plugin_id": AUTHORED_ID,
            "expected_version": expected_version,
            **dict(native or {}),
        },
        "backup": str(backup) if backup else None,
        "restart_required": restart,
        "partial_cleanup": partial_cleanup,
    }


def _record_operation(
    journal_path: Path,
    journal: dict[str, Any],
    action: str,
    target: str,
    state: str,
) -> None:
    operations = journal.setdefault("operations", [])
    if not isinstance(operations, list):
        raise MigrationError("migration journal has an invalid operations list")
    existing = next(
        (item for item in operations if item.get("action") == action and item.get("target") == target),
        None,
    )
    if existing is None:
        existing = {"action": action, "target": target, "state": state}
        operations.append(existing)
    else:
        existing["state"] = state
    _write_journal(journal_path, journal)


def _cleanup_artifacts(
    journal_path: Path,
    journal: dict[str, Any],
    fault: FaultInjector | None,
) -> None:
    artifacts = journal.get("artifacts")
    if not isinstance(artifacts, list):
        raise MigrationError("migration journal has no artifact inventory")
    codex_home = Path(str(journal["codex_home"]))
    for record in artifacts:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise MigrationError("migration journal contains an invalid artifact")
        path = Path(record["path"])
        _safe_descendant(path, codex_home, "legacy cleanup target")
        pre = record.get("pre_sha256")
        post_hash = record.get("post_sha256")
        post_encoded = record.get("post_base64")
        if not isinstance(pre, str) or not HEX_64.fullmatch(pre):
            raise MigrationError(f"migration journal has an invalid preimage hash: {path}")
        try:
            post = base64.b64decode(post_encoded, validate=True) if isinstance(post_encoded, str) else None
        except (binascii.Error, ValueError) as exc:
            raise MigrationError(f"migration journal has invalid postimage encoding: {path}") from exc
        current = _sha256(path.read_bytes()) if path.is_file() and not _is_reparse(path) else None
        if current == post_hash or (post_hash is None and not path.exists()):
            record["state"] = "applied"
            _write_journal(journal_path, journal)
            continue
        if current != pre:
            raise MigrationError(
                f"legacy artifact drift at {path}; expected its backup preimage or applied postimage"
            )
        record["state"] = "planned"
        _write_journal(journal_path, journal)
        if post is None:
            path.unlink()
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(post)
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        if fault:
            fault("cleanup", path)
        record["state"] = "applied"
        _write_journal(journal_path, journal)
    candidates: set[Path] = set()
    for record in artifacts:
        if record.get("kind") in {"stub", "old_gateway"}:
            candidates.add(Path(str(record["path"])).parent)
    for directory in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        while directory != codex_home / "skills" and directory != codex_home:
            _safe_descendant(directory, codex_home, "legacy cleanup directory")
            action = "rmdir"
            _record_operation(journal_path, journal, action, str(directory), "planned")
            try:
                directory.rmdir()
            except OSError:
                _record_operation(journal_path, journal, action, str(directory), "preserved")
                break
            _record_operation(journal_path, journal, action, str(directory), "applied")
            directory = directory.parent


def _identity_statuses(identities: Sequence[str], expected: set[str], skipped: Sequence[str]) -> tuple[list[str], list[str]]:
    collisions: list[str] = []
    retired: list[str] = []
    skipped_set = set(skipped)
    for identity in identities:
        source, separator, skill = identity.rpartition("/")
        plugin = source.split("@", 1)[0]
        qualified = f"{plugin}:{skill}" if separator else ""
        if plugin in skipped_set:
            collisions.append(identity)
        elif qualified not in expected:
            retired.append(identity)
    return sorted(collisions), sorted(retired)


def migrate(
    *,
    env: Mapping[str, str],
    runner: CommandRunner,
    bridge_root: Path,
    install: bool = False,
    sync_launcher: SyncLauncher | None = None,
    discoverer: SkillDiscoverer | None = None,
    fault: FaultInjector | None = None,
) -> dict[str, Any]:
    """Preview or safely apply migration from exact-owned standalone artifacts."""
    phase = "inventory"
    backup: Path | None = None
    legacy: dict[str, Any] = {"hooks": 0, "stubs": 0, "state": 0, "old_gateway": 0, "identities": []}
    expected_version = "unknown"
    journal: dict[str, Any] | None = None
    journal_path: Path | None = None
    migration_lock: MigrationLock | None = None
    try:
        expected_version, _ = _source_contract(bridge_root)
        raw_home, codex_home = _resolve_codex_home(env)
        data_root = _data_root(env)
        if install:
            migration_lock = MigrationLock(data_root / ".migration.lock")
            migration_lock.__enter__()
        active = _load_active_journal(
            data_root / "migration-backups", raw_home, codex_home, expected_version
        )
        if active:
            backup, journal = active
            journal_path = backup / "journal.json"
            legacy = _legacy_summary(journal.get("legacy", {}))
        else:
            current = inventory(env)
            legacy = _legacy_summary(current)
        needed = bool(sum(int(legacy.get(key, 0)) for key in ("hooks", "stubs", "state", "old_gateway")))
        catalog = _catalog(runner)
        installed_now = [item for item in catalog["installed"] if item.get("pluginId") == AUTHORED_ID]
        available_now = [item for item in catalog["available"] if item.get("pluginId") == AUTHORED_ID]
        native_preview = {
            "installed": len(installed_now) == 1 and installed_now[0].get("installed") is True,
            "enabled": len(installed_now) == 1 and installed_now[0].get("enabled") is True,
            "available": len(available_now) == 1,
        }
        if not needed:
            return _result(
                status="unchanged",
                changed=False,
                needed=False,
                phase="complete",
                message="No exact-owned standalone bridge artifacts remain.",
                legacy=legacy,
                expected_version=expected_version,
                native=native_preview,
            )
        if not install:
            resumed = f" An interrupted migration is recoverable from {backup}; rerun migrate --install." if backup else ""
            return _result(
                status="changed",
                changed=False,
                needed=True,
                phase=str(journal.get("phase", "inventory")) if journal else "inventory",
                message=(
                    "Exact-owned standalone bridge artifacts require migration. Run migrate --install; "
                    "legacy cleanup will occur only after native catalog verification." + resumed
                ),
                legacy=legacy,
                expected_version=expected_version,
                backup=backup,
                native=native_preview,
            )
        if journal is None:
            current = inventory(env)
            backup, journal = _backup(current, data_root, expected_version)
            journal_path = backup / "journal.json"
            legacy = _legacy_summary(current)
        if journal_path is None or backup is None:
            raise MigrationError("migration backup journal was not initialized")
        if journal["expected_version"] != expected_version:
            # The entire old journal and its backup preimages were validated above.
            # Repeat installation and native verification with this newer release.
            journal["previous_version"] = journal["expected_version"]
            journal["expected_version"] = expected_version
        phase = "install"
        journal["phase"] = phase
        journal["error"] = None
        _write_journal(journal_path, journal)
        marketplaces = [item for item in _marketplaces(runner) if item.get("name") == AUTHORED_MARKETPLACE]
        if len(marketplaces) > 1:
            raise MigrationError("multiple Codex marketplaces are named codex-kit")
        repository = bridge_root.parents[1]
        if not marketplaces:
            _record_operation(journal_path, journal, "marketplace-add", AUTHORED_MARKETPLACE, "planned")
            added = _json_command(
                runner,
                ["plugin", "marketplace", "add", AUTHORED_MARKETPLACE_SOURCE, "--json"],
                "Codex marketplace add",
            )
            if not isinstance(added, dict) or added.get("marketplaceName") != AUTHORED_MARKETPLACE:
                raise MigrationError("Codex marketplace add returned the wrong marketplace")
            _record_operation(journal_path, journal, "marketplace-add", AUTHORED_MARKETPLACE, "applied")
        else:
            should_upgrade = _validate_existing_marketplace(marketplaces[0], repository)
            if should_upgrade:
                _record_operation(journal_path, journal, "marketplace-upgrade", AUTHORED_MARKETPLACE, "planned")
                _json_command(
                    runner,
                    ["plugin", "marketplace", "upgrade", AUTHORED_MARKETPLACE, "--json"],
                    "Codex marketplace upgrade",
                )
                _record_operation(journal_path, journal, "marketplace-upgrade", AUTHORED_MARKETPLACE, "applied")
        _record_operation(journal_path, journal, "plugin-add", AUTHORED_ID, "planned")
        added_plugin = _json_command(
            runner, ["plugin", "add", AUTHORED_ID, "--json"], "native bridge install"
        )
        if (
            not isinstance(added_plugin, dict)
            or added_plugin.get("pluginId") != AUTHORED_ID
            or not isinstance(added_plugin.get("installedPath"), str)
        ):
            raise MigrationError("native bridge install returned no trustworthy installedPath")
        installed_root = _verify_installed_root(
            Path(added_plugin["installedPath"]), codex_home, expected_version
        )
        journal["native"] = {"installed_path": str(installed_root)}
        _record_operation(journal_path, journal, "plugin-add", AUTHORED_ID, "applied")
        phase = "sync"
        journal["phase"] = phase
        _write_journal(journal_path, journal)
        _record_operation(journal_path, journal, "sync-install", str(installed_root), "planned")
        sync_result = (sync_launcher or _default_sync)(installed_root, env)
        if not isinstance(sync_result, Mapping) or sync_result.get("status") not in {"changed", "unchanged"}:
            raise MigrationError(f"installed gateway sync failed: {sync_result!r}")
        skipped = sync_result.get("skipped", [])
        if not isinstance(skipped, list) or any(not isinstance(item, str) for item in skipped):
            raise MigrationError("installed gateway sync returned an invalid skipped list")
        _record_operation(journal_path, journal, "sync-install", str(installed_root), "applied")
        phase = "verify"
        journal["phase"] = phase
        _write_journal(journal_path, journal)
        versions, expected_skills, _ = _verify_catalog(
            runner,
            installed_root,
            expected_version,
            env,
            discoverer or discover_codex_skills,
            skipped,
        )
        collision_identities, retired_identities = _identity_statuses(
            list(legacy.get("identities", [])), expected_skills, skipped
        )
        native = {
            "installed": True,
            "enabled": True,
            "installed_path": str(installed_root),
            "generated_versions": versions,
            "generated_skills": sorted(expected_skills),
            "skipped": sorted(skipped),
            "collision_identities": collision_identities,
            "retired_identities": retired_identities,
        }
        journal["native"] = native
        phase = "cleanup"
        journal["phase"] = phase
        _write_journal(journal_path, journal)
        _cleanup_artifacts(journal_path, journal, fault)
        phase = "complete"
        journal["phase"] = phase
        journal["error"] = None
        _write_journal(journal_path, journal)
        return _result(
            status="changed",
            changed=True,
            needed=False,
            phase=phase,
            message=(
                "Native bridge and generated skill catalog verified; exact-owned standalone artifacts removed. "
                "Restart Codex to load the migrated catalog."
            ),
            legacy=legacy,
            expected_version=expected_version,
            backup=backup,
            restart=True,
            native=native,
        )
    except (MigrationError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        partial_cleanup = bool(
            journal
            and isinstance(journal.get("artifacts"), list)
            and any(item.get("state") in {"planned", "applied"} for item in journal["artifacts"] if isinstance(item, dict))
        )
        if journal is not None and journal_path is not None:
            journal["phase"] = phase
            journal["error"] = str(exc)
            _write_journal(journal_path, journal)
        recovery = (
            "Cleanup is partial; keep the backup and rerun migrate --install after fixing the diagnostic."
            if partial_cleanup
            else "Legacy cleanup did not begin; fix the diagnostic and rerun migrate --install."
        )
        return _result(
            status="error",
            changed=False,
            needed=True,
            phase=phase,
            message=f"Migration stopped during {phase}: {exc}. {recovery}",
            legacy=legacy,
            expected_version=expected_version,
            backup=backup,
            restart=phase in {"sync", "verify", "cleanup", "complete"},
            partial_cleanup=partial_cleanup,
        )
    finally:
        if migration_lock is not None:
            migration_lock.__exit__(None, None, None)

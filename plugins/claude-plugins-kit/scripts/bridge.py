#!/usr/bin/env python3
"""Portable Claude-to-Codex skill bridge with a native plugin lifecycle."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

BRIDGE_ID = "claude-plugins-kit"
GENERATED_MARKETPLACE = "claude-plugins-kit-generated"
OWNER_FILE = ".claude-plugins-kit-owner.json"
STATE_SCHEMA = 1
PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")
MARKETPLACE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
STATUSES = {"supported", "adapted", "claude-only"}


class BridgeError(ValueError):
    """A safe, user-facing bridge failure."""


@dataclass(frozen=True)
class SkillSource:
    folder: str
    relative_path: str
    description: str


@dataclass(frozen=True)
class PluginSource:
    name: str
    identity: str
    kind: str
    skills: tuple[SkillSource, ...]


@dataclass(frozen=True)
class Paths:
    bridge_root: Path
    data_root: Path
    marketplace_root: Path
    plugins_root: Path
    marketplace_file: Path
    state_file: Path
    lock_file: Path
    overrides_file: Path


class Runner:
    def __init__(self, executable: str = "codex", timeout: float = 30.0) -> None:
        self.executable = executable
        self.timeout = timeout

    def run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.executable, *arguments],
            text=True,
            capture_output=True,
            shell=False,
            timeout=self.timeout,
        )


class FileLock:
    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self.handle: Any = None

    def __enter__(self) -> "FileLock":
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
                    raise BridgeError("another Claude Plugins Kit sync is still running")
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


def platform_data_root(env: Mapping[str, str]) -> Path:
    if env.get("CODEX_KIT_DATA_ROOT"):
        return Path(env["CODEX_KIT_DATA_ROOT"]).expanduser().absolute()
    if os.name == "nt":
        base = env.get("LOCALAPPDATA")
        if not base:
            raise BridgeError("LOCALAPPDATA is unset; set CODEX_KIT_DATA_ROOT")
        return Path(base).expanduser().absolute() / "codex-kit"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "codex-kit"
    return Path(env.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "codex-kit"


def paths_for(env: Mapping[str, str], script: Path | None = None) -> Paths:
    bridge_root = (script or Path(__file__)).resolve().parents[1]
    data_root = platform_data_root(env)
    validate_data_root(data_root)
    marketplace_root = data_root / GENERATED_MARKETPLACE
    paths = Paths(
        bridge_root=bridge_root,
        data_root=data_root,
        marketplace_root=marketplace_root,
        plugins_root=marketplace_root / "plugins",
        marketplace_file=marketplace_root / ".agents" / "plugins" / "marketplace.json",
        state_file=data_root / "state.json",
        lock_file=data_root / ".sync.lock",
        overrides_file=bridge_root / "compatibility-overrides.json",
    )
    validate_managed_paths(paths)
    return paths


def validate_data_root(path: Path) -> None:
    if not path.is_absolute():
        raise BridgeError("generated data root must be absolute")
    probe = Path(path.anchor)
    for part in path.parts[1:]:
        probe /= part
        if probe.is_symlink():
            raise BridgeError(f"generated data root traverses a symlink: {probe}")
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            raise BridgeError(f"generated data root cannot be inside a Git worktree: {path}")


def validate_managed_paths(paths: Paths) -> None:
    for path in (
        paths.data_root,
        paths.marketplace_root,
        paths.plugins_root,
        paths.marketplace_file.parent,
        paths.marketplace_file,
        paths.state_file,
        paths.lock_file,
    ):
        if path.is_symlink():
            raise BridgeError(f"managed path is a symlink: {path}")


def claude_paths(env: Mapping[str, str]) -> tuple[Path, Path]:
    explicit_registry = env.get("CLAUDE_PLUGINS_REGISTRY")
    explicit_skills = env.get("CLAUDE_SKILLS_ROOT")
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured:
        claude_root = Path(configured).expanduser().absolute()
    else:
        devroot = env.get("DEVROOT")
        shared = Path(devroot).expanduser().absolute() / "claude-settings" if devroot else None
        claude_root = shared if shared and shared.is_dir() else Path.home() / ".claude"
    registry = (
        Path(explicit_registry).expanduser().absolute()
        if explicit_registry
        else claude_root / "plugins" / "installed_plugins.json"
    )
    skills = (
        Path(explicit_skills).expanduser().absolute()
        if explicit_skills
        else claude_root / "skills"
    )
    return registry, skills


def _frontmatter(text: str, path: Path) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise BridgeError(f"missing YAML frontmatter: {path}")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise BridgeError(f"unterminated YAML frontmatter: {path}") from exc
    return lines[1:end]


def _scalar(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BridgeError("invalid double-quoted YAML scalar") from exc
        if not isinstance(decoded, str):
            raise BridgeError("frontmatter scalar must be text")
        return decoded
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise BridgeError("invalid single-quoted YAML scalar")
        return value[1:-1].replace("''", "'")
    if value[:1] in "[{":
        raise BridgeError("frontmatter scalar must be text")
    comment = re.search(r"\s+#", value)
    return value[: comment.start()].rstrip() if comment else value


def frontmatter_fields(text: str, path: Path) -> dict[str, str]:
    lines = _frontmatter(text, path)
    fields: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^([A-Za-z0-9_-]+):(?:[ \t]*(.*))$", line)
        if not match:
            index += 1
            continue
        key, raw = match.groups()
        if key not in {"name", "description"}:
            index += 1
            continue
        if raw.startswith(("|", ">")):
            style = raw[0]
            block: list[str] = []
            index += 1
            while index < len(lines):
                child = lines[index]
                if child and not child[0].isspace():
                    break
                block.append(child.lstrip() if child else "")
                index += 1
            fields[key] = "\n".join(block).strip() if style == "|" else " ".join(part.strip() for part in block).strip()
            continue
        continuation: list[str] = []
        cursor = index + 1
        while cursor < len(lines) and (not lines[cursor] or lines[cursor][0].isspace()):
            if lines[cursor].strip():
                continuation.append(lines[cursor].strip())
            cursor += 1
        combined = raw if not continuation else " ".join([raw, *continuation])
        try:
            fields[key] = _scalar(combined)
        except BridgeError as exc:
            raise BridgeError(f"invalid frontmatter in {path}: {exc}") from exc
        index = cursor
    return fields


def load_overrides(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"invalid compatibility overrides: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BridgeError("compatibility overrides must be an object")
    result: dict[tuple[str, str], dict[str, str]] = {}
    for identity, record in raw.items():
        if not isinstance(identity, str) or identity.count("/") != 1 or not isinstance(record, dict):
            raise BridgeError(f"invalid compatibility override: {identity!r}")
        plugin, skill = identity.split("/", 1)
        status, instructions = record.get("status"), record.get("instructions", "")
        if status not in STATUSES or not isinstance(instructions, str) or not plugin or not skill:
            raise BridgeError(f"invalid compatibility override: {identity}")
        result[(plugin, skill)] = {"status": status, "instructions": instructions}
    return result


def _safe_source_file(root: Path, path: Path) -> None:
    probe = root
    for part in path.relative_to(root).parts:
        probe /= part
        if probe.is_symlink():
            raise BridgeError(f"skill source traverses a symlink: {path}")
    resolved_root, resolved = root.resolve(), path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise BridgeError(f"skill source escapes plugin root: {path}") from exc
    if not path.is_file():
        raise BridgeError(f"skill source is missing: {path}")


def skill_sources(root: Path, kind: str) -> tuple[SkillSource, ...]:
    if not root.is_dir():
        raise BridgeError(f"Claude {kind} source is missing: {root}")
    if kind == "plugin":
        paths = set(root.glob("skills/*/SKILL.md"))
        manifest = root / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            try:
                document = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BridgeError(f"invalid Claude plugin manifest: {manifest}: {exc}") from exc
            if not isinstance(document, dict):
                raise BridgeError(f"Claude plugin manifest must be an object: {manifest}")
            declarations = document.get("skills", [])
            if isinstance(declarations, str):
                declarations = [declarations]
            if not isinstance(declarations, list):
                raise BridgeError(f"invalid skills declaration: {manifest}")
            for declaration in declarations:
                if not isinstance(declaration, str):
                    raise BridgeError(f"invalid skills declaration: {manifest}")
                declared = root / declaration
                resolved = declared.resolve()
                try:
                    resolved.relative_to(root.resolve())
                except ValueError as exc:
                    raise BridgeError(f"skills declaration escapes plugin: {declaration}") from exc
                if declared.is_file():
                    if declared.name != "SKILL.md":
                        raise BridgeError(f"declared skill file is not SKILL.md: {declared}")
                    paths.add(declared)
                elif declared.is_dir():
                    paths.update(declared.rglob("SKILL.md"))
                else:
                    raise BridgeError(f"declared skill source is missing: {declared}")
    else:
        paths = set(root.glob("*/SKILL.md"))
    found: dict[str, SkillSource] = {}
    for path in sorted(paths):
        _safe_source_file(root, path)
        folder = path.parent.name
        if not SKILL_NAME_RE.fullmatch(folder):
            raise BridgeError(f"invalid Claude skill folder name: {folder!r}")
        if folder in found:
            raise BridgeError(f"duplicate Claude skill folder name: {folder}")
        fields = frontmatter_fields(path.read_text(encoding="utf-8"), path)
        if not fields.get("name", "").strip():
            raise BridgeError(f"Claude skill has no name: {path}")
        description = fields.get("description", "").strip() or f"Forward to the canonical {folder} Claude skill."
        found[folder] = SkillSource(folder, path.relative_to(root).as_posix(), description)
    return tuple(found[name] for name in sorted(found))


def installed_sources(registry: Path, personal_skills: Path) -> tuple[list[PluginSource], list[str]]:
    try:
        document = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"cannot read Claude plugin registry {registry}: {exc}") from exc
    plugins = document.get("plugins") if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get("version") != 2 or not isinstance(plugins, dict):
        raise BridgeError(f"invalid Claude plugin registry: {registry}")
    sources: list[PluginSource] = []
    by_name: dict[str, list[str]] = {}
    for identity, records in sorted(plugins.items()):
        if not isinstance(identity, str) or identity.count("@") != 1 or not isinstance(records, list) or not records:
            raise BridgeError(f"invalid installed Claude plugin record: {identity!r}")
        name, marketplace = identity.split("@", 1)
        if not PLUGIN_NAME_RE.fullmatch(name) or not MARKETPLACE_NAME_RE.fullmatch(marketplace):
            raise BridgeError(f"invalid installed Claude plugin identity: {identity!r}")
        for record in records:
            if not isinstance(record, dict):
                raise BridgeError(f"invalid installed Claude plugin record: {identity!r}")
            if record.get("scope") == "user" and (
                not isinstance(record.get("installPath"), str) or not record["installPath"].strip()
            ):
                raise BridgeError(f"invalid user-scope installPath for Claude plugin: {identity}")
        users = [
            record
            for record in records
            if isinstance(record, dict)
            and record.get("scope") == "user"
            and isinstance(record.get("installPath"), str)
        ]
        if not users:
            continue
        selected = max(users, key=lambda record: str(record.get("installedAt", "")))
        root = Path(selected["installPath"]).expanduser().absolute()
        sources.append(PluginSource(name, identity, "plugin", skill_sources(root, "plugin")))
        by_name.setdefault(name, []).append(identity)
    duplicates = sorted(name for name, identities in by_name.items() if len(identities) > 1)
    sources = [source for source in sources if source.name not in duplicates]
    if any(source.name == "claude-user" for source in sources):
        duplicates.append("claude-user")
        sources = [source for source in sources if source.name != "claude-user"]
    sources = [source for source in sources if source.skills]
    personal = skill_sources(personal_skills, "personal")
    if personal:
        sources.append(PluginSource("claude-user", "claude-user", "personal", personal))
    return sources, sorted(set(duplicates))


def quote_yaml(value: str) -> str:
    clean = value.replace("<", "[").replace(">", "]").strip()
    if len(clean) > 1024:
        clean = clean[:1021].rstrip() + "..."
    return json.dumps(clean, ensure_ascii=False)


def render_skill(
    source: PluginSource,
    skill: SkillSource,
    override: dict[str, str] | None,
) -> str:
    status = (override or {}).get("status", "supported")
    extra = (override or {}).get("instructions", "").strip()
    metadata = [
        "metadata:",
        "  claude-plugins-kit:",
        '    schema: "1"',
        f"    source-kind: {source.kind}",
        f"    source-identity: {quote_yaml(source.identity)}",
        f"    source-skill: {quote_yaml(skill.folder)}",
        f"    source-relative-path: {quote_yaml(skill.relative_path)}",
        f"    status: {status}",
    ]
    header = "\n".join(
        ["---", f"name: {skill.folder}", f"description: {quote_yaml(skill.description)}", *metadata, "---"]
    )
    if status == "claude-only":
        body = (
            f"This skill is available only in Claude Code. Ask the user to use `{source.identity}/{skill.folder}` "
            "there. Do not load or interpret its canonical source."
        )
    else:
        command = f"info {source.identity}" if source.kind == "plugin" else f"personal-info {skill.folder}"
        body = (
            "This is a generated forwarding skill; its canonical instructions remain in Claude's installation.\n\n"
            "First load the installed Codex gateway skill `claude-plugins-kit:claude-plugins` and follow its runtime "
            f"contract. Use its bridge resolver command `{command}`, then read and follow `{skill.relative_path}` "
            "from the returned source root. Do not guess a path when resolution fails."
        )
    if extra:
        body += f"\n\nCodex compatibility instructions: {extra}"
    return f"{header}\n\n{body}\n"


def override_for(
    overrides: Mapping[tuple[str, str], dict[str, str]], source: PluginSource, skill: str
) -> dict[str, str] | None:
    direct = overrides.get((source.identity, skill))
    if direct:
        return direct
    return overrides.get((source.identity.split("@", 1)[0], skill))


def rendered_plugin(
    source: PluginSource, overrides: Mapping[tuple[str, str], dict[str, str]]
) -> tuple[dict[str, str], str]:
    skills = {
        f"skills/{skill.folder}/SKILL.md": render_skill(
            source, skill, override_for(overrides, source, skill.folder)
        )
        for skill in source.skills
    }
    digest = hashlib.sha256(
        (source.identity + "\0" + "".join(f"{path}\0{content}" for path, content in sorted(skills.items()))).encode()
    ).hexdigest()
    version = f"0.1.0+codex.{digest[:12]}"
    description = (
        "Forward personal Claude Code skills into Codex."
        if source.kind == "personal"
        else f"Forward skills from the installed Claude Code plugin {source.identity}."
    )
    portable = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": source.name,
        "version": version,
        "description": description,
    }
    compatibility: dict[str, Any] = {
        "name": source.name,
        "version": version,
        "description": description,
        "author": {"name": "claude-plugins-kit"},
        "interface": {
            "displayName": source.name,
            "shortDescription": description[:120],
            "longDescription": description,
            "developerName": "claude-plugins-kit",
            "category": "Developer Tools",
            "capabilities": ["Read"],
        },
    }
    if skills:
        compatibility["skills"] = "./skills/"
    generated_files = sorted(["plugin.json", ".codex-plugin/plugin.json", *skills])
    marker = {
        "owner": BRIDGE_ID,
        "schema": STATE_SCHEMA,
        "plugin": source.name,
        "source_identity": source.identity,
        "digest": digest,
        "files": generated_files,
    }
    files = {
        "plugin.json": json.dumps(portable, indent=2, sort_keys=True) + "\n",
        ".codex-plugin/plugin.json": json.dumps(compatibility, indent=2, sort_keys=True) + "\n",
        OWNER_FILE: json.dumps(marker, indent=2, sort_keys=True) + "\n",
        **skills,
    }
    return files, digest


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"invalid {label}: {path}: {exc}") from exc


def load_state(paths: Paths) -> dict[str, Any]:
    if not paths.state_file.exists():
        if paths.marketplace_root.exists():
            raise BridgeError("generated marketplace exists without its ownership state; refusing to modify it")
        return {"schema": STATE_SCHEMA, "plugins": {}}
    state = read_json(paths.state_file, "bridge state")
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA or not isinstance(state.get("plugins"), dict):
        raise BridgeError(f"invalid bridge state: {paths.state_file}")
    for name, record in state["plugins"].items():
        if not isinstance(name, str) or not PLUGIN_NAME_RE.fullmatch(name) or not isinstance(record, dict):
            raise BridgeError(f"invalid plugin record in bridge state: {name!r}")
        if not isinstance(record.get("source_identity"), str):
            raise BridgeError(f"invalid source identity in bridge state: {name}")
        for field in ("source_digest", "installed_digest", "digest"):
            if record.get(field) is not None and not isinstance(record[field], str):
                raise BridgeError(f"invalid {field} in bridge state: {name}")
    return state


def owner_record(path: Path, expected_name: str) -> dict[str, Any] | None:
    marker = path / OWNER_FILE
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        isinstance(record, dict)
        and record.get("owner") == BRIDGE_ID
        and record.get("schema") == STATE_SCHEMA
        and record.get("plugin") == expected_name
        and isinstance(record.get("source_identity"), str)
        and isinstance(record.get("digest"), str)
        and isinstance(record.get("files"), list)
        and all(isinstance(item, str) for item in record["files"])
    ):
        return record
    return None


def owned_by_state(paths: Paths, state: Mapping[str, Any], name: str) -> bool:
    record = state.get("plugins", {}).get(name)
    marker = owner_record(paths.plugins_root / name, name)
    return (
        isinstance(record, dict)
        and marker is not None
        and record.get("source_identity") == marker.get("source_identity")
        and (record.get("source_digest") or record.get("digest")) == marker.get("digest")
    )


def ensure_marketplace_root(paths: Paths) -> None:
    if paths.marketplace_root.exists():
        marker = read_json(paths.marketplace_root / OWNER_FILE, "generated marketplace ownership marker")
        if marker != {"owner": BRIDGE_ID, "schema": STATE_SCHEMA, "marketplace": GENERATED_MARKETPLACE}:
            raise BridgeError(f"refusing to modify unowned marketplace: {paths.marketplace_root}")
        return
    paths.marketplace_root.mkdir(parents=True)
    atomic_write(
        paths.marketplace_root / OWNER_FILE,
        json.dumps({"owner": BRIDGE_ID, "schema": STATE_SCHEMA, "marketplace": GENERATED_MARKETPLACE}, indent=2, sort_keys=True) + "\n",
    )


def verify_existing_marketplace_root(paths: Paths) -> None:
    if not paths.marketplace_root.exists():
        return
    marker = read_json(paths.marketplace_root / OWNER_FILE, "generated marketplace ownership marker")
    expected = {"owner": BRIDGE_ID, "schema": STATE_SCHEMA, "marketplace": GENERATED_MARKETPLACE}
    if marker != expected:
        raise BridgeError(f"refusing to modify unowned marketplace: {paths.marketplace_root}")


def tree_matches(root: Path, files: Mapping[str, str]) -> bool:
    if root.is_symlink():
        return False
    marker = owner_record(root, root.name)
    if marker is None:
        return False
    current = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    return current == dict(files)


def check_owned_tree(root: Path) -> None:
    if root.is_symlink():
        raise BridgeError(f"generated plugin source is a symlink: {root}")
    if root.exists():
        marker = owner_record(root, root.name)
        if marker is None:
            raise BridgeError(f"refusing to replace unowned generated plugin source: {root}")
        assert isinstance(marker["files"], list)
        current = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        extras = sorted(current - set(marker["files"]) - {OWNER_FILE})
        symlinks = sorted(
            path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_symlink()
        )
        if extras or symlinks:
            detail = extras or symlinks
            raise BridgeError(f"generated plugin {root.name} contains unowned or symlinked files: {detail}")


def replace_owned_tree(root: Path, files: Mapping[str, str]) -> None:
    check_owned_tree(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}.", dir=root.parent))
    backup: Path | None = None
    try:
        for relative, content in files.items():
            atomic_write(temporary / relative, content)
        if root.exists():
            backup = root.parent / f".{root.name}.backup-{os.getpid()}"
            if backup.exists():
                raise BridgeError(f"stale generated plugin backup blocks update: {backup}")
            root.rename(backup)
        temporary.rename(root)
        if backup:
            shutil.rmtree(backup)
    except Exception:
        if backup and backup.exists() and not root.exists():
            backup.rename(root)
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def json_command(runner: Runner, arguments: Sequence[str], label: str) -> Any:
    try:
        result = runner.run(arguments)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"{label} timed out") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise BridgeError(f"{label} failed: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"{label} returned invalid JSON") from exc


def installed_plugins(runner: Runner) -> list[dict[str, Any]]:
    document = json_command(runner, ["plugin", "list", "--json"], "Codex plugin list")
    installed = document.get("installed") if isinstance(document, dict) else None
    if not isinstance(installed, list):
        raise BridgeError("Codex plugin list has no installed array")
    for record in installed:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("name"), str)
            or not isinstance(record.get("marketplaceName"), str)
            or not isinstance(record.get("pluginId"), str)
            or not isinstance(record.get("version"), str)
            or record["pluginId"] != f"{record['name']}@{record['marketplaceName']}"
        ):
            raise BridgeError("Codex plugin list contains an invalid record")
    return installed


def configured_marketplaces(runner: Runner) -> list[dict[str, Any]]:
    document = json_command(runner, ["plugin", "marketplace", "list", "--json"], "Codex marketplace list")
    records = document.get("marketplaces") if isinstance(document, dict) else None
    if not isinstance(records, list):
        raise BridgeError("Codex marketplace list has no marketplaces array")
    if any(not isinstance(record, dict) or not isinstance(record.get("name"), str) for record in records):
        raise BridgeError("Codex marketplace list contains an invalid record")
    return records


def marketplace_document(names: Sequence[str]) -> str:
    entries = [
        {
            "name": name,
            "source": {"source": "local", "path": f"./plugins/{name}"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Developer Tools",
        }
        for name in sorted(names)
    ]
    return json.dumps(
        {"name": GENERATED_MARKETPLACE, "interface": {"displayName": "Claude Plugins Kit (Generated)"}, "plugins": entries},
        indent=2,
    ) + "\n"


def run_plain(runner: Runner, arguments: Sequence[str], label: str) -> None:
    try:
        result = runner.run(arguments)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"{label} timed out") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise BridgeError(f"{label} failed: {detail}")


def sync(
    *, env: Mapping[str, str], runner: Runner, script: Path | None = None, install: bool = False
) -> dict[str, Any]:
    paths = paths_for(env, script)
    with FileLock(paths.lock_file):
        try:
            registry, personal = claude_paths(env)
            overrides = load_overrides(paths.overrides_file)
            sources, duplicates = installed_sources(registry, personal)
            state = load_state(paths)
            installed = installed_plugins(runner)
            marketplaces = configured_marketplaces(runner)
            same_marketplaces = [item for item in marketplaces if item["name"] == GENERATED_MARKETPLACE]
            if len(same_marketplaces) > 1:
                raise BridgeError(f"multiple Codex marketplaces are named {GENERATED_MARKETPLACE}")
            if same_marketplaces and not paths.marketplace_root.exists():
                raise BridgeError(f"Codex already has an unowned marketplace named {GENERATED_MARKETPLACE}")
            verify_existing_marketplace_root(paths)
            if same_marketplaces:
                configured_root = same_marketplaces[0].get("root")
                if not isinstance(configured_root, str):
                    raise BridgeError(f"Codex marketplace {GENERATED_MARKETPLACE} has no root")
                if Path(configured_root).expanduser().resolve() != paths.marketplace_root.resolve():
                    raise BridgeError(
                        f"Codex marketplace {GENERATED_MARKETPLACE} points at another root: {configured_root}"
                    )

            installed_by_name: dict[str, list[dict[str, Any]]] = {}
            for record in installed:
                installed_by_name.setdefault(record["name"], []).append(record)
            owned_installed: set[str] = set()
            owned_versions: dict[str, set[str]] = {}
            for name, records in installed_by_name.items():
                if owned_by_state(paths, state, name) and any(
                    record["marketplaceName"] == GENERATED_MARKETPLACE
                    and record["pluginId"] == f"{name}@{GENERATED_MARKETPLACE}"
                    for record in records
                ):
                    owned_installed.add(name)
                    owned_versions[name] = {
                        record["version"]
                        for record in records
                        if record["marketplaceName"] == GENERATED_MARKETPLACE
                        and record["pluginId"] == f"{name}@{GENERATED_MARKETPLACE}"
                    }
            native_collisions = sorted(
                source.name
                for source in sources
                if any(
                    not (
                        record["marketplaceName"] == GENERATED_MARKETPLACE
                        and source.name in owned_installed
                    )
                    for record in installed_by_name.get(source.name, [])
                )
            )
            skipped = sorted(set(duplicates) | set(native_collisions))
            desired_sources = [source for source in sources if source.name not in skipped]
            rendered: dict[str, tuple[dict[str, str], str, PluginSource]] = {}
            for source in desired_sources:
                files, digest = rendered_plugin(source, overrides)
                rendered[source.name] = (files, digest, source)

            existing_owned = {
                name for name in state["plugins"] if owned_by_state(paths, state, name)
            }
            desired_names = set(rendered)
            removals = sorted(existing_owned - desired_names)
            source_changes = sorted(
                name
                for name, (files, _, _) in rendered.items()
                if not tree_matches(paths.plugins_root / name, files)
            )
            install_changes = sorted(
                name
                for name, (files, digest, _) in rendered.items()
                if name not in owned_installed
                or json.loads(files["plugin.json"])["version"] not in owned_versions.get(name, set())
                or (
                    state["plugins"].get(name, {}).get("installed_digest")
                    or state["plugins"].get(name, {}).get("digest")
                )
                != digest
            )
            changed = bool(source_changes or install_changes or removals)
            if not install:
                message = (
                    "Claude wrapper plugin catalog would change. Run sync --install."
                    if changed
                    else "Claude wrapper plugins are current."
                )
                return {
                    "status": "changed" if changed else "unchanged",
                    "changed": changed,
                    "catalog_changed": changed,
                    "message": message,
                    "creates_or_updates": len(source_changes),
                    "removes": len(removals),
                    "skipped": skipped,
                }

            for name in source_changes:
                root = paths.plugins_root / name
                check_owned_tree(root)
            for name in removals:
                check_owned_tree(paths.plugins_root / name)

            ensure_marketplace_root(paths)
            next_state = {"schema": STATE_SCHEMA, "plugins": dict(state["plugins"])}
            for name in source_changes:
                replace_owned_tree(paths.plugins_root / name, rendered[name][0])
                _, digest, source = rendered[name]
                previous = next_state["plugins"].get(name, {})
                next_state["plugins"][name] = {
                    "source_identity": source.identity,
                    "source_digest": digest,
                    "installed_digest": previous.get("installed_digest") or previous.get("digest"),
                }
                atomic_write(paths.state_file, json.dumps(next_state, indent=2, sort_keys=True) + "\n")
            for name in sorted(desired_names - set(source_changes)):
                _, digest, source = rendered[name]
                previous = next_state["plugins"].get(name, {})
                next_state["plugins"][name] = {
                    "source_identity": source.identity,
                    "source_digest": digest,
                    "installed_digest": previous.get("installed_digest") or previous.get("digest"),
                }
            atomic_write(paths.marketplace_file, marketplace_document(sorted(desired_names)))
            if not same_marketplaces:
                run_plain(
                    runner,
                    ["plugin", "marketplace", "add", str(paths.marketplace_root), "--json"],
                    "Codex marketplace add",
                )

            failures: list[str] = []
            for name in install_changes:
                try:
                    run_plain(
                        runner,
                        ["plugin", "add", f"{name}@{GENERATED_MARKETPLACE}", "--json"],
                        f"install generated plugin {name}",
                    )
                except BridgeError as exc:
                    failures.append(str(exc))
                    continue
                _, digest, source = rendered[name]
                next_state["plugins"][name] = {
                    "source_identity": source.identity,
                    "source_digest": digest,
                    "installed_digest": digest,
                }
            for name in sorted(desired_names - set(install_changes)):
                _, digest, source = rendered[name]
                next_state["plugins"][name] = {
                    "source_identity": source.identity,
                    "source_digest": digest,
                    "installed_digest": digest,
                }
            for name in removals:
                if name in owned_installed:
                    try:
                        run_plain(
                            runner,
                            ["plugin", "remove", f"{name}@{GENERATED_MARKETPLACE}", "--json"],
                            f"remove generated plugin {name}",
                        )
                    except BridgeError as exc:
                        failures.append(str(exc))
                        continue
                root = paths.plugins_root / name
                if root.exists() and owner_record(root, name) is not None:
                    shutil.rmtree(root)
                next_state["plugins"].pop(name, None)
            atomic_write(paths.state_file, json.dumps(next_state, indent=2, sort_keys=True) + "\n")
            if failures:
                raise BridgeError("; ".join(failures))
            message = (
                "Claude wrapper plugin catalog changed; restart Codex to load it."
                if changed
                else "Claude wrapper plugins are current."
            )
            if skipped:
                message += " Skipped collisions: " + ", ".join(skipped) + "."
            return {
                "status": "changed" if changed else "unchanged",
                "changed": changed,
                "catalog_changed": changed,
                "message": message,
                "creates_or_updates": len(source_changes),
                "removes": len(removals),
                "skipped": skipped,
            }
        except (BridgeError, OSError, UnicodeError) as exc:
            return {
                "status": "error",
                "changed": False,
                "catalog_changed": False,
                "message": str(exc),
                "creates_or_updates": 0,
                "removes": 0,
                "skipped": [],
            }


def registry_path(env: Mapping[str, str]) -> Path:
    return claude_paths(env)[0]


def installed_record(plugin: str, env: Mapping[str, str]) -> tuple[str, dict[str, Any]]:
    path = registry_path(env)
    document = read_json(path, "Claude plugin registry")
    plugins = document.get("plugins") if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get("version") != 2 or not isinstance(plugins, dict):
        raise BridgeError(f"invalid Claude plugin registry: {path}")
    matches = [key for key in plugins if key == plugin or ("@" not in plugin and key.split("@", 1)[0] == plugin)]
    if len(matches) != 1:
        raise BridgeError(f"installed Claude plugin is missing or ambiguous: {plugin}")
    identity = matches[0]
    name, marketplace = identity.split("@", 1)
    if not PLUGIN_NAME_RE.fullmatch(name) or not MARKETPLACE_NAME_RE.fullmatch(marketplace):
        raise BridgeError(f"invalid installed Claude plugin identity: {identity!r}")
    records = plugins[identity]
    if not isinstance(records, list) or not records or any(not isinstance(record, dict) for record in records):
        raise BridgeError(f"invalid installed records for {identity}")
    users = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("scope") == "user"
        and isinstance(record.get("installPath"), str)
    ]
    if any(
        isinstance(record, dict)
        and record.get("scope") == "user"
        and (not isinstance(record.get("installPath"), str) or not record["installPath"].strip())
        for record in records
    ):
        raise BridgeError(f"invalid user-scope installPath for Claude plugin: {identity}")
    if not users:
        raise BridgeError(f"no user-scoped installed record for {identity}")
    return identity, max(users, key=lambda record: str(record.get("installedAt", "")))


def env_stem(plugin: str) -> str:
    if not PLUGIN_NAME_RE.fullmatch(plugin):
        raise BridgeError(f"invalid plugin name: {plugin!r}")
    return re.sub(r"[^A-Z0-9_]", "_", plugin.upper())


def claude_data_root(env: Mapping[str, str]) -> Path:
    configured = env.get("CLAUDE_BOOTSTRAP_DATA_ROOT")
    return Path(configured).expanduser().absolute() if configured else Path.home() / ".claude" / "plugins" / "data"


def runtime_info(plugin: str, env: Mapping[str, str]) -> dict[str, Any]:
    identity, record = installed_record(plugin, env)
    name, marketplace = identity.split("@", 1)
    root = Path(record["installPath"]).expanduser().resolve()
    plugin_data = claude_data_root(env) / marketplace / name
    declared = env.get(f"{env_stem(name)}_VENV")
    candidates = ([Path(declared).expanduser()] if declared else []) + [
        plugin_data / ".venv" / "bin" / "python",
        plugin_data / ".venv" / "Scripts" / "python.exe",
    ]
    python = next((candidate.absolute() for candidate in candidates if candidate.is_file()), None)
    return {
        "plugin": identity,
        "source_root": str(root),
        "source_exists": root.is_dir(),
        "data_root": str(plugin_data),
        "python": str(python) if python else None,
        "python_exists": python is not None,
    }


def personal_info(skill: str, env: Mapping[str, str]) -> dict[str, Any]:
    if not SKILL_NAME_RE.fullmatch(skill):
        raise BridgeError(f"invalid personal skill folder: {skill!r}")
    root = claude_paths(env)[1]
    source = root / skill / "SKILL.md"
    _safe_source_file(root, source)
    return {"skill": skill, "source_root": str(root), "source_file": str(source)}


def resolve_script(root: Path, value: str) -> Path:
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise BridgeError(f"script escapes plugin root: {value}") from exc
    if not candidate.is_file():
        raise BridgeError(f"script not found: {candidate}")
    return candidate


def run_python(plugin: str, script_name: str | None, module: str | None, arguments: Sequence[str], env: Mapping[str, str]) -> int:
    info = runtime_info(plugin, env)
    if not info["source_exists"]:
        raise BridgeError(f"plugin source not found: {info['source_root']}")
    if not info["python_exists"]:
        raise BridgeError(f"provisioned Python not found under {info['data_root']}; run Claude bootstrap first")
    root = Path(info["source_root"])
    target = [str(resolve_script(root, script_name))] if script_name else ["-m", str(module)]
    child = dict(env)
    name = str(info["plugin"]).split("@", 1)[0]
    child.update(
        {
            "CLAUDE_PLUGIN_ROOT": str(root),
            f"{env_stem(name)}_ROOT": str(root),
            f"{env_stem(name)}_VENV": str(info["python"]),
        }
    )
    return subprocess.run([str(info["python"]), *target, *arguments], env=child, shell=False).returncode


def uninstall(*, env: Mapping[str, str], runner: Runner, script: Path | None = None) -> dict[str, Any]:
    paths = paths_for(env, script)
    with FileLock(paths.lock_file):
        try:
            state = load_state(paths)
            installed = installed_plugins(runner)
            owned = sorted(name for name in state["plugins"] if owned_by_state(paths, state, name))
            marketplaces = configured_marketplaces(runner)
            configured = [record for record in marketplaces if record["name"] == GENERATED_MARKETPLACE]
            if len(configured) > 1:
                raise BridgeError(f"multiple Codex marketplaces are named {GENERATED_MARKETPLACE}")
            if configured:
                root = configured[0].get("root")
                if not isinstance(root, str) or Path(root).expanduser().resolve() != paths.marketplace_root.resolve():
                    raise BridgeError(f"Codex marketplace {GENERATED_MARKETPLACE} points at another root")
            verify_existing_marketplace_root(paths)
            for name in owned:
                check_owned_tree(paths.plugins_root / name)
            failures: list[str] = []
            for name in owned:
                if any(record["pluginId"] == f"{name}@{GENERATED_MARKETPLACE}" for record in installed):
                    try:
                        run_plain(runner, ["plugin", "remove", f"{name}@{GENERATED_MARKETPLACE}", "--json"], f"remove generated plugin {name}")
                    except BridgeError as exc:
                        failures.append(str(exc))
            if failures:
                raise BridgeError("; ".join(failures))
            if configured:
                run_plain(runner, ["plugin", "marketplace", "remove", GENERATED_MARKETPLACE], "remove generated marketplace")
            for name in owned:
                root = paths.plugins_root / name
                if root.exists():
                    shutil.rmtree(root)
            if paths.marketplace_file.exists():
                document = read_json(paths.marketplace_file, "generated marketplace")
                if not isinstance(document, dict) or document.get("name") != GENERATED_MARKETPLACE:
                    raise BridgeError("refusing to remove invalid generated marketplace file")
                paths.marketplace_file.unlink()
            marker = paths.marketplace_root / OWNER_FILE
            if marker.exists():
                marker.unlink()
            for directory in (
                paths.marketplace_file.parent,
                paths.marketplace_file.parent.parent,
                paths.plugins_root,
                paths.marketplace_root,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if paths.state_file.exists():
                paths.state_file.unlink()
            return {"status": "changed", "message": f"Removed {len(owned)} generated Claude wrapper plugins."}
        except (BridgeError, OSError, UnicodeError) as exc:
            return {"status": "error", "message": str(exc)}


def print_sync_result(result: Mapping[str, Any], startup: bool) -> None:
    if startup:
        message = str(result["message"])
        print(
            json.dumps(
                {
                    "systemMessage": message,
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": message,
                    },
                },
                separators=(",", ":"),
            )
        )
    else:
        print(json.dumps(result, indent=2, sort_keys=True))


def _migration_module() -> Any:
    path = Path(__file__).resolve().with_name("migration.py")
    spec = importlib.util.spec_from_file_location("codex_kit_native_migration", path)
    if spec is None or spec.loader is None:
        raise BridgeError(f"cannot load migration module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def migrate(
    *,
    env: Mapping[str, str],
    runner: Runner,
    script: Path | None = None,
    install: bool = False,
    sync_launcher: Any = None,
    discoverer: Any = None,
    fault: Any = None,
) -> dict[str, Any]:
    """Preview or apply migration from the retired standalone bridge."""
    bridge_root = (script or Path(__file__)).resolve().parents[1]
    return _migration_module().migrate(
        env=env,
        runner=runner,
        bridge_root=bridge_root,
        install=install,
        sync_launcher=sync_launcher,
        discoverer=discoverer,
        fault=fault,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--install", action="store_true")
    sync_parser.add_argument("--startup", action="store_true")
    migrate_parser = subparsers.add_parser("migrate")
    migrate_parser.add_argument("--install", action="store_true")
    info_parser = subparsers.add_parser("info")
    info_parser.add_argument("plugin")
    personal_parser = subparsers.add_parser("personal-info")
    personal_parser.add_argument("skill")
    python_parser = subparsers.add_parser("python")
    python_parser.add_argument("plugin")
    target = python_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--script")
    target.add_argument("-m", "--module")
    subparsers.add_parser("uninstall")
    arguments, passthrough = parser.parse_known_args(argv)
    if passthrough[:1] == ["--"]:
        passthrough = passthrough[1:]
    env = os.environ
    runner = Runner(env.get("CODEX_KIT_CODEX_BIN", "codex"))
    try:
        if arguments.command == "sync":
            if passthrough:
                parser.error("unrecognized sync arguments: " + " ".join(passthrough))
            result = sync(env=env, runner=runner, install=arguments.install)
            print_sync_result(result, arguments.startup)
            return 2 if result["status"] == "error" else 0
        if arguments.command == "migrate":
            if passthrough:
                parser.error("unrecognized migrate arguments: " + " ".join(passthrough))
            result = migrate(env=env, runner=runner, install=arguments.install)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 2 if result["status"] == "error" else 0
        if arguments.command == "info":
            if passthrough:
                parser.error("unrecognized info arguments: " + " ".join(passthrough))
            print(json.dumps(runtime_info(arguments.plugin, env), indent=2, sort_keys=True))
            return 0
        if arguments.command == "personal-info":
            if passthrough:
                parser.error("unrecognized personal-info arguments: " + " ".join(passthrough))
            print(json.dumps(personal_info(arguments.skill, env), indent=2, sort_keys=True))
            return 0
        if arguments.command == "python":
            return run_python(arguments.plugin, arguments.script, arguments.module, passthrough, env)
        if passthrough:
            parser.error("unrecognized uninstall arguments: " + " ".join(passthrough))
        result = uninstall(env=env, runner=runner)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2 if result["status"] == "error" else 0
    except (BridgeError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

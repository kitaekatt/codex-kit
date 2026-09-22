"""Generic scaffold-time compatibility analysis for forwarded skills."""
from __future__ import annotations

import hashlib
import contextlib
import errno
import json
import os
import re
import secrets
import stat
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

ANALYZER_VERSION = "1"
PROMPT_VERSION = "1"
REPORT_SCHEMA = 1
CACHE_SCHEMA = 1
INFERENCE_SCHEMA_VERSION = "1"
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_EFFORT = "xhigh"
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


class CompatibilityError(ValueError):
    """Invalid compatibility configuration or report data."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompatibilityError(f"invalid compatibility JSON {path}: {exc}") from exc


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _cache_path(data_root: Path, kind: str, key: str) -> Path:
    if kind not in {"inference", "resolution"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        raise CompatibilityError("invalid compatibility cache identity")
    cache_root = data_root / "compatibility-cache"
    cache_dir = cache_root / kind
    for directory in (cache_root, cache_dir):
        if _is_reparse_point(directory):
            raise CompatibilityError(f"cache directory cannot be a symlink: {directory}")
        if directory.exists() and not directory.is_dir():
            raise CompatibilityError(f"cache path is not a directory: {directory}")
    path = cache_dir / f"{key}.json"
    if _is_reparse_point(path):
        raise CompatibilityError(f"cache file cannot be a symlink: {path}")
    return path


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


@contextlib.contextmanager
def _cache_directory(data_root: Path, kind: str):
    """Open cache directories without following symlinks where dirfd APIs exist."""
    if kind not in {"inference", "resolution"}:
        raise CompatibilityError("invalid compatibility cache kind")
    supports_anchored = (
        os.name != "nt"
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
    )
    if not supports_anchored:
        # Windows lacks these stdlib dirfd/no-follow primitives. The fallback
        # rejects static links, junctions, and reparse points under the private
        # user data root; concurrent
        # same-user mutation of that directory namespace is outside its threat model.
        directory = data_root / "compatibility-cache" / kind
        _cache_path(data_root, kind, "placeholder")
        directory.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(directory) or not directory.is_dir():
            raise CompatibilityError(f"cache directory cannot be a symlink: {directory}")
        yield directory
        return

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags = directory_flags | os.O_NOFOLLOW
    descriptor = os.open(data_root, directory_flags)
    try:
        for component in ("compatibility-cache", kind):
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise CompatibilityError("cache directory cannot be a symlink or non-directory") from exc
        raise
    finally:
        os.close(descriptor)


def _read_cache_json(data_root: Path, kind: str, key: str) -> Any:
    filename = f"{key}.json"
    with _cache_directory(data_root, kind) as directory:
        if isinstance(directory, Path):
            return _read_json(_cache_path(data_root, kind, key), None)
        try:
            descriptor = os.open(
                filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise CompatibilityError("cache file cannot be a symlink or non-file") from exc
            raise
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CompatibilityError(f"invalid compatibility cache JSON {filename}: {exc}") from exc


def _write_cache_json(data_root: Path, kind: str, key: str, value: Any) -> None:
    filename = f"{key}.json"
    with _cache_directory(data_root, kind) as directory:
        if isinstance(directory, Path):
            _atomic_json(_cache_path(data_root, kind, key), value)
            return
        temporary = f".{filename}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def load_inference_policy(data_root: Path) -> dict[str, str]:
    """Read an optional user-local override, never inheriting the caller model."""
    path = data_root / "inference-config.json"
    value = _read_json(path, {})
    if not isinstance(value, dict):
        raise CompatibilityError("inference configuration must be an object")
    config = value.get("inference", {})
    if not isinstance(config, dict):
        raise CompatibilityError("inference configuration must contain an inference object")
    model = config.get("model", DEFAULT_MODEL)
    effort = config.get("effort", DEFAULT_EFFORT)
    if not isinstance(model, str) or not model.strip() or len(model) > 160:
        raise CompatibilityError("inference model must be a non-empty string of at most 160 characters")
    if not isinstance(effort, str) or effort not in EFFORTS:
        raise CompatibilityError(f"inference effort must be one of: {', '.join(sorted(EFFORTS))}")
    return {"requested_model": model.strip(), "requested_effort": effort}


def load_capability_documents(bridge_root: Path, data_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    profile_path = data_root / "capability-profile.json"
    mappings_path = data_root / "capability-mappings.json"
    profile = _read_json(profile_path if profile_path.exists() else bridge_root / "capability-profile.json", {})
    mappings = _read_json(mappings_path if mappings_path.exists() else bridge_root / "capability-mappings.json", {})
    if (not isinstance(profile, dict) or not isinstance(profile.get("id"), str)
            or not isinstance(profile.get("version"), str) or not isinstance(profile.get("capabilities"), list)):
        raise CompatibilityError("capability profile must define id, version, and capabilities")
    if (not isinstance(mappings, dict) or not isinstance(mappings.get("version"), str)
            or not isinstance(mappings.get("mappings"), list)):
        raise CompatibilityError("capability mappings must define version and mappings")
    return profile, mappings


def extract_requirements(source: str) -> list[dict[str, Any]]:
    """Extract the documented `required_capabilities` frontmatter list."""
    lines = source.replace("\r\n", "\n").splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return []
    result: list[dict[str, Any]] = []
    in_capabilities = False
    for line in lines[1:end]:
        if not line[:1].isspace():
            in_capabilities = line.strip() == "required_capabilities:"
            continue
        if not in_capabilities:
            continue
        match = re.fullmatch(r"\s*-\s*([A-Za-z0-9][A-Za-z0-9._:/-]{0,127})\s*", line)
        if match:
            name = match.group(1)
            result.append({
                "id": name,
                "source_evidence": f"required_capabilities: {name}",
                "method": "frontmatter",
                "applicability": "unconditional",
            })
    return result


def resolve_requirements(
    requirements: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    mapping_document: Mapping[str, Any],
) -> list[dict[str, Any]]:
    capabilities = profile.get("capabilities", [])
    if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
        raise CompatibilityError("capability profile capabilities must be a string list")
    mappings = mapping_document.get("mappings", [])
    if not isinstance(mappings, list):
        raise CompatibilityError("capability mappings must be a list")
    by_requirement: dict[str, Mapping[str, Any]] = {}
    for item in mappings:
        if (not isinstance(item, dict) or not isinstance(item.get("requirement"), str)
                or not isinstance(item.get("capability"), str)):
            raise CompatibilityError("each capability mapping must define requirement and capability strings")
        if item["requirement"] in by_requirement:
            raise CompatibilityError(f"duplicate capability mapping for {item['requirement']}")
        for field in ("preserves", "does_not_preserve"):
            if field in item and (not isinstance(item[field], list)
                                  or any(not isinstance(value, str) for value in item[field])):
                raise CompatibilityError(f"mapping {field} must be a string list")
        by_requirement[item["requirement"]] = item
    result: list[dict[str, Any]] = []
    for original in requirements:
        requirement = dict(original)
        name = requirement.get("id")
        mapping = by_requirement.get(name)
        if name in capabilities:
            requirement["status"] = "available"
        elif mapping and mapping.get("capability") in capabilities:
            requirement["status"] = "partial" if mapping.get("does_not_preserve") else "mapped"
            requirement["mapping_evidence"] = f"{name} -> {mapping['capability']}"
            requirement["adapter"] = mapping.get("adapter")
            requirement["preserves"] = mapping.get("preserves", [])
            requirement["does_not_preserve"] = mapping.get("does_not_preserve", [])
            requirement["mapping_version"] = mapping_document.get("version")
        elif mapping:
            requirement["status"] = "unavailable"
            requirement["mapping_evidence"] = f"mapped capability {mapping.get('capability')} is absent from {profile.get('id')}"
        else:
            requirement["status"] = "unmapped"
        result.append(requirement)
    return result


def inference_cache_key(
    *, source_digest: str, reference_identity: str, model: str, effort: str,
    analyzer_version: str = ANALYZER_VERSION, prompt_version: str = PROMPT_VERSION,
    schema_version: str = INFERENCE_SCHEMA_VERSION,
) -> str:
    return _digest({
        "source_digest": source_digest, "reference_identity": reference_identity,
        "model": model, "effort": effort, "analyzer_version": analyzer_version,
        "prompt_version": prompt_version, "schema_version": schema_version,
    })


def resolution_cache_key(
    requirements_digest: str, profile: Mapping[str, Any], mappings: Mapping[str, Any]
) -> str:
    return _digest({
        "requirements_digest": requirements_digest,
        "profile_id": profile.get("id"), "profile_version": profile.get("version"),
        "profile": profile,
        "mapping_version": mappings.get("version"), "mappings": mappings,
    })


def read_resolution_cache(data_root: Path, key: str) -> list[dict[str, Any]] | None:
    value = _read_cache_json(data_root, "resolution", key)
    if (not isinstance(value, dict) or value.get("schema") != CACHE_SCHEMA or value.get("key") != key
            or value.get("status") != "complete" or not isinstance(value.get("requirements"), list)
            or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                   or not isinstance(item.get("source_evidence"), str)
                   or item.get("applicability") not in {"unconditional", "conditional", "optional"}
                   or item.get("status") not in {"available", "mapped", "partial", "unavailable", "unmapped"}
                   for item in value["requirements"])):
        return None
    return value["requirements"]


def write_resolution_cache(data_root: Path, key: str, requirements: Sequence[Mapping[str, Any]]) -> None:
    _write_cache_json(data_root, "resolution", key, {
        "schema": CACHE_SCHEMA, "key": key, "status": "complete",
        "requirements": [dict(item) for item in requirements],
    })


def read_inference_cache(
    data_root: Path, key: str, *, requested_model: str | None = None,
    requested_effort: str | None = None,
) -> dict[str, Any] | None:
    value = _read_cache_json(data_root, "inference", key)
    if (not isinstance(value, dict) or value.get("schema") != CACHE_SCHEMA or value.get("key") != key
            or value.get("status") != "complete" or not isinstance(value.get("requirements"), list)
            or not isinstance(value.get("actual_model"), str)
            or value.get("actual_model") != (requested_model or value.get("requested_model"))
            or value.get("requested_model") != (requested_model or value.get("requested_model"))
            or value.get("requested_effort") != (requested_effort or value.get("requested_effort"))
            or (value.get("actual_effort") is not None
                and value.get("actual_effort") != value.get("requested_effort"))
            or (value.get("actual_effort") is None and value.get("effort_transmitted") is not True)
            or (value.get("actual_effort") is not None and not isinstance(value.get("actual_effort"), str))
            or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                   or not isinstance(item.get("source_evidence"), str)
                   or item.get("applicability") not in {"unconditional", "conditional", "optional"}
                   for item in value["requirements"])):
        return None
    return value


def write_inference_cache(data_root: Path, key: str, value: Mapping[str, Any]) -> None:
    if (value.get("status") != "complete" or not isinstance(value.get("actual_model"), str)
            or value.get("actual_model") != value.get("requested_model")
            or not isinstance(value.get("requested_effort"), str)
            or not isinstance(value.get("requested_model"), str)
            or (value.get("actual_effort") is not None
                and value.get("actual_effort") != value.get("requested_effort"))
            or (not isinstance(value.get("requirements"), list)
                or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                       or not isinstance(item.get("source_evidence"), str)
                       or item.get("applicability") not in {"unconditional", "conditional", "optional"}
                       for item in value["requirements"]))
            or (value.get("actual_effort") is None and value.get("effort_transmitted") is not True)
            or (value.get("actual_effort") is not None and not isinstance(value.get("actual_effort"), str))):
        return
    _write_cache_json(data_root, "inference", key, {
        **dict(value), "schema": CACHE_SCHEMA, "key": key,
    })


def validate_inference_result(
    value: Mapping[str, Any], *, requested_model: str, requested_effort: str
) -> dict[str, Any]:
    actual_model = value.get("actual_model")
    actual_effort = value.get("actual_effort")
    requirements = value.get("requirements")
    if actual_model != requested_model or (actual_effort is not None and actual_effort != requested_effort):
        raise CompatibilityError("inference execution identity does not match the requested model and effort")
    if actual_effort is None and value.get("effort_transmitted") is not True:
        raise CompatibilityError("requested effort was not verifiably transmitted")
    if not isinstance(requirements, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str)
        or not isinstance(item.get("source_evidence"), str)
        or item.get("applicability") not in {"unconditional", "conditional", "optional"}
        for item in requirements
    ):
        raise CompatibilityError("inference result requirements are incomplete")
    return {"status": "complete", "actual_model": actual_model, "actual_effort": actual_effort,
            "effort_transmitted": value.get("effort_transmitted", True), "requirements": requirements}


def analyze_skill(
    source: str, *, source_identity: str, skill_path: str, data_root: Path,
    policy: Mapping[str, str], profile: Mapping[str, Any], mappings: Mapping[str, Any],
    executor: Any = None, allow_inference: bool = True, cache_enabled: bool = True,
) -> dict[str, Any]:
    """Analyze deterministic declarations, then resolve one narrow prose ambiguity."""
    source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    requirements = extract_requirements(source)
    # Natural-language requirements that cannot be represented in the documented
    # frontmatter form are the only inference path. Explicit declarations stay deterministic.
    ambiguous = re.findall(r"(?im)^\s*requires\s+([^\n.]{8,240})[.]?\s*$", source)
    inference: dict[str, Any] = {
        "status": "not-needed", "requested_model": policy["requested_model"],
        "requested_effort": policy["requested_effort"], "actual_model": None, "actual_effort": None,
    }
    if ambiguous:
        reference_identity = f"{source_identity}:{skill_path}"
        key = inference_cache_key(
            source_digest=source_digest, reference_identity=reference_identity,
            model=policy["requested_model"], effort=policy["requested_effort"],
        )
        cached = read_inference_cache(
            data_root, key, requested_model=policy["requested_model"],
            requested_effort=policy["requested_effort"],
        )
        if cached is None and allow_inference:
            evidence = "\n".join(ambiguous)
            prompt = (
                "Extract only concrete host capabilities explicitly required by this skill. "
                "Return a JSON object with a requirements array. Each item must contain id, "
                "source_evidence copied from the text, and applicability (unconditional, "
                "conditional, or optional). Do not claim host equivalence.\n\n" + evidence
            )
            completed = run_inference(
                prompt, requested_model=policy["requested_model"],
                requested_effort=policy["requested_effort"], executor=executor,
            )
            if completed.get("status") == "complete" and any(
                item["source_evidence"] not in source for item in completed["requirements"]
            ):
                completed = {
                    "status": "deferred", "requested_model": policy["requested_model"],
                    "requested_effort": policy["requested_effort"],
                    "actual_model": completed.get("actual_model"),
                    "actual_effort": completed.get("actual_effort"),
                    "reason": "inference cited evidence absent from the skill source",
                }
            if completed.get("status") == "complete":
                requirements.extend({
                    **item, "method": "inference",
                } for item in completed["requirements"])
                cached = completed
                if cache_enabled:
                    write_inference_cache(data_root, key, completed)
            else:
                inference = completed
        if cached is not None:
            requirements.extend({**item, "method": "inference"} for item in cached["requirements"])
            inference = {key: cached.get(key) for key in (
                "status", "requested_model", "requested_effort", "actual_model", "actual_effort",
                "actual_effort_status", "effort_transmitted",
            )}
        elif not allow_inference:
            inference = {
                "status": "deferred", "requested_model": policy["requested_model"],
                "requested_effort": policy["requested_effort"], "actual_model": None,
                "actual_effort": None, "reason": "semantic inference was not run during preview",
            }
    resolved: list[dict[str, Any]] = []
    for requirement in requirements:
        cache_key = resolution_cache_key(_digest([requirement]), profile, mappings)
        cached_resolution = read_resolution_cache(data_root, cache_key)
        if cached_resolution is None:
            cached_resolution = resolve_requirements([requirement], profile, mappings)
            if cache_enabled:
                write_resolution_cache(data_root, cache_key, cached_resolution)
        resolved.extend(cached_resolution)
    return make_report(
        source_identity=source_identity, skill_path=skill_path, source_digest=source_digest,
        requirements=resolved, inference=inference, profile=profile, mappings=mappings,
    )


def run_inference(
    prompt: str, *, requested_model: str, requested_effort: str,
    executor: Any = None, executable: str | None = None, timeout: float = 90.0,
) -> dict[str, Any]:
    """Run optional semantic inference with explicit routing and verified identity."""
    if executor is not None:
        try:
            raw = executor(prompt=prompt, model=requested_model, effort=requested_effort)
        except Exception as exc:
            return {"status": "deferred", "requested_model": requested_model,
                    "requested_effort": requested_effort, "actual_model": None,
                    "actual_effort": None, "reason": f"inference failed: {exc}"}
    else:
        command = executable or shutil.which("llm-scripting-kit")
        if not command:
            return {"status": "deferred", "requested_model": requested_model,
                    "requested_effort": requested_effort, "actual_model": None,
                    "actual_effort": None, "reason": "inference executor unavailable"}
        args = [command, "complete", "--endpoint", "luna", "--format", "json",
                "--model", requested_model, "--effort", requested_effort, "--prompt", prompt]
        try:
            completed = subprocess.run(args, text=True, capture_output=True, timeout=timeout, shell=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"status": "deferred", "requested_model": requested_model,
                    "requested_effort": requested_effort, "actual_model": None,
                    "actual_effort": None, "reason": f"inference failed: {exc}"}
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()[-1000:]
            return {"status": "deferred", "requested_model": requested_model,
                    "requested_effort": requested_effort, "actual_model": None,
                    "actual_effort": None, "reason": f"inference failed: {detail or completed.returncode}"}
        try:
            envelope = json.loads(completed.stdout)
            raw = envelope.get("response", envelope)
            if isinstance(raw, Mapping) and isinstance(raw.get("text"), str):
                completion = json.loads(raw["text"])
                if isinstance(completion, Mapping):
                    raw = {**completion, "actual_model": raw.get("model"),
                           "actual_effort": raw.get("effort"), "effort_transmitted": True}
        except (json.JSONDecodeError, AttributeError):
            return {"status": "deferred", "requested_model": requested_model,
                    "requested_effort": requested_effort, "actual_model": None,
                    "actual_effort": None, "reason": "inference returned invalid JSON"}
    if not isinstance(raw, Mapping):
        return {"status": "deferred", "requested_model": requested_model,
                "requested_effort": requested_effort, "actual_model": None,
                "actual_effort": None, "reason": "inference response has no verifiable execution metadata"}
    try:
        checked = validate_inference_result(raw, requested_model=requested_model,
                                            requested_effort=requested_effort)
    except CompatibilityError as exc:
        return {"status": "deferred", "requested_model": requested_model,
                "requested_effort": requested_effort, "actual_model": raw.get("actual_model"),
                "actual_effort": raw.get("actual_effort"), "reason": str(exc)}
    return {**checked, "requested_model": requested_model, "requested_effort": requested_effort,
            "actual_effort_status": "verified" if checked["actual_effort"] else "unverifiable"}


def make_report(
    *, source_identity: str, skill_path: str, source_digest: str,
    requirements: Sequence[Mapping[str, Any]], inference: Mapping[str, Any],
    profile: Mapping[str, Any] | None = None, mappings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "source": {"identity": source_identity, "skill_path": skill_path, "digest": source_digest},
        "analyzer_version": ANALYZER_VERSION,
        "requirements": [dict(item) for item in requirements],
        "inference": dict(inference),
        "resolution_context": {
            "profile_id": (profile or {}).get("id"),
            "profile_version": (profile or {}).get("version"),
            "profile_digest": _digest(profile) if profile is not None else None,
            "mapping_version": (mappings or {}).get("version"),
            "mapping_digest": _digest(mappings) if mappings is not None else None,
        },
    }
    report["generation"] = report_generation(report)
    return report


def report_generation(report: Mapping[str, Any]) -> str:
    value = dict(report)
    value.pop("generation", None)
    return _digest(value)


def advisory_findings(
    report: Mapping[str, Any] | None, *, source_digest: str,
    profile: Mapping[str, Any] | None = None, mappings: Mapping[str, Any] | None = None,
    source_identity: str | None = None, skill_path: str | None = None,
) -> list[str]:
    if not isinstance(report, Mapping):
        return ["Compatibility report is missing; normal skill execution continues."]
    if (report.get("schema") != REPORT_SCHEMA or not isinstance(report.get("generation"), str)
            or report.get("generation") != report_generation(report)
            or not isinstance(report.get("requirements"), list)
            or not isinstance(report.get("inference"), Mapping)):
        return ["Compatibility report is invalid; normal skill execution continues."]
    source = report.get("source")
    if not isinstance(source, Mapping) or source.get("digest") != source_digest:
        return ["Compatibility report is stale; normal skill execution continues."]
    if ((source_identity is not None and source.get("identity") != source_identity)
            or (skill_path is not None and source.get("skill_path") != skill_path)):
        return ["Compatibility report is stale for this source skill; normal skill execution continues."]
    context = report.get("resolution_context", {})
    if not isinstance(context, Mapping):
        return ["Compatibility report is invalid; normal skill execution continues."]
    if profile and (context.get("profile_id") != profile.get("id")
                    or context.get("profile_version") != profile.get("version")
                    or context.get("profile_digest") != _digest(profile)):
        return ["Compatibility report is stale for the current capability profile; normal skill execution continues."]
    if mappings and (context.get("mapping_version") != mappings.get("version")
                     or context.get("mapping_digest") != _digest(mappings)):
        return ["Compatibility report is stale for the current capability mappings; normal skill execution continues."]
    findings: list[str] = []
    inference = report.get("inference", {})
    if inference.get("status") == "deferred":
        findings.append("Some compatibility analysis was deferred; normal skill execution continues.")
    if inference.get("status") == "complete" and inference.get("actual_effort_status") == "unverifiable":
        findings.append("Inference effort was explicitly sent but was not returned as execution metadata; normal skill execution continues.")
    for item in report.get("requirements", []):
        if (not isinstance(item, Mapping) or not isinstance(item.get("id"), str)
                or not isinstance(item.get("source_evidence"), str)):
            return ["Compatibility report is invalid; normal skill execution continues."]
        status = item.get("status")
        if status in {"partial", "unavailable", "unmapped"}:
            findings.append(f"Compatibility requirement {item.get('id')} is {status}.")
    return findings

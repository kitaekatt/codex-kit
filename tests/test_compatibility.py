from __future__ import annotations

import importlib.util
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def compatibility():
    path = Path(__file__).parents[1] / "plugins" / "claude-plugins-kit" / "scripts" / "compatibility.py"
    spec = importlib.util.spec_from_file_location("codex_kit_compatibility_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_inference_policy_is_explicit_luna_xhigh(compatibility, tmp_path):
    policy = compatibility.load_inference_policy(tmp_path)
    assert policy == {"requested_model": "gpt-5.6-luna", "requested_effort": "xhigh"}


def test_local_inference_policy_overrides_are_validated(compatibility, tmp_path):
    config = tmp_path / "inference-config.json"
    config.write_text(json.dumps({"inference": {"model": "local-model", "effort": "high"}}))
    assert compatibility.load_inference_policy(tmp_path) == {
        "requested_model": "local-model", "requested_effort": "high"
    }
    config.write_text(json.dumps({"inference": {"model": "", "effort": "impossible"}}))
    with pytest.raises(compatibility.CompatibilityError):
        compatibility.load_inference_policy(tmp_path)


def test_extracts_requirements_with_evidence_and_resolves_explicit_mapping(compatibility):
    source = """---
name: sample
required_capabilities:
  - shell.exec
---
Use a shell to inspect the files.
"""
    requirements = compatibility.extract_requirements(source)
    assert requirements == [{
        "id": "shell.exec",
        "source_evidence": "required_capabilities: shell.exec",
        "method": "frontmatter",
        "applicability": "unconditional",
    }]
    result = compatibility.resolve_requirements(
        requirements,
        {"id": "codex", "version": "1", "capabilities": ["terminal.run"]},
        {"version": "1", "mappings": [{"requirement": "shell.exec", "capability": "terminal.run"}]},
    )
    assert result[0]["status"] == "mapped"
    assert result[0]["mapping_evidence"] == "shell.exec -> terminal.run"


def test_partial_adapter_does_not_hide_unpreserved_properties(compatibility):
    requirements = [{
        "id": "filesystem.write",
        "source_evidence": "requires filesystem.write",
        "method": "rule",
        "applicability": "unconditional",
    }]
    result = compatibility.resolve_requirements(
        requirements,
        {"id": "codex", "version": "1", "capabilities": ["workspace.write"]},
        {"version": "1", "mappings": [{
            "requirement": "filesystem.write",
            "capability": "workspace.write",
            "adapter": "workspace-guard",
            "preserves": ["writes-inside-workspace"],
            "does_not_preserve": ["writes-outside-workspace"],
        }]},
    )
    assert result[0]["status"] == "partial"
    assert result[0]["does_not_preserve"] == ["writes-outside-workspace"]


def test_inference_result_is_accepted_only_when_execution_identity_matches(compatibility):
    result = compatibility.validate_inference_result(
        {"requirements": [], "actual_model": "gpt-5.6-luna", "actual_effort": "xhigh"},
        requested_model="gpt-5.6-luna", requested_effort="xhigh",
    )
    assert result["status"] == "complete"
    with pytest.raises(compatibility.CompatibilityError):
        compatibility.validate_inference_result(
            {"requirements": [], "actual_model": "other", "actual_effort": "xhigh"},
            requested_model="gpt-5.6-luna", requested_effort="xhigh",
        )


def test_cache_key_includes_profile_and_adapter_versions(compatibility):
    base = compatibility.resolution_cache_key(
        "requirements", {"id": "codex", "version": "1"}, {"version": "1", "mappings": []}
    )
    profile_changed = compatibility.resolution_cache_key(
        "requirements", {"id": "codex", "version": "2"}, {"version": "1", "mappings": []}
    )
    mapping_changed = compatibility.resolution_cache_key(
        "requirements", {"id": "codex", "version": "1"}, {"version": "2", "mappings": []}
    )
    assert len({base, profile_changed, mapping_changed}) == 3


def test_inference_cache_key_includes_source_reference_model_effort_and_schema(compatibility):
    arguments = {"source_digest": "source", "reference_identity": "plugin@market:skills/a/SKILL.md",
                 "model": "gpt-5.6-luna", "effort": "xhigh"}
    base = compatibility.inference_cache_key(**arguments)
    changed = [
        compatibility.inference_cache_key(**{**arguments, "source_digest": "other-source"}),
        compatibility.inference_cache_key(**{**arguments, "reference_identity": "plugin@market:skills/b/SKILL.md"}),
        compatibility.inference_cache_key(**{**arguments, "model": "local-model"}),
        compatibility.inference_cache_key(**{**arguments, "effort": "high"}),
        compatibility.inference_cache_key(**arguments, schema_version="2"),
    ]
    assert all(key != base for key in changed)


def test_failed_inference_is_not_a_successful_cache_hit(compatibility, tmp_path):
    key = "same-analysis-key"
    compatibility.write_inference_cache(tmp_path, key, {"status": "deferred", "requirements": []})
    assert compatibility.read_inference_cache(tmp_path, key) is None
    compatibility.write_inference_cache(tmp_path, key, {
        "status": "complete", "requested_model": "gpt-5.6-luna", "requested_effort": "xhigh",
        "actual_model": "gpt-5.6-luna", "actual_effort": None, "effort_transmitted": True,
        "requirements": [{"id": "missing-evidence"}],
    })
    assert compatibility.read_inference_cache(tmp_path, key) is None


def test_unavailable_inference_retries_without_repeating_deterministic_resolution(compatibility, tmp_path, monkeypatch):
    source = "\n".join([
        "---", "name: demo", "required_capabilities:", "  - terminal.run", "---",
        "Requires a remote browser session to inspect the page.",
    ])
    policy = {"requested_model": "gpt-5.6-luna", "requested_effort": "xhigh"}
    profile = {"id": "codex", "version": "1", "capabilities": []}
    mappings = {"version": "1", "mappings": []}
    calls = []
    resolve = compatibility.resolve_requirements

    def counted(*args):
        calls.append(args)
        return resolve(*args)

    monkeypatch.setattr(compatibility, "resolve_requirements", counted)
    degraded = compatibility.analyze_skill(
        source, source_identity="sample@market", skill_path="skills/demo/SKILL.md",
        data_root=tmp_path, policy=policy, profile=profile, mappings=mappings,
        executor=lambda **kwargs: {"status": "deferred", "requested_model": kwargs["model"],
                                   "requested_effort": kwargs["effort"], "actual_model": None,
                                   "actual_effort": None, "reason": "offline"},
    )
    assert degraded["inference"]["status"] == "deferred"
    assert len(calls) == 1

    recovered = compatibility.analyze_skill(
        source, source_identity="sample@market", skill_path="skills/demo/SKILL.md",
        data_root=tmp_path, policy=policy, profile=profile, mappings=mappings,
        executor=lambda **kwargs: {"status": "complete", "actual_model": kwargs["model"],
                                   "actual_effort": kwargs["effort"],
                                   "requirements": [{"id": "browser.inspect",
                                       "source_evidence": "remote browser session",
                                       "applicability": "unconditional"}]},
    )
    assert recovered["inference"]["status"] == "complete"
    assert {item["id"] for item in recovered["requirements"]} == {"terminal.run", "browser.inspect"}
    assert len(calls) == 2
    repeated = compatibility.analyze_skill(
        source, source_identity="sample@market", skill_path="skills/demo/SKILL.md",
        data_root=tmp_path, policy=policy, profile=profile, mappings=mappings,
        executor=lambda **kwargs: pytest.fail("a complete inference cache must be reused"),
    )
    assert repeated["inference"]["status"] == "complete"
    assert len(calls) == 2


def test_inference_routing_is_explicit_and_mismatch_never_falls_back(compatibility):
    calls = []

    def executor(**kwargs):
        calls.append(kwargs)
        return {"actual_model": "different-model", "actual_effort": "high", "requirements": []}

    result = compatibility.run_inference(
        "prompt", requested_model="gpt-5.6-luna", requested_effort="xhigh", executor=executor,
    )
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-5.6-luna"
    assert calls[0]["effort"] == "xhigh"
    assert result["status"] == "deferred"


def test_cli_inference_sends_luna_and_effort_and_records_unverified_actual_effort(compatibility, monkeypatch):
    observed = {}
    response = {
        "protocol": "1", "endpoint": "luna", "kind": "harness", "backend": "codex-cli",
        "response": {
            "text": json.dumps({"requirements": [{"id": "browser.inspect",
                "source_evidence": "remote browser session", "applicability": "unconditional"}]}),
            "model": "gpt-5.6-luna", "status": "completed",
        },
    }

    def fake_run(arguments, **kwargs):
        observed["arguments"] = arguments
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    monkeypatch.setattr(compatibility.shutil, "which", lambda _: str(Path.cwd() / "fake-llm-scripting-kit"))
    monkeypatch.setattr(compatibility.subprocess, "run", fake_run)
    result = compatibility.run_inference(
        "prompt", requested_model="gpt-5.6-luna", requested_effort="xhigh",
    )
    assert result["status"] == "complete"
    assert result["actual_model"] == "gpt-5.6-luna"
    assert result["actual_effort"] is None
    assert result["actual_effort_status"] == "unverifiable"
    assert observed["arguments"][observed["arguments"].index("--endpoint") + 1] == "luna"
    assert observed["arguments"][observed["arguments"].index("--effort") + 1] == "xhigh"


def test_unavailable_inference_does_not_fall_back_or_cache(compatibility, tmp_path):
    result = compatibility.run_inference(
        "prompt", requested_model="gpt-5.6-luna", requested_effort="xhigh",
        executable=str(tmp_path / "missing-executable"),
    )
    assert result["status"] == "deferred"
    compatibility.write_inference_cache(tmp_path, "key", result)
    assert compatibility.read_inference_cache(tmp_path, "key") is None


def test_resolution_cache_invalidates_for_profile_and_mapping_versions(compatibility, tmp_path):
    for capabilities in (["terminal.run"], []):
        profile = {"id": "host", "version": "1", "capabilities": capabilities}
        report = compatibility.analyze_skill(
            "\n".join(["---", "name: demo", "required_capabilities:", "  - terminal.run", "---", ""]),
            source_identity="sample", skill_path="skills/demo/SKILL.md",
            data_root=tmp_path, policy={"requested_model": "gpt-5.6-luna", "requested_effort": "xhigh"},
            profile=profile, mappings={"version": "1", "mappings": []},
        )
        assert report["requirements"][0]["status"] == ("available" if capabilities else "unmapped")
    assert len(list((tmp_path / "compatibility-cache" / "resolution").glob("*.json"))) == 2


@pytest.mark.parametrize("cache_kind", ["inference", "resolution"])
def test_cache_write_rejects_symlinked_cache_directory(compatibility, tmp_path, cache_kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    cache_root = tmp_path / "compatibility-cache"
    cache_root.mkdir()
    (cache_root / cache_kind).symlink_to(outside, target_is_directory=True)

    with pytest.raises(compatibility.CompatibilityError, match="cache directory cannot be a symlink"):
        if cache_kind == "resolution":
            compatibility.write_resolution_cache(tmp_path, "key", [{
                "id": "terminal.run", "source_evidence": "explicit evidence",
                "applicability": "unconditional", "status": "available",
            }])
        else:
            compatibility.write_inference_cache(tmp_path, "key", {
                "status": "complete", "actual_model": "gpt-5.6-luna",
                "requested_model": "gpt-5.6-luna", "requested_effort": "xhigh",
                "actual_effort": None, "effort_transmitted": True, "requirements": [],
            })
    assert list(outside.iterdir()) == []


def test_cache_path_rejects_junction_or_reparse_point(compatibility, tmp_path, monkeypatch):
    cache_root = tmp_path / "compatibility-cache"
    cache_dir = cache_root / "inference"
    cache_dir.mkdir(parents=True)

    def is_junction(path):
        return path == cache_dir

    monkeypatch.setattr(compatibility.Path, "is_junction", is_junction, raising=False)

    with pytest.raises(compatibility.CompatibilityError, match="cache directory cannot be a symlink"):
        compatibility._cache_path(tmp_path, "inference", "key")


@pytest.mark.skipif(os.name == "nt", reason="dirfd anchored cache access is POSIX-specific")
def test_cache_write_stays_anchored_if_cache_path_is_swapped(compatibility, tmp_path, monkeypatch):
    cache_root = tmp_path / "compatibility-cache"
    cache_dir = cache_root / "inference"
    cache_dir.mkdir(parents=True)
    detached_dir = tmp_path / "detached-cache"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_open_cache = compatibility._cache_directory

    @contextlib.contextmanager
    def swap_after_open(data_root, kind):
        with original_open_cache(data_root, kind) as directory:
            if kind == "inference":
                cache_dir.rename(detached_dir)
                cache_dir.symlink_to(outside, target_is_directory=True)
            yield directory

    monkeypatch.setattr(compatibility, "_cache_directory", swap_after_open)
    compatibility.write_inference_cache(tmp_path, "race-key", {
        "status": "complete", "actual_model": "gpt-5.6-luna",
        "requested_model": "gpt-5.6-luna", "requested_effort": "xhigh",
        "actual_effort": None, "effort_transmitted": True, "requirements": [],
    })

    assert (detached_dir / "race-key.json").is_file()
    assert list(outside.iterdir()) == []


def test_report_generation_identity_is_stable_and_versioned(compatibility):
    profile = {"id": "codex", "version": "1", "capabilities": ["terminal.run"]}
    mappings = {"version": "1", "mappings": []}
    report = compatibility.make_report(
        source_identity="plugin@market", skill_path="skills/sample/SKILL.md",
        source_digest="source", requirements=[], inference={"status": "deferred"},
        profile=profile, mappings=mappings,
    )
    assert report["schema"] == compatibility.REPORT_SCHEMA
    assert report["generation"] == compatibility.report_generation(report)
    assert compatibility.advisory_findings(report, source_digest="source", profile=profile, mappings=mappings) == [
        "Some compatibility analysis was deferred; normal skill execution continues."
    ]
    changed_profile = {**profile, "capabilities": []}
    assert compatibility.advisory_findings(
        report, source_digest="source", profile=changed_profile, mappings=mappings
    )[0].startswith("Compatibility report is stale")


@pytest.mark.skipif(os.environ.get("CODEX_KIT_LUNA_SMOKE") != "1", reason="opt-in live Luna inference")
def test_live_luna_smoke_checks_returned_execution_metadata(compatibility):
    result = compatibility.run_inference(
        "Extract one capability from this evidence: 'Requires a browser session to inspect pages.' "
        "Return only a JSON object containing requirements with id, source_evidence, and applicability. "
        "Applicability must be exactly one of unconditional, conditional, or optional.",
        requested_model="gpt-5.6-luna", requested_effort="xhigh",
    )
    if result["status"] == "deferred" and result.get("reason") in {
        "inference executor unavailable",
    }:
        pytest.skip(f"live Luna executor is unavailable in this process: {result['reason']}")
    if result["status"] == "deferred" and str(result.get("reason", "")).startswith("inference failed:"):
        pytest.skip(f"live Luna endpoint is unavailable in this process: {result['reason']}")
    assert result["status"] == "complete", result
    assert result["requested_model"] == "gpt-5.6-luna"
    assert result["requested_effort"] == "xhigh"
    assert result["actual_model"] == "gpt-5.6-luna"
    assert result["effort_transmitted"] is True

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "plugins/claude-plugins-kit/scripts"


def shell_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("dirname", "uname", "getconf", "cat", "curl", "tar", "sha256sum", "shasum", "mkdir", "mktemp", "mv", "rm", "flock"):
        executable = shutil.which(name)
        if executable:
            (tools / name).symlink_to(executable)
    return {"HOME": str(home), "PATH": str(tools), "CODEX_KIT_DATA_ROOT": str(tmp_path / "data")}


def launch(env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/sh", str(SCRIPTS / "launch.sh"), *arguments], env=env, text=True, capture_output=True, timeout=240)


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_existing_python_never_provisions_runtime(tmp_path: Path) -> None:
    env = shell_env(tmp_path)
    env["CODEX_KIT_PYTHON"] = sys.executable
    result = launch(env, "--help")
    assert result.returncode == 0, result.stderr
    assert not Path(env["CODEX_KIT_DATA_ROOT"]).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_no_python_download_opt_out_is_clear(tmp_path: Path) -> None:
    env = shell_env(tmp_path)
    env["CODEX_KIT_RUNTIME_DOWNLOAD"] = "0"
    result = launch(env, "--help")
    assert result.returncode == 2
    assert "download disabled" in result.stderr
    assert not Path(env["CODEX_KIT_DATA_ROOT"]).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
@pytest.mark.parametrize("unsafe", ["symlink", "git", "relative"])
def test_no_python_rejects_unsafe_data_root_before_writing(tmp_path: Path, unsafe: str) -> None:
    env = shell_env(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    if unsafe == "symlink":
        Path(env["CODEX_KIT_DATA_ROOT"]).symlink_to(destination, target_is_directory=True)
    elif unsafe == "git":
        (tmp_path / ".git").mkdir()
    else:
        env["CODEX_KIT_DATA_ROOT"] = "relative/data"
    result = launch(env, "--help")
    assert result.returncode == 2
    assert list(destination.iterdir()) == []
    assert "data root" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_corrupt_download_is_not_extracted_or_executed(tmp_path: Path) -> None:
    env = shell_env(tmp_path)
    fake_curl = Path(env["PATH"]) / "curl"
    fake_curl.unlink()
    fake_curl.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n if [ "$1" = --output ]; then shift; echo corrupt > "$1"; exit 0; fi\n shift\ndone\nexit 1\n')
    fake_curl.chmod(0o755)
    result = launch(env, "--help")
    assert result.returncode == 2, result.stderr
    assert "SHA256 mismatch" in result.stderr
    base = Path(env["CODEX_KIT_DATA_ROOT"]) / "runtime"
    assert not list(base.glob("cpython*"))
    assert not list(base.glob(".install.*[!.lock]"))
    # The OS lock is released after failure; a retry reaches verification again.
    retried = launch(env, "--help")
    assert "SHA256 mismatch" in retried.stderr


@pytest.mark.skipif(os.name == "nt" or os.environ.get("CODEX_KIT_RUNTIME_TESTS") != "1", reason="opt-in real isolated runtime download")
def test_real_no_python_install_and_offline_reuse(tmp_path: Path) -> None:
    env = shell_env(tmp_path)
    assert not (Path(env["PATH"]) / "python3").exists()
    result = launch(env, "--help")
    assert result.returncode == 0, result.stderr
    assert "downloading private Python" in result.stderr
    assert list((Path(env["CODEX_KIT_DATA_ROOT"]) / "runtime").glob("cpython*/.codex-kit-runtime"))
    env["CODEX_KIT_RUNTIME_DOWNLOAD"] = "0"
    reused = launch(env, "--help")
    assert reused.returncode == 0, reused.stderr
    assert "downloading" not in reused.stderr

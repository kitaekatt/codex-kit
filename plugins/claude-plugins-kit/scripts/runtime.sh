#!/bin/sh
# Called by launch.sh only when no suitable interpreter is available.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

runtime_error() { echo "Claude Plugins Kit runtime: $*" >&2; exit 2; }
runtime_os=$(uname -s)
runtime_arch=$(uname -m)
runtime_target=
while read -r asset_os asset_arch asset_target asset_sha; do
    if [ "$asset_os" = "$runtime_os" ] && [ "$asset_arch" = "$runtime_arch" ]; then
        runtime_target=$asset_target
        runtime_sha=$asset_sha
    fi
done < "$script_dir/runtime-assets.tsv"
[ -n "$runtime_target" ] || runtime_error "unsupported platform $runtime_os/$runtime_arch; set CODEX_KIT_PYTHON to Python >=3.10."
if [ "$runtime_os" = Linux ] && ! getconf GNU_LIBC_VERSION >/dev/null 2>&1; then
    runtime_error "the private Linux runtime requires glibc; set CODEX_KIT_PYTHON on this platform."
fi
case "$runtime_os" in
    Darwin) runtime_data=${CODEX_KIT_DATA_ROOT:-"$HOME/Library/Application Support/codex-kit"} ;;
    Linux) runtime_data=${CODEX_KIT_DATA_ROOT:-"${XDG_DATA_HOME:-$HOME/.local/share}/codex-kit"} ;;
esac
case "$runtime_data" in /*) ;; *) runtime_error "data root must be absolute" ;; esac
# Reject traversed symlinks and Git roots before writing anything.
runtime_probe=$runtime_data
while [ "$runtime_probe" != / ]; do
    [ ! -L "$runtime_probe" ] || runtime_error "data root traverses a symlink: $runtime_probe"
    [ ! -e "$runtime_probe/.git" ] || runtime_error "data root is inside a Git worktree: $runtime_probe"
    runtime_probe=$(dirname -- "$runtime_probe")
done
case "$runtime_data/" in */../*|*/./*) runtime_error "data root must not contain dot segments" ;; esac
runtime_base=$runtime_data/runtime
[ ! -L "$runtime_base" ] || runtime_error "runtime directory is a symlink"
runtime_name=cpython-3.13.15-20260901-$runtime_target
runtime_root=$runtime_base/$runtime_name
bridge_python=$runtime_root/python/bin/python3
if [ -e "$runtime_root" ] || [ -L "$runtime_root" ]; then
    [ ! -L "$runtime_root" ] && [ ! -L "$runtime_root/.codex-kit-runtime" ] && [ -f "$runtime_root/.codex-kit-runtime" ] || runtime_error "unowned runtime directory: $runtime_root"
    [ "$(cat "$runtime_root/.codex-kit-runtime")" = "$runtime_sha" ] || runtime_error "runtime ownership mismatch"
    "$bridge_python" -I -c 'import sys; assert sys.version_info >= (3, 10)' || runtime_error "cached runtime is damaged: $runtime_root"
else
    [ "${CODEX_KIT_RUNTIME_DOWNLOAD:-1}" != 0 ] || runtime_error "download disabled; set CODEX_KIT_PYTHON to Python >=3.10."
    command -v curl >/dev/null 2>&1 || runtime_error "curl is required for the private runtime download"
    command -v tar >/dev/null 2>&1 || runtime_error "tar is required for the private runtime archive"
    if command -v sha256sum >/dev/null 2>&1; then runtime_hash=sha256sum
    elif command -v shasum >/dev/null 2>&1; then runtime_hash=shasum
    else runtime_error "a SHA256 verifier (sha256sum or shasum) is required"; fi
    umask 077
    mkdir -p "$runtime_base"
    runtime_lock=$runtime_base/.install.lock
    [ ! -L "$runtime_lock" ] || runtime_error "runtime lock is a symlink"
    if [ "${1:-}" != --locked ]; then
        if [ "$runtime_os" = Darwin ]; then
            /usr/bin/lockf -k -t 120 "$runtime_lock" /bin/sh "$0" --locked >/dev/null
        else
            command -v flock >/dev/null 2>&1 || runtime_error "flock is required to lock private runtime installation"
            flock -w 120 "$runtime_lock" /bin/sh "$0" --locked >/dev/null
        fi
        echo "$bridge_python"
        exit 0
    fi
    runtime_stage=
    runtime_cleanup() {
        [ -z "$runtime_stage" ] || rm -rf -- "$runtime_stage"
    }
    trap runtime_cleanup EXIT
    trap 'exit 2' HUP INT TERM
    # A concurrent installer may have completed while this process waited.
    if [ ! -e "$runtime_root" ]; then
        runtime_stage=$(mktemp -d "$runtime_base/.install.XXXXXXXX")
        runtime_archive=$runtime_stage/python.tar.gz
        runtime_url="https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15%2B20260901-$runtime_target-install_only_stripped.tar.gz"
        echo "Claude Plugins Kit: downloading private Python 3.13.15 from python-build-standalone." >&2
        curl --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 180 --output "$runtime_archive" "$runtime_url"
        if [ "$runtime_hash" = sha256sum ]; then
            runtime_actual=$(sha256sum "$runtime_archive")
        else
            runtime_actual=$(shasum -a 256 "$runtime_archive")
        fi
        runtime_actual=${runtime_actual%% *}
        [ "$runtime_actual" = "$runtime_sha" ] || runtime_error "download SHA256 mismatch; archive was not executed"
        mkdir "$runtime_stage/payload"
        tar -xzf "$runtime_archive" -C "$runtime_stage/payload"
        "$runtime_stage/payload/python/bin/python3" -I -c 'import sys; assert sys.version_info >= (3, 10)' || runtime_error "downloaded interpreter cannot run on this platform"
        echo "$runtime_sha" > "$runtime_stage/payload/.codex-kit-runtime"
        mv "$runtime_stage/payload" "$runtime_root"
    fi
    runtime_cleanup
    trap - EXIT HUP INT TERM
fi
echo "$bridge_python"

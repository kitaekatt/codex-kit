#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ -n "${CODEX_KIT_PYTHON:-}" ]; then
    bridge_python=$CODEX_KIT_PYTHON
elif [ -x "${HOME}/.local/share/python-standalone/python/bin/python3" ]; then
    bridge_python=${HOME}/.local/share/python-standalone/python/bin/python3
elif command -v python3 >/dev/null 2>&1; then
    bridge_python=python3
elif command -v python >/dev/null 2>&1; then
    bridge_python=python
else
    echo "Claude Plugins Kit needs Python 3.10 or newer. Set CODEX_KIT_PYTHON." >&2
    exit 2
fi

exec "$bridge_python" "$script_dir/bridge.py" "$@"

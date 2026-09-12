#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ -n "${CODEX_KIT_PYTHON:-}" ]; then
    bridge_python=$CODEX_KIT_PYTHON
    "$bridge_python" -I -c 'import sys; assert sys.version_info >= (3, 10)' || {
        echo "CODEX_KIT_PYTHON must be an executable Python >=3.10." >&2
        exit 2
    }
else
    bridge_python=
    for runtime_candidate in "${HOME}/.local/share/python-standalone/python/bin/python3" python3 python; do
        if "$runtime_candidate" -I -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
            bridge_python=$runtime_candidate
            break
        fi
    done
    if [ -z "$bridge_python" ]; then bridge_python=$(/bin/sh "$script_dir/runtime.sh"); fi
fi

exec "$bridge_python" -I "$script_dir/bridge.py" "$@"

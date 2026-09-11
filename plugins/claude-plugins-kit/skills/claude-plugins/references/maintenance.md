# Maintenance and lifecycle

The bridge discovers Claude sources in this order:

1. `CLAUDE_PLUGINS_REGISTRY` and `CLAUDE_SKILLS_ROOT` overrides.
2. `CLAUDE_CONFIG_DIR`.
3. `$DEVROOT/claude-settings` when it exists.
4. The platform Claude home, normally `~/.claude`.

Generated state defaults to the platform application-data directory under `codex-kit`: `$XDG_DATA_HOME/codex-kit` or `~/.local/share/codex-kit` on Linux, `~/Library/Application Support/codex-kit` on macOS, and `%LOCALAPPDATA%\\codex-kit` on Windows. `CODEX_KIT_DATA_ROOT` overrides it. The bridge refuses a data root inside a Git worktree or through a symlink.

The generated marketplace is `claude-plugins-kit-generated`. Synchronization uses native `codex plugin marketplace add`, `codex plugin add`, and `codex plugin remove`. It never edits the downloaded plugin cache for Codex. An installed native Codex plugin with the same name wins. The bridge reports and skips that wrapper. It does not modify the native plugin.

Resolve `<launcher>` with the [runtime contract](runtime.md). Then run `<launcher> sync --install` to refresh. Start a new Codex thread when it reports a catalog change. The command reports one of three states: current, changed and restart required, or error.

Codex 0.154.0 does not load bundled plugin hooks. Use manual gateway synchronization on that version. The package includes a SessionStart hook for forward compatibility. On a runtime that supports bundled hooks, review and trust the hook before use. The hook then runs synchronization on startup and resume.

Only plugins with bridge ownership markers and matching state records can change. Missing or malformed sources and ownership records cause an error. The bridge leaves existing wrappers in place after these errors. To do a full cleanup, run `<launcher> uninstall` before you remove the bridge. Removing this authored plugin does not remove Claude plugins.

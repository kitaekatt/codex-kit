# Codex Kit

Codex Kit contains `claude-plugins-kit`, a portable Codex plugin that exposes user-installed Claude Code skills through native Codex wrapper plugins. Wrappers contain forwarding and compatibility instructions only. The canonical Claude skill files stay in their installed locations.

## Install

Python 3.10 or newer and a Codex build with native plugin commands are required.

```sh
codex plugin marketplace add kitaekatt/codex-kit
codex plugin add claude-plugins-kit@codex-kit
```

Start a new Codex thread so that Codex discovers the gateway skill. Invoke `claude-plugins-kit:claude-plugins` and ask it to refresh the bridge. If the gateway reports a catalog change, start one more thread to load the skills.

Codex 0.154.0 does not load bundled plugin hooks. Manual gateway synchronization works on that version. The package includes a SessionStart hook for forward compatibility with runtimes that support bundled hooks. If `/hooks` shows this hook, review and trust it before use. The hook then synchronizes the wrappers on startup and resume.

The generated marketplace is named `claude-plugins-kit-generated`. It is a marketplace name, not one plugin selector. It contains one wrapper plugin for each installed Claude plugin that exposes skills, plus `claude-user` when personal skills exist. The bridge installs selectors such as `awesome-kit@claude-plugins-kit-generated` itself.

To refresh manually from a source checkout:

```sh
plugins/claude-plugins-kit/scripts/launch.sh sync --install
```

From an installed copy, invoke the `claude-plugins-kit:claude-plugins` gateway skill. Ask the skill to refresh the bridge. The gateway locates its installed plugin root before it runs the command. A successful change uses native `codex plugin add` or `codex plugin remove`. Restart Codex after a change.

## Discovery and local data

Claude source discovery uses:

1. `CLAUDE_PLUGINS_REGISTRY` and `CLAUDE_SKILLS_ROOT`.
2. `CLAUDE_CONFIG_DIR`.
3. `$DEVROOT/claude-settings` when present.
4. The platform Claude home, normally `~/.claude`.

Generated files default to the platform application-data directory under `codex-kit`: `$XDG_DATA_HOME/codex-kit` or `~/.local/share/codex-kit` on Linux, `~/Library/Application Support/codex-kit` on macOS, and `%LOCALAPPDATA%\\codex-kit` on Windows. Set `CODEX_KIT_DATA_ROOT` to override it. The bridge rejects generated roots inside Git worktrees and paths that traverse symlinks.

The launcher chooses Python at runtime. `CODEX_KIT_PYTHON` has highest priority, followed by the managed Python under `~/.local/share/python-standalone`, then a system Python 3 command. It never provisions dependencies. Forwarded Python entry points reuse the existing Claude-provisioned plugin environment.

## Safety and collisions

- An installed native Codex plugin with the same name wins. Its generated wrapper is skipped and the native plugin is never modified.
- Duplicate Claude plugin names from different marketplaces are both skipped.
- Invalid or missing registries, skill roots, manifests, installed paths, Codex listings, state, or ownership records fail closed without pruning wrappers.
- Only exact bridge-owned wrapper trees and native selectors can be updated or removed. Foreign files block mutation and are preserved.
- The bridge never edits the downloaded plugin cache for Codex. It never edits the Claude registry, plugins, skills, or environments.

If you want a full cleanup, use the gateway `uninstall` operation before you remove the authored plugin. This operation removes all bridge-owned wrapper plugins. If you remove only the authored plugin, the Claude installations do not change.

See [COMPATIBILITY.md](COMPATIBILITY.md) for the reviewed compatibility boundary.

## Development

```sh
python3 -m pytest -q
```

The runtime uses only the Python standard library.

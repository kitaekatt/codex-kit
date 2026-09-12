# Codex Kit

Codex Kit contains `claude-plugins-kit`, a portable Codex plugin that exposes user-installed Claude Code skills through native Codex wrapper plugins. Wrappers contain forwarding and compatibility instructions only. The canonical Claude skill files stay in their installed locations.

## Install

A Codex build with native plugin commands is required. The launcher uses Python 3.10 or newer when available, or downloads a private, SHA256-verified CPython runtime. A fleet checkout and Claude bootstrap installation are not required.

```sh
codex plugin marketplace add kitaekatt/codex-kit
codex plugin add claude-plugins-kit@codex-kit
```

Start a new Codex thread so that Codex discovers the gateway skill. Invoke `claude-plugins-kit:claude-plugins` and ask it to refresh the bridge. If the gateway reports a catalog change, start one more thread to load the skills.

Codex 0.154.0 loads the bundled SessionStart hook. An older package included a second manifest that shadowed the native manifest and prevented hook discovery. Review and trust the hook through `/hooks`; changed hook definitions require review again. On startup and resume, the trusted hook runs `maintain --startup` to converge the catalog and check for newer official bridge releases.

The generated marketplace is named `claude-plugins-kit-generated`. It is a marketplace name, not one plugin selector. It contains one wrapper plugin for each installed Claude plugin that exposes skills, plus `claude-user` when personal skills exist. The bridge installs selectors such as `awesome-kit@claude-plugins-kit-generated` itself.

To refresh manually from a source checkout:

```sh
plugins/claude-plugins-kit/scripts/launch.sh sync --install
```

From an installed copy, invoke the `claude-plugins-kit:claude-plugins` gateway skill. Ask the skill to refresh the bridge. The gateway locates its installed plugin root before it runs the command. A successful change uses native `codex plugin add` or `codex plugin remove`. Restart Codex after a change.

## Migrate a standalone bridge

Use the source checkout when an older bridge installed hooks, discovery stubs,
or state directly in `CODEX_HOME`. The source launcher works before Codex can
discover the native gateway.

Preview the migration on macOS or Linux:

```sh
plugins/claude-plugins-kit/scripts/launch.sh migrate
```

Apply it after you review the preview:

```sh
plugins/claude-plugins-kit/scripts/launch.sh migrate --install
```

On Windows PowerShell, run the checkout launcher through `DEVROOT`:

```powershell
& (Join-Path $env:DEVROOT 'codex-kit\plugins\claude-plugins-kit\scripts\launch.cmd') migrate --install
```

The apply operation first writes a backup and journal. It then registers the
native marketplace when necessary. It installs the authored plugin and
continues through its installed launcher. It verifies the native catalog before
it removes exact-owned old artifacts. For an eligible replacement, it verifies
the native plugin before it removes the old stub. Reported retired and collision
identities are not eligible replacements. Cleanup handles them by their reported
dispositions. Native plugins win collisions.

The migration stores the backup and journal in `migration-backups/` under the
data root. This directory is outside Git worktrees and the generated
marketplace. An interrupted operation is safe to run again. Its JSON result
reports the `phase` and `backup` path. A foreign edit stops cleanup and
remains unchanged. The migration resolves a `CODEX_HOME` symlink to its target.

The migration removes an exact-owned legacy hook after native verification. It
does not install a new automatic user hook or change bootstrap behavior. It
does not merge a portable configuration manifest. Pulling a settings
repository can remove tracked old code, but it cannot install native payloads
or clean ignored artifacts. Use the migration instead of manual deletion.

## Discovery and local data

Claude source discovery uses:

1. `CLAUDE_PLUGINS_REGISTRY` and `CLAUDE_SKILLS_ROOT`.
2. `CLAUDE_CONFIG_DIR`.
3. `$DEVROOT/claude-settings` when present.
4. The platform Claude home, normally `~/.claude`.

Generated files default to the platform application-data directory under `codex-kit`: `$XDG_DATA_HOME/codex-kit` or `~/.local/share/codex-kit` on Linux, `~/Library/Application Support/codex-kit` on macOS, and `%LOCALAPPDATA%\\codex-kit` on Windows. Set `CODEX_KIT_DATA_ROOT` to override it. The bridge rejects generated roots inside Git worktrees and paths that traverse symlinks.

The launcher chooses Python at runtime. `CODEX_KIT_PYTHON` has highest priority, followed by an existing managed Python under `~/.local/share/python-standalone`, then a compatible system Python command. If none is available, it installs pinned CPython 3.13.15 from python-build-standalone release 20260901 under the application-data root's `runtime/` directory. It verifies the shipped SHA256 before extraction, stages the install atomically, and uses an OS lock that releases when the installer exits. An interrupted download is retried; abandoned scratch directories are never treated as installed runtimes. It leaves PATH and system Python unchanged. Set `CODEX_KIT_RUNTIME_DOWNLOAD=0` to disable new downloads. Existing verified private runtimes still work offline.

The fallback targets macOS and Windows on ARM64/x86-64 and glibc Linux on aarch64/x86-64. POSIX hosts need curl, tar, a SHA256 verifier, and lockf (macOS) or flock (Linux). Windows uses PowerShell and tar.exe. The launcher selects RemoteSigned policy for its process only; it does not change persistent execution policy or override Group Policy. macOS ARM64 fallback execution is tested; Windows runtime execution has not been verified in this release's local environment. See [runtime notices](plugins/claude-plugins-kit/RUNTIME-NOTICES.md) for artifact provenance and licenses. Forwarded Python entry points still reuse the existing Claude-provisioned plugin environment; this private runtime does not install canonical plugin dependencies.

`maintain` refreshes local sources before checking releases. Official release checks run at most every six hours, with a fifteen-minute retry interval after failure; `maintain --check-updates` forces a check. Offline release errors are reported and do not prevent local catalog convergence. `CODEX_KIT_AUTO_UPDATE=0` disables release checks. Local and foreign marketplaces are never automatically upgraded. Disabled plugins remain disabled; their state is rechecked immediately before native installation, although Codex's CLI does not offer an atomic conditional install against a simultaneous user toggle.

Maintenance reports legacy artifacts and interrupted migration journals through `migrate` preview. Run `migrate --install` for verified cleanup. A newer bridge can resume an older journal of the same schema after validating its complete backup; it repeats native installation and catalog verification before cleanup. A journal from a newer release is refused.

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

# Maintenance and lifecycle

The bridge discovers Claude sources in this order:

1. `CLAUDE_PLUGINS_REGISTRY` and `CLAUDE_SKILLS_ROOT` overrides.
2. `CLAUDE_CONFIG_DIR`.
3. `$DEVROOT/claude-settings` when it exists.
4. The platform Claude home, normally `~/.claude`.

Generated state defaults to the platform application-data directory under `codex-kit`: `$XDG_DATA_HOME/codex-kit` or `~/.local/share/codex-kit` on Linux, `~/Library/Application Support/codex-kit` on macOS, and `%LOCALAPPDATA%\\codex-kit` on Windows. `CODEX_KIT_DATA_ROOT` overrides it. The bridge refuses a data root inside a Git worktree or through a symlink.

The generated marketplace is `claude-plugins-kit-generated`. Synchronization uses native `codex plugin marketplace add`, `codex plugin add`, and `codex plugin remove`. It never edits the downloaded plugin cache for Codex. An installed native Codex plugin with the same name wins. The bridge reports and skips that wrapper. It does not modify the native plugin.

Resolve `<launcher>` with the [runtime contract](runtime.md). Run `<launcher> maintain` for catalog and release maintenance, or `<launcher> sync --install` for a manual catalog refresh. Start a new Codex thread if a reported catalog change is not visible. Always surface current, changed, error, and warning results accurately.

Maintenance checks the official bridge release at most every six hours and retries failed checks after fifteen minutes. `<launcher> maintain --check-updates` forces a check. `CODEX_KIT_AUTO_UPDATE=0` disables release checks. Local and foreign marketplaces are never upgraded automatically. Local catalog refresh still runs when a release check fails offline. Disabled generated plugins remain unchanged, and authored bridge disablement or removal stops automatic maintenance. Native CLI state is rechecked before installation; a simultaneous user toggle cannot be made atomic with the native add command.

Startup reports legacy artifacts through a read-only migration preview. Use `<launcher> migrate --install` for cleanup. An older same-schema journal can resume under a newer release after complete backup validation; native installation and verification run again before cleanup. A journal from a newer release is refused.

## Migrate an old standalone bridge

Run `<launcher> migrate` to preview a migration. Run `<launcher> migrate --install`
to apply it. These commands have the same contract for source and installed
launchers.

Use a cloned `codex-kit` checkout when the gateway is not yet discoverable:

```sh
plugins/claude-plugins-kit/scripts/launch.sh migrate
plugins/claude-plugins-kit/scripts/launch.sh migrate --install
```

On Windows PowerShell, apply the migration with:

```powershell
& (Join-Path $env:DEVROOT 'codex-kit\plugins\claude-plugins-kit\scripts\launch.cmd') migrate --install
```

The apply operation completes these phases in order:

1. Write a snapshot backup and migration journal.
2. Register the native `codex-kit` marketplace when it is missing.
3. Install the authored native plugin.
4. Continue synchronization through the installed launcher.
5. Verify the native catalog and all eligible native replacements.
6. Remove only exact-owned old hooks, stubs, and state.

An installed native plugin wins a name collision. The migration does not
replace that plugin. It reports the collision identity and retires the old
wrapper identity. For an eligible replacement, the old stub remains until the
native replacement is verified. Reported retired and collision identities are
not eligible replacements. Cleanup removes their exact-owned old stubs under
their reported dispositions.

The migration resolves the live `CODEX_HOME`. If that path is a symlink, it
uses the target. Before native installation, it writes a backup and a journal to
`migration-backups/` under the data root. This location is outside Git and the
generated marketplace. If an operation stops, run the same command again. The
JSON result reports the `phase` and `backup` path.

Only exact bridge-owned artifacts are eligible for cleanup. A foreign edit
stops the migration. The migration preserves that artifact and reports the
conflict. Do not remove old files by hand.

The migration removes an exact-owned legacy hook after native verification. It
does not install a new automatic user hook or change bootstrap behavior. It
does not merge a portable configuration manifest. Pulling a settings
repository removes tracked old code only. A pull cannot install native payloads
or remove ignored runtime artifacts.

Codex 0.154.0 loads this package's bundled hook. An older duplicate manifest caused the previous discovery failure. Review and trust the SessionStart hook through `/hooks`; changed definitions require review again. The trusted hook runs `maintain --startup` on startup and resume. The first submitted prompt triggers startup execution in the tested CLI flow.

Only plugins with bridge ownership markers and matching state records can change. Missing or malformed sources and ownership records cause an error. The bridge leaves existing wrappers in place after these errors. To do a full cleanup, run `<launcher> uninstall` before you remove the bridge. Removing this authored plugin does not remove Claude plugins.

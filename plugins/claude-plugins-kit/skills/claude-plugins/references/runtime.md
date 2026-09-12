# Runtime contract

Generated skills are forwarding instructions. Their metadata identifies the canonical Claude plugin and skill folder. Read the forwarding skill before its source so its compatibility status and Codex-specific instructions apply.

The launcher uses a compatible existing Python >=3.10 or installs a private, pinned, SHA256-verified CPython under the bridge application-data root's `runtime/` directory. No fleet checkout or bootstrap plugin is required for the bridge itself. Set `CODEX_KIT_PYTHON` to select an interpreter or `CODEX_KIT_RUNTIME_DOWNLOAD=0` to forbid downloads. An existing private runtime remains usable offline. PATH and system Python remain unchanged. This fallback does not provision canonical Claude plugin dependencies. Artifact provenance and supported targets are in the plugin's `RUNTIME-NOTICES.md`.

The fallback needs curl, tar, a SHA256 verifier, and lockf on macOS or flock on glibc Linux. Windows needs PowerShell and tar.exe. The Windows launcher selects RemoteSigned for its process only; persistent execution policy and Group Policy remain authoritative. macOS ARM64 fallback execution is verified for this release; Windows runtime execution is unverified.

Locate this installed gateway skill. The plugin root is three directories above its `SKILL.md`. On macOS or Linux, use `<gateway-plugin-root>/scripts/launch.sh`. On Windows, use `<gateway-plugin-root>\\scripts\\launch.cmd`. The launchers resolve their locations and work from any current directory. The commands are:

```text
<launcher> info <plugin@marketplace>
<launcher> personal-info <skill-folder>
<launcher> python <plugin@marketplace> --script path/inside/plugin.py -- [args...]
<launcher> python <plugin@marketplace> -m package.module -- [args...]
```

`info` returns JSON with `source_root`. Resolve the recorded relative path under that root. Then read the live `SKILL.md`. `personal-info` returns the personal skill root and source file. If a source is missing, ambiguous, invalid, or moved, report the error. Do not guess another path.

Translate `${CLAUDE_PLUGIN_ROOT}` to `source_root` and `${CLAUDE_SKILL_DIR}` to the canonical skill folder. Resolve ordinary supporting files, scripts, and assets from that canonical folder.

For a skill dependency, first locate its forwarding wrapper in the available Codex skills catalog by matching source identity and skill metadata, including `source-kind: personal` for personal skills. Read that wrapper and apply its compatibility instructions before reading any canonical `SKILL.md`. This procedure covers `Skill(...)` calls, slash commands, bare skill names, relative or sibling `SKILL.md` paths, and entries in `required_skills` that identify actual skills; other entries remain host capability requirements. A native skill may substitute only when a documented bridge mapping identifies the dependency; a same-name collision alone is insufficient. Report a missing, ambiguous, stale, disabled, or Claude-only wrapper instead of loading its Claude source directly.

For plugin skills, resolve `skills/<name>/SKILL.md` against `source_root` and sibling `../<name>/SKILL.md` against the current canonical skill folder. Match the resulting path to the wrapper's `source-relative-path` metadata.

Keep conditional references conditional; a related-skills list or metadata alone does not require eager loading or authorize workflow execution. Ordinary reference documents do not require loading their owner's skill unless the source instructions say so.

The `python` command uses the plugin's existing Claude-provisioned virtual environment. It preserves the caller's working directory, exports `CLAUDE_PLUGIN_ROOT`, `<PLUGIN>_ROOT`, and `<PLUGIN>_VENV`, and passes arguments without a shell. It does not install or repair dependencies. When the runtime is absent, use Claude's normal provisioning lifecycle and retry.

Codex does not automatically implement Claude-only commands, agents, hooks, or Workflow. Use equivalent Codex capabilities when the forwarding skill says they are supported or adapted. Report an honest limitation for `claude-only` skills.

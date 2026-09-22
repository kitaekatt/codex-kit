# Runtime contract

Generated skills are forwarding instructions. Their metadata identifies the canonical Claude plugin and skill folder. Read the forwarding skill before its source so its compatibility status and Codex-specific instructions apply.

The launcher uses a compatible existing Python >=3.10 or installs a private, pinned, SHA256-verified CPython under the bridge application-data root's `runtime/` directory. No fleet checkout or bootstrap plugin is required for the bridge itself. Set `CODEX_KIT_PYTHON` to select an interpreter or `CODEX_KIT_RUNTIME_DOWNLOAD=0` to forbid downloads. An existing private runtime remains usable offline. PATH and system Python remain unchanged. This fallback does not provision canonical Claude plugin dependencies. Artifact provenance and supported targets are in the plugin's `RUNTIME-NOTICES.md`.

The fallback needs curl, tar, a SHA256 verifier, and lockf on macOS or flock on glibc Linux. Windows needs PowerShell and tar.exe. The Windows launcher selects RemoteSigned for its process only; persistent execution policy and Group Policy remain authoritative. macOS ARM64 fallback execution is verified for this release; Windows runtime execution is unverified.

Locate this installed gateway skill. The plugin root is three directories above its `SKILL.md`. On macOS or Linux, use `<gateway-plugin-root>/scripts/launch.sh`. On Windows, use `<gateway-plugin-root>\\scripts\\launch.cmd`. The launchers resolve their locations and work from any current directory. The commands are:

```text
<launcher> info <plugin@marketplace> <skill-folder>
<launcher> personal-info <skill-folder>
<launcher> python <plugin@marketplace> --script path/inside/plugin.py -- [args...]
<launcher> python <plugin@marketplace> -m package.module -- [args...]
```

`info` returns JSON with `source_root` and the selected skill's compatibility report. Resolve the recorded relative path under that root. Then read the live `SKILL.md`. `personal-info` returns the personal skill root, source file, and selected skill's report. If a source is missing, ambiguous, invalid, or moved, report the error. Do not guess another path.

## Compatibility reports

`info` and `personal-info` also return per-skill compatibility reports and write concise findings to stderr. The `python` command checks the same reports before launching a script. Findings are advisory: a missing, stale, partial, or unavailable requirement never blocks canonical source resolution or the underlying command. Preserve the underlying command's exit status.

Each generated skill has `compatibility/<skill-folder>.json` beside its forwarding wrapper. The report has a generation ID, source identity and digest, analyzer version, per-requirement evidence and status, inference metadata, and capability profile/mapping versions. The wrapper records the same generation ID. The bridge replaces the wrapper tree and its reports together, then checks that source files did not change during scaffold refresh. A runtime source, profile, or mapping change makes the report stale.

The deterministic extractor currently recognizes the frontmatter list `required_capabilities`. Each item is a host capability identifier. Natural-language lines beginning with `Requires` may be sent for optional semantic extraction. Inference is not run during ordinary source resolution or command use. A scaffold refresh selects the configured model and effort explicitly. Its default is `gpt-5.6-luna` with `xhigh`; it uses the configured `luna` endpoint. The local file `inference-config.json` under the bridge data root can override `inference.model` and `inference.effort`:

```json
{
  "inference": {
    "model": "gpt-5.6-luna",
    "effort": "xhigh"
  }
}
```

If `llm-scripting-kit` is unavailable, the request fails, or the returned model differs from the selected model, deterministic findings remain and the report marks semantic analysis deferred. The bridge does not select a replacement model. The inference CLI reports the actual model but not actual effort. The report records the requested effort and that it was explicitly sent; actual effort remains `null` with status `unverifiable` when the endpoint omits it. Deferred inference is not stored as a successful inference-cache result.

The generic `capability-profile.json` and `capability-mappings.json` files shipped with the bridge define the host profile and explicit equivalence mappings. User-local files with those names under the bridge data root override the shipped files. A mapping must name both its requirement and host capability. Optional `adapter`, `preserves`, and `does_not_preserve` fields describe partial behavior; an adapter does not imply full equivalence. Reports use `available`, `mapped`, `partial`, `unavailable`, and `unmapped` per requirement.

Inference and resolution caches are separate under `compatibility-cache/`. Inference keys include the source digest, analyzed skill identity, analyzer/prompt/schema versions, model, and effort. Resolution keys include the individual requirement, capability profile identity/version, and complete mapping/adapter document. Incomplete inference results are never cache hits. Ordinary source resolution and script dispatch do not call a model.

Translate `${CLAUDE_PLUGIN_ROOT}` to `source_root` and `${CLAUDE_SKILL_DIR}` to the canonical skill folder. Resolve ordinary supporting files, scripts, and assets from that canonical folder.

For a skill dependency, first locate its forwarding wrapper in the available Codex skills catalog by matching source identity and skill metadata, including `source-kind: personal` for personal skills. Read that wrapper and apply its compatibility instructions before reading any canonical `SKILL.md`. This procedure covers `Skill(...)` calls, slash commands, bare skill names, relative or sibling `SKILL.md` paths, and entries in `required_skills` that identify actual skills; other entries remain host capability requirements. A native skill may substitute only when a documented bridge mapping identifies the dependency; a same-name collision alone is insufficient. Report a missing, ambiguous, stale, disabled, or Claude-only wrapper instead of loading its Claude source directly.

For plugin skills, resolve `skills/<name>/SKILL.md` against `source_root` and sibling `../<name>/SKILL.md` against the current canonical skill folder. Match the resulting path to the wrapper's `source-relative-path` metadata.

Keep conditional references conditional; a related-skills list or metadata alone does not require eager loading or authorize workflow execution. Ordinary reference documents do not require loading their owner's skill unless the source instructions say so.

The `python` command uses the plugin's existing Claude-provisioned virtual environment. It preserves the caller's working directory, exports `CLAUDE_PLUGIN_ROOT`, `<PLUGIN>_ROOT`, and `<PLUGIN>_VENV`, and passes arguments without a shell. It does not install or repair dependencies. When the runtime is absent, use Claude's normal provisioning lifecycle and retry.

Codex does not automatically implement Claude-only commands, agents, hooks, or Workflow. Use equivalent Codex capabilities when the forwarding skill says they are supported or adapted. Report an honest limitation for `claude-only` skills.

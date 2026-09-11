# Runtime contract

Generated skills are forwarding instructions. Their metadata identifies the canonical Claude plugin and skill folder. Read the forwarding skill before its source so its compatibility status and Codex-specific instructions apply.

Locate this installed gateway skill. The plugin root is three directories above its `SKILL.md`. On macOS or Linux, use `<gateway-plugin-root>/scripts/launch.sh`. On Windows, use `<gateway-plugin-root>\\scripts\\launch.cmd`. The launchers resolve their locations and work from any current directory. The commands are:

```text
<launcher> info <plugin@marketplace>
<launcher> personal-info <skill-folder>
<launcher> python <plugin@marketplace> --script path/inside/plugin.py -- [args...]
<launcher> python <plugin@marketplace> -m package.module -- [args...]
```

`info` returns JSON with `source_root`. Resolve the recorded relative path under that root. Then read the live `SKILL.md`. `personal-info` returns the personal skill root and source file. If a source is missing, ambiguous, invalid, or moved, report the error. Do not guess another path.

Translate `${CLAUDE_PLUGIN_ROOT}` to `source_root` and `${CLAUDE_SKILL_DIR}` to the canonical skill folder. Resolve referenced files, scripts, and assets from that canonical folder. For a Claude `Skill(plugin:skill)` call, locate the generated Codex plugin in the same marketplace namespace and read its forwarding skill first. Report a missing or ambiguous wrapper instead of bypassing its compatibility guard.

The `python` command uses the plugin's existing Claude-provisioned virtual environment. It preserves the caller's working directory, exports `CLAUDE_PLUGIN_ROOT`, `<PLUGIN>_ROOT`, and `<PLUGIN>_VENV`, and passes arguments without a shell. It does not install or repair dependencies. When the runtime is absent, use Claude's normal provisioning lifecycle and retry.

Codex does not automatically implement Claude-only commands, agents, hooks, or Workflow. Use equivalent Codex capabilities when the forwarding skill says they are supported or adapted. Report an honest limitation for `claude-only` skills.

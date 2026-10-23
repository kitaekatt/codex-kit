# Claude skill compatibility inventory

The bridge discovers enabled user-scope Claude plugins and personal skills.
Hooks, commands, agents, and metadata are not user skill surfaces. The policy
table includes historical sources that other machines can still install.
The [compatibility overrides](plugins/claude-plugins-kit/compatibility-overrides.json) are the authoritative map of explicit adaptations.

## Installed plugin skills

| Marketplace plugins | Exposed skills and compatibility |
|---|---|
| `awesome-kit@plugins-kit` | `arewedone`, `debug-context`, `orchestrate`, `plugin-ecosystem`, `verbose-updates`: adapted to Codex context/tool names; historical `recap` and `task` mappings remain available |
| `bootstrap@plugins-kit` | `bootstrap`, `plugin-dev`: adapted; preserve lifecycle/path contracts |
| `claude-ui-kit@plugins-kit` | `statusline`: Claude-only; it configures Claude's status line and cannot provide the current Codex session's status |
| `content-pipeline-kit@plugins-kit` | `content-pipeline-domain`: supported; `background-pipeline`, `workflow-pipeline`, `execute-work-unit`: Claude-only because contracts require Claude `--bg`, native Workflow, and strict worker Write/allowlists |
| `git-kit@plugins-kit` | `git-code-review`: adapted; preserve review lane and GitHub CLI prerequisites |
| `hue-kit@plugins-kit` | `hue-domain`: adapted; preserve Hue environment/CLI, translate prompts/opener |
| `llm-kit@plugins-kit` | `content-pipeline-domain`: supported; `background-pipeline`, `workflow-pipeline`, `execute-work-unit`: Claude-only because contracts require Claude `--bg`, native Workflow, and strict worker Write/allowlists; `openrouter-account`: adapted; preserve credential precedence and prompt secrecy |
| `llm-scripting-kit@plugins-kit` | `openrouter-account`: adapted; preserve credential precedence and prompt secrecy |
| `presence-kit@plugins-kit` | `recap`: adapted; preserve text-first reporting and optional interview without inventing Claude prompt ids or state |
| `secrets-kit@plugins-kit` | `secrets-kit`: adapted; invoke installed `bin/secrets-kit`, use a real terminal for tty passphrase verbs |
| `skills-kit@plugins-kit` | `knowledge-encoding`, `md-domain`, `update-documentation`: adapted; `materialized-output`: supported |
| `task-kit@plugins-kit` | `task`: adapted; preserve canonical task behavior and translate emitted skill calls to available Codex skills |
| `unreal-kit@plugins-kit` | `unreal-domain`, `ue-python-api`, `ue-mcp-server`, `fix-up-redirectors`: adapted; UE, MCP, P4, and venv are conditional external capabilities |
| `workflow-kit@plugins-kit` | `workflow-kit`: validation and compilation work; execution requires Claude's native Workflow tool and must be handed to Claude |
| `codex@openai-codex` | `codex-cli-runtime`, `codex-result-handling`, `gpt-5-4-prompting`: Claude-only internal rescue helpers; do not expose as Codex user skills |
| `core@spryfox-plugins` | `copy`, `powershell`: adapted; platform/Windows capability conditional |
| `designer@spryfox-plugins` | `bulk-rename`: adapted; preserve encoding-safe rewrite and structural validation |
| `engineer@spryfox-plugins` | `investigate-crash`, `open`: adapted; Backtrace and platform opener conditional |
| `prototyping@spryfox-plugins` | `codemap`, `debug`, `migrate-id`, `prototype-ui`, `quick`, `tdd`, `test`, `verify-cpp`: adapted; preserve project indexes, test evidence, and engine/P4 prerequisites |
| `claude-admin@spryfox-plugins` | `my-tool-permissions-audit`, `tool-permissions-pattern`: adapted; operate on settings corpus and preserve safety framework |
| `claude-sandbox@spryfox-plugins` | `backup-claude-to-git`, `writing-skills`: adapted; preserve private `~/.claude` target and skills-kit cross-reference |

Plugins without an exposed `SKILL.md` provide no forwarding surface.
Source-tree presence alone does not establish an installed skill.

## Personal skills

Personal skills use their own source identities in the override map. Skills
without an explicit override use the supported default and retain capability
reports. The default is not evidence that external runtime requirements exist.

## Skill moves and native coverage

The 2026-10-23 audit observed 14 enabled plugin sources and 24 personal skills,
with 54 exposed skills in total. Three adaptations needed current identities:
`arewedone` moved to `awesome-kit`, `task` moved to `task-kit`, and `recap`
moved to `presence-kit`. Their earlier mappings remain for legacy installs.
The llm-kit consolidation already had mappings at its current identity.

Native adapters cover individual skills, not entire Claude plugins. Generated
wrappers expose uncovered skills. The [maintenance contract](plugins/claude-plugins-kit/skills/claude-plugins/references/maintenance.md)
defines coverage results and diagnostics for possible mapping moves. The
[runtime contract](plugins/claude-plugins-kit/skills/claude-plugins/references/runtime.md)
defines how an agent resolves native substitutions.

## Runtime boundary

Forwarders refer to the resolved installed plugin root, never a development
checkout. `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_SKILL_DIR}` are source tokens to
translate at invocation time. External tools, credentials, UE/Hue/P4 services,
and venvs are prerequisites to report when absent, not evidence of Claude-only
behavior. The three content-pipeline worker/orchestration skills, three internal
Codex rescue helpers, and Claude statusline have Claude-only stubs. Workflow
execution has a Claude-only boundary within its otherwise usable skill.

Inspection covered installed manifests, skill trees, frontmatter, procedures,
and relevant scripts/imports. No live hooks, provisioning, UE/MCP, Hue, P4,
OpenRouter, Chromium, or age operations were run.

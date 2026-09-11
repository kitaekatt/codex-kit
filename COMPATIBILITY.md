# Claude skill compatibility inventory

Reviewed scope is the user-scope records discovered from
Claude installed plugin registry, resolved at audit
time to stable `installPath` versions. This covers 24 registry plugins and 46
exposed `SKILL.md` files. Hooks, commands, agents, and metadata are not user
skill surfaces.

## Installed plugin skills

| Marketplace plugins | Exposed skills and compatibility |
|---|---|
| `awesome-kit@plugins-kit` | `debug-context`, `orchestrate`, `plugin-ecosystem`, `recap`, `task`, `verbose-updates`: adapted to Codex context/tool names |
| `bootstrap@plugins-kit` | `bootstrap`, `plugin-dev`: adapted; preserve lifecycle/path contracts |
| `claude-ui-kit@plugins-kit` | `statusline`: Claude-only; it configures Claude's status line and cannot provide the current Codex session's status |
| `content-pipeline-kit@plugins-kit` | `content-pipeline-domain`: supported; `background-pipeline`, `workflow-pipeline`, `execute-work-unit`: Claude-only because contracts require Claude `--bg`, native Workflow, and strict worker Write/allowlists |
| `git-kit@plugins-kit` | `git-code-review`: adapted; preserve review lane and GitHub CLI prerequisites |
| `hue-kit@plugins-kit` | `hue-domain`: adapted; preserve Hue environment/CLI, translate prompts/opener |
| `llm-scripting-kit@plugins-kit` | `openrouter-account`: adapted; preserve credential precedence and prompt secrecy |
| `secrets-kit@plugins-kit` | `secrets-kit`: adapted; invoke installed `bin/secrets-kit`, use a real terminal for tty passphrase verbs |
| `skills-kit@plugins-kit` | `knowledge-encoding`, `md-domain`, `update-documentation`: adapted; `materialized-output`: supported |
| `unreal-kit@plugins-kit` | `unreal-domain`, `ue-python-api`, `ue-mcp-server`, `fix-up-redirectors`: adapted; UE, MCP, P4, and venv are conditional external capabilities |
| `workflow-kit@plugins-kit` | `workflow-kit`: validation and compilation work; execution requires Claude's native Workflow tool and must be handed to Claude |
| `codex@openai-codex` | `codex-cli-runtime`, `codex-result-handling`, `gpt-5-4-prompting`: Claude-only internal rescue helpers; do not expose as Codex user skills |
| `core@spryfox-plugins` | `copy`, `powershell`: adapted; platform/Windows capability conditional |
| `designer@spryfox-plugins` | `bulk-rename`: adapted; preserve encoding-safe rewrite and structural validation |
| `engineer@spryfox-plugins` | `investigate-crash`, `open`: adapted; Backtrace and platform opener conditional |
| `prototyping@spryfox-plugins` | `codemap`, `debug`, `migrate-id`, `prototype-ui`, `quick`, `tdd`, `test`, `verify-cpp`: adapted; preserve project indexes, test evidence, and engine/P4 prerequisites |
| `claude-admin@spryfox-plugins` | `my-tool-permissions-audit`, `tool-permissions-pattern`: adapted; operate on settings corpus and preserve safety framework |
| `claude-sandbox@spryfox-plugins` | `backup-claude-to-git`, `writing-skills`: adapted; preserve private `~/.claude` target and skills-kit cross-reference |

The registry also has user records for `agent-glue`, `bootstrap-stuck-fix`,
`codex-kit`, `engineering-advanced-fleet`, `job-kit`, and `opencode-kit`; they
expose no `SKILL.md` and have no forwarding capability. `cache-kit`, `pdf-kit`,
`prototypes`, and `yaml-data-editor-kit` are absent from the installed user
registry; source-tree presence is not an installed skill surface.

## Personal skills

The prior source audit covered 22 separate personal files under
the discovered personal Claude skills root; none has a true Claude-only semantic blocker.
Their statuses remain in the personal override map.

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

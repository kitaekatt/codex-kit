---
name: claude-plugins
description: Explain, refresh, troubleshoot, or remove Codex's bridge to user-installed Claude Code plugin skills and personal skills.
---

# Claude skills in Codex

Claude Plugins Kit creates one local Codex forwarding plugin for each user-scoped Claude plugin and one `claude-user` plugin for personal skills. It never copies canonical Claude instructions.

Read only the reference needed:

- For skill invocation and canonical path translation, read [runtime](references/runtime.md).
- For discovery, synchronization, collisions, updates, or removal, read [maintenance](references/maintenance.md).

When a forwarded skill is missing, stale, blocked by a native Codex plugin, or cannot resolve its canonical source, tell the user. Never bypass a forwarding skill's compatibility status by loading a Claude source directly.

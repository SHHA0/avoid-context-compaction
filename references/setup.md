# Setup and limitations

Run `python <skill>/scripts/install.py` to install the skill under `CODEX_HOME/skills/avoid-context-compaction` and merge its lifecycle handlers into `CODEX_HOME/hooks.json`. The installer replaces the former `context-guard` installation and removes its obsolete hook handlers. Restart Codex after installation. Codex requires unmanaged hooks to be reviewed and trusted before they run; approve the displayed definition only after checking that it points to the installed `avoid_context_compaction.py`.

The hooks perform these actions:

- `UserPromptSubmit` and `PostToolUse`: read the current session's latest supported token snapshot. They stay silent below 75%, warn once at 75%, and request handoff once at 85%. A new notification is allowed when the level changes or usage grows another five percentage points.
- `PreCompact`: write a small marker under `<project>/.avoid-context-compaction/<session-id>/`. It does not block compaction, because blocking could leave a turn unfinished and does not give the model a guaranteed extra summarization call.
- `SessionStart`: after resume or compaction, inject the same session's latest structured handoff when one exists. It does not guess which unrelated task a brand-new session should resume.

Use `python <skill>/scripts/avoid_context_compaction.py --warn 0.70 --handoff 0.82 status` for a one-off threshold override. To change automatic thresholds, add the same flags before `hook` in each handler command.

The local JSONL transcript is a practical adapter, not a stable public API. The official App Server exposes `thread/tokenUsage/updated`, which is the preferred future adapter for a resident integration. A skill hook is short-lived and does not subscribe to that event stream. If the transcript schema changes, this implementation reports `unknown` rather than estimating.

Hooks run at lifecycle boundaries. Hosted tools can bypass tool hooks, a token snapshot can lag current additions, and a large result can jump over a warning line. `PreCompact` remains the final observable boundary. Network stream disconnections are transport failures; this skill preserves disk checkpoints but cannot prevent or repair the connection.

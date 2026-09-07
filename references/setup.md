# Setup and verification

Run `python <skill>/scripts/install.py` to copy the skill into `CODEX_HOME/skills/avoid-context-compaction` and install a marked basic-monitor block in `CODEX_HOME/AGENTS.md`. It preserves unrelated instructions and supports upgrades from `context-guard`.

Restart Codex so the global instruction is loaded. Basic mode requires no Hook trust. It asks the main agent to run `final-check` before every final response; normal checks return a visible percentage, while threshold checks also offer a handoff.

Installation does not activate all conversations. Invoke the skill once in the intended conversation so it runs `activate --project <absolute-session-project>`. State is scoped to that session ID and the session working directory. A new conversation must opt in separately. When changing the session's project directory, activate there too; do not pick another project's latest state.

## Optional lifecycle Hooks

Install them with `python <skill>/scripts/install.py --with-hooks`, restart Codex, then review/trust the updated definitions in the client (CLI: `/hooks`). Do not write trust records or bypass Hook trust. See [official Hooks documentation](https://learn.chatgpt.com/zh-Hans/docs/hooks).

Hooks add these guards:

| Event | Enabled-session behavior |
| --- | --- |
| UserPromptSubmit | Reinject monitoring instructions on each request; retain unanswered offers. |
| PostToolUse | Record threshold peaks quietly. |
| PreCompact | Persist a pending compaction reminder without blocking work. |
| SessionStart | Restore monitoring instructions and the exact existing handoff path, if any. |
| Stop | Check current/pending usage and final text; request one short continuation if the footer was missed. |

Stop uses `decision: "block"` plus `reason` to ask for a continuation, not to abort the task. `stop_hook_active` and a per-turn ID prevent correction loops. If the continuation still omits the footer, emit a hook warning and stop retrying. Hooks cannot directly edit displayed final messages or create arbitrary choice buttons.

## Diagnose before claiming success

Run `python <skill>/scripts/avoid_context_compaction.py doctor --project <absolute-session-project>`.

- `basic_instructions_configured` confirms the marked global instruction exists.
- `last_final_check` and `final_check_count` provide evidence that final checks were run.
- `missing_events` lists absent configured handlers, including Stop.
- `observed_events` stores actual event timestamps received by this script for this enabled session.
- `automatic_monitoring: unverified` means no Stop execution was observed. Configuration alone does not establish trust or runtime support.

For basic-mode acceptance, activate in a conversation and complete two short requests. Each final answer should show a usage footer, and `final_check_count` should increase. For Hook acceptance, actual UserPromptSubmit/PostToolUse/Stop timestamps must advance. Synthetic tests validate code paths, not client delivery.

Defaults: warn 0.75, handoff 0.85, stale after 300 seconds. Set global flags **before** the subcommand, e.g. `--warn 0.70 --handoff 0.82 final-check --project ...`. For automatic thresholds, use the same flags before `hook` in every installed handler. Keep manual and automatic settings consistent.

## Limits

The JSONL format is version-dependent. Incremental observation retains peaks since activation; incomplete lines are retried. Current usage uses the latest supported snapshot; stale or unknown data is reported in final replies. Monitoring stores metadata, not transcripts or inferred task facts. OS file locks serialize monitoring state writes.

Hosted tools may bypass PostToolUse. Later checks can recover supported intermediate snapshots and compaction records from the transcript, but missing records cannot be reconstructed. Large output may cross both thresholds and compact before a final reply. The default policy finishes current work and then offers a handoff; it cannot guarantee that automatic compaction never happens during that work.

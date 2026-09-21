# Setup and verification

Run `python <skill>/scripts/install.py` to copy the skill into `CODEX_HOME/skills/context-continuity` and update its marked block in `CODEX_HOME/AGENTS.md`. Restart Codex, then invoke the skill in the intended conversation. Installation alone does not activate monitoring.

Basic mode asks the main agent to run `final-check` before every final reply. A due 50% or 80% update is written before the reply, then the final check is repeated. The visible reply ends with only the current usage sentence.

## Optional lifecycle Hooks

Install with `python <skill>/scripts/install.py --with-hooks`, restart, then review and explicitly trust the `Stop` handler in `/hooks`. The installer removes all other managed lifecycle handlers from older releases.

| Event | Enabled-session behavior |
| --- | --- |
| Stop | Request one short continuation when a state update or footer was omitted. |

Stop uses `decision: "block"` to request a continuation, not to cancel the task. A turn ID and `stop_hook_active` prevent correction loops. Hooks never deny tool use because of context percentage.

## Diagnose

Run `python <skill>/scripts/context_continuity.py doctor --project <absolute-project>`.

- `basic_instructions_configured` confirms the managed global instruction exists.
- `last_final_check` and `final_check_count` show manual checks.
- `last_state_update` and `state_update_count` show threshold saves.
- `missing_events` reports whether the Stop handler is absent.
- `observed_events` records actual lifecycle delivery.
- `automatic_monitoring: observed` requires a real `Stop` event; configuration alone is not proof.

For acceptance, verify that 49% does not request an update, 50% requests one update, repeated checks do not request another, and 80% requests the second update. After that update, a new user turn must request one rolling refresh while a repeated check in the same turn must not. Compaction starts a new cycle. Verify that even usage above 90% never denies a tool call. Defaults are state updates at 50% and 80%, with snapshots stale after 300 seconds.

The JSONL format is version-dependent. Incremental monitoring retries incomplete records. A large tool result may compact before the next observed boundary, and hosted tools may omit lifecycle events. These limits affect detection timing but never cause a percentage-based task stop.

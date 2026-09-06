---
name: avoid-context-compaction
description: Reduce quality loss from Codex context compaction by monitoring context usage, preserving task constraints and decisions, and creating reliable new-session handoffs. Use for long-running tasks, compaction preparation, or recovery from a saved handoff.
---

# Avoid Context Compaction

Maintain a small, factual checkpoint of the current task. This skill cannot prevent network failures or guarantee lossless memory. Automatic checks require the bundled lifecycle hooks to be installed and trusted.

## Working procedure

1. Read applicable AGENTS.md. On continuation, read the specific handoff the user supplied, then inspect real files and relevant verification evidence before editing. Preserve the original goal and later corrections. Do not select another task merely because its handoff is newer.
2. Run `python <skill>/scripts/avoid_context_compaction.py status` to inspect the current session. It uses CODEX_THREAD_ID or CODEX_SESSION_ID and never chooses the newest unrelated transcript. Unknown or stale data is not zero usage.
3. Save a checkpoint after meaningful progress, a user correction, a consequential decision, or a verified result. Write structured JSON as described in [references/checkpoints.md](references/checkpoints.md), then run `python <skill>/scripts/avoid_context_compaction.py checkpoint --input <file> --project <absolute-project-path>`. Read the generated handoff to verify it faithfully captures the task. The script formats facts you provide; it cannot independently infer accomplishments.
4. At the configured warning level, update the checkpoint at the next safe boundary. At the handoff level, finish an immediately achievable task or prepare a handoff before starting another large phase. Give the user the generated HANDOFF.md and the exact contents of RESUME.txt and suggest opening a new task. Do not open a task automatically, commit, deploy, or stop a running job solely because of a threshold.
5. After compaction, reread the checkpoint and reconcile it with actual state. After a network interruption, verify whether the previous operation completed before retrying it.

Keep explicit user requirements with their scope and reasons. Distinguish verified results, unverified changes, assumptions, rejected approaches, and open questions. Include pending approvals and running process identifiers when relevant. Never treat a saved plan as new authorization. Do not copy secrets or whole transcripts into checkpoints. Avoid making the user repeat information already available.

## Usage semantics

The supported local adapter reads `event_msg/token_count.info.last_token_usage` and `model_context_window` from the matching JSONL transcript. It reports input/window and (input + output)/window separately; the latter is the conservative warning metric, not exact live occupancy. Cached input remains part of input. Cumulative usage and account rate limits are never context occupancy. New tool output can exceed the last snapshot. A compaction marker invalidates older snapshots. Transcript format is version-dependent; unknown formats fail visibly.

Defaults: warning 75%, handoff 85%, stale after 300 seconds. These are configurable early-warning heuristics, not the actual auto-compaction limit. Large tool output can cross a threshold before a hook runs. Hooks only check supported lifecycle boundaries, not every instant.

For installation, hook trust, supported events, limitations, and configuration, read [references/setup.md](references/setup.md).

# Task-state and handoff input

Use this schema for both threshold task-state updates and user-requested handoffs. Supply factual state in the user's language; do not copy secrets, entire transcripts, or unsupported claims.

```json
{
  "base_state_saved_at": "Required only for state-refresh: copy state.json meta.saved_at, or null when state.json does not exist",
  "language": "BCP 47 tag matching the user's language, for example zh-CN or en",
  "task": "Short task name",
  "project_goal": "The current project objective and acceptance criteria",
  "expected_outcome": "The observable result the user expects",
  "requirements": ["Explicit constraints and their scope"],
  "corrections": ["User corrections or changed requirements, preserving the latest instruction"],
  "decisions": ["Decision, reason, and rejected approach when relevant"],
  "completed": ["Delivered work with paths or identifiers"],
  "results": ["Current task effects or observed outcomes"],
  "verification": ["Check, outcome, time, and relevant version; say untested when appropriate"],
  "remaining": ["Outstanding work or blocker"],
  "next_steps": ["Concrete next action"],
  "cautions": ["Unverified assumptions, authorization limits, and risks"],
  "workspace": ["Branch, uncommitted changes, running jobs, and operation IDs"],
  "status": "active"
}
```

All task fields except `language` are required. `base_state_saved_at` is used only by `state-refresh`; omit it for threshold `state-update`. `status` is `active`, `ready`, or `complete`. Empty lists are allowed. The script accepts the older `goal` field as `project_goal`, defaults `expected_outcome` to that goal, and treats missing `corrections` or `results` as empty for compatibility.

At a 50% or 80% threshold, update only the current conversation's `TASK_STATE.md` and `state.json`. Preserve useful earlier facts while incorporating later corrections and evidence. One update at 80% also satisfies an unprocessed 50% crossing in that cycle. After the 80% update, refresh the same state once at the end of each later user turn until compaction begins a new cycle; this closes the information gap between the 80% snapshot and the actual compaction point.

For an explicit handoff request, first read the current `TASK_STATE.md` and `state.json`. Refresh the complete state with everything that occurred since the last threshold save. The refresh command verifies `base_state_saved_at` against the file it read, and the handoff command accepts no separate content input: it renders directly from that refreshed `state.json`. The generated `RESUME.txt` points to the exact `HANDOFF.md`; the new conversation must verify actual files, workspace state, and interrupted operations before continuing.

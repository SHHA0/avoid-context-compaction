# Checkpoint input

In a monitored session, generate these files only after the user chooses yes (or explicitly requests a handoff). Record that choice with `decision --choice yes --project <absolute-project>` first. Enabling the skill or reaching a threshold does not authorize a checkpoint. One successful generation consumes the recorded choice; failed attempts can be retried.

Use a UTF-8 JSON object with these required fields:

```json
{
  "task": "Task name",
  "goal": "Original objective and acceptance criteria",
  "requirements": ["Explicit user constraint, its scope and reason"],
  "completed": ["Delivered file or result, with location"],
  "verification": ["Command/check, outcome, time and relevant version; say untested when appropriate"],
  "decisions": ["Decision and reason; rejected approaches and why"],
  "remaining": ["Outstanding work or blocker"],
  "next_steps": ["Concrete next action"],
  "cautions": ["Unverified assumptions, authorization limits, risks"],
  "workspace": ["Branch and uncommitted changes if relevant; running jobs and IDs"],
  "status": "active"
}
```

`status` is `active`, `ready`, or `complete`. Empty lists are allowed when nothing applies. Do not fill fields with fictional evidence. `ready` means the record is ready for handoff, not that the task is complete.

Each save creates an immutable generation under `<project>/.avoid-context-compaction/<session-id>/`. `current.json` points to the newest successful generation. Older generations remain available. The generated RESUME.txt explicitly identifies the task and handoff; a new session should use that path rather than a global "latest task" pointer. Recovery also recognizes the legacy `.context-guard` directory so existing handoffs are not lost after an upgrade.

On recovery read the handoff, relevant AGENTS.md and referenced evidence. Check actual files and worktree state. State important discrepancies and resolve them before relying on old verification. Preserve user corrections over older decisions, without silently extending their scope.

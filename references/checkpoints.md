# Checkpoint input

In a monitored session, generate these files only after the user chooses yes (or explicitly requests a handoff). Record that choice with `decision --choice yes --project <absolute-project>` first. Enabling the skill or reaching a threshold does not authorize a checkpoint. One successful generation consumes the recorded choice; failed attempts can be retried.

Use a UTF-8 JSON object with the following schema. `language` is optional for compatibility; all other fields are required:

```json
{
  "language": "BCP 47 tag matching the user's language, for example zh-CN or en",
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

`language` controls the generated `HANDOFF.md` headings and `RESUME.txt` instructions so they match the user's language. Chinese (`zh` tags) and English are currently supported. For compatibility with older checkpoint files, the field is optional; when omitted, the script detects Chinese text and otherwise uses English. `status` is `active`, `ready`, or `complete`. Empty lists are allowed when nothing applies. Do not fill fields with fictional evidence. `ready` means the record is ready for handoff, not that the task is complete.

Each save atomically updates one project-wide `HANDOFF.md`, `RESUME.txt`, and `checkpoint.json` set. `current.json` points to that set. On the first save after an upgrade, the script reuses the newest valid legacy handoff target when one exists. The generated `RESUME.txt` identifies the task and exact handoff path. Recovery also recognizes the legacy `.context-guard` directory so existing handoffs are not lost after an upgrade.

On recovery read the handoff, relevant AGENTS.md and referenced evidence. Check actual files and worktree state. State important discrepancies and resolve them before relying on old verification. Preserve user corrections over older decisions, without silently extending their scope.

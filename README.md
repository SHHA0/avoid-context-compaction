# Avoid Context Compaction

Preserve task quality before a long Codex conversation is compacted.

`avoid-context-compaction` monitors the latest available context-usage snapshot, asks Codex to save structured task state before the context becomes crowded, and creates handoff files that a new session can use to continue the work.

## The problem it solves

Long-running Codex tasks often contain more than a goal. They also contain user corrections, rejected approaches, decisions and their reasons, verification results, incomplete changes, running processes, and permission boundaries.

When a conversation is compacted, a shorter summary may preserve the main objective while losing some of that detail. A later continuation can then repeat work, forget a constraint, rely on an outdated decision, or reintroduce a rejected approach. Network stream interruptions can create a similar recovery problem when important progress only exists in the conversation.

This project reduces that risk by keeping a small, structured checkpoint on disk and by warning before the context window becomes crowded. It does **not** disable Codex compaction, guarantee lossless memory, or repair network transport failures.

## Main features

- **Session-specific usage checks.** Reads the latest supported `token_count` event from the transcript belonging to `CODEX_THREAD_ID` or `CODEX_SESSION_ID`. It does not select another session merely because its transcript is newer.
- **Correct usage semantics.** Uses `last_token_usage` and `model_context_window`. Cumulative token usage and account rate limits are not treated as current context occupancy.
- **Two warning levels.** At 75%, Codex is asked to update the task checkpoint at the next safe boundary. At 85%, Codex is asked to prepare a handoff before starting another large phase.
- **Durable handoffs.** Generates an immutable checkpoint generation containing `HANDOFF.md`, `RESUME.txt`, and `checkpoint.json` under `.avoid-context-compaction/<session-id>/` in the project.
- **Structured task state.** Preserves the task goal, explicit user requirements and corrections, completed work, verification evidence, decisions and reasons, remaining work, next steps, cautions, and workspace state.
- **Compaction lifecycle support.** Records a marker on `PreCompact` and reloads the same session's latest handoff through `SessionStart` after compaction or resume.
- **Safe recovery.** Requires the next session to compare the handoff with real files and verification evidence before continuing.
- **Visible failure states.** Reports stale or unknown usage instead of presenting an estimate as a reliable measurement.
- **Existing-hook preservation.** The installer merges its handlers into `CODEX_HOME/hooks.json` and keeps unrelated user hooks.

The automatic checks run at Codex lifecycle boundaries:

| Hook | Behavior |
| --- | --- |
| `UserPromptSubmit` | Checks the latest usage snapshot when the user submits a prompt. |
| `PostToolUse` | Checks again after supported local tool calls. |
| `PreCompact` | Writes a small compaction marker and points to an existing handoff when available. |
| `SessionStart` | Reloads the same session's saved handoff after startup, resume, or compaction. |

The 75% and 85% values are conservative project defaults, not documented Codex auto-compaction thresholds.

## Installation

### Requirements

- Codex with lifecycle Hooks support
- Python 3.10 or newer
- A local Codex configuration directory, normally `~/.codex` or the path in `CODEX_HOME`

Clone or download this repository, open a terminal in its root directory, and run:

```bash
python scripts/install.py
```

On systems where Python 3 is exposed as `python3`:

```bash
python3 scripts/install.py
```

To install into a non-default Codex directory:

```bash
python scripts/install.py --codex-home /absolute/path/to/.codex
```

The installer:

1. Copies the project to `CODEX_HOME/skills/avoid-context-compaction`.
2. Adds its `SessionStart`, `UserPromptSubmit`, `PostToolUse`, and `PreCompact` handlers to `CODEX_HOME/hooks.json`.
3. Preserves unrelated existing hook handlers.
4. Replaces the former `context-guard` installation and obsolete hook references when upgrading from the earlier name.

Restart Codex after installation. Codex requires non-managed hooks to be reviewed and trusted before execution. Review the hook definition and confirm that its command points to:

```text
CODEX_HOME/skills/avoid-context-compaction/scripts/avoid_context_compaction.py
```

In the Codex CLI, `/hooks` can be used to inspect and trust the configured hooks. See the [official Codex Hooks documentation](https://learn.chatgpt.com/docs/hooks) for the current trust workflow and supported events.

## Usage

After installation, restart, and hook approval, monitoring applies automatically to Codex sessions. You do not need to invoke the skill once at the beginning of every conversation.

You can also invoke it explicitly in Codex:

```text
$avoid-context-compaction check the current context usage and preserve a handoff for this task
```

### Check current usage

From an active Codex session, the skill runs the installed script. On macOS or Linux:

```bash
python3 "$CODEX_HOME/skills/avoid-context-compaction/scripts/avoid_context_compaction.py" status
```

On Windows PowerShell:

```powershell
python "$env:CODEX_HOME\skills\avoid-context-compaction\scripts\avoid_context_compaction.py" status
```

The result includes the matching session ID, snapshot time and age, input and output tokens, context-window size, conservative ratio, and one of these states:

- `ok`: below 75%
- `warning`: 75% to less than 85%
- `handoff`: 85% or higher
- `stale`: the latest snapshot is older than five minutes
- `unknown`: a matching transcript or supported usage event could not be read

### Save a checkpoint manually

Create a UTF-8 JSON file using the schema in [`references/checkpoints.md`](references/checkpoints.md), then run:

```bash
python scripts/avoid_context_compaction.py checkpoint --input checkpoint.json --project /absolute/path/to/project
```

The command creates:

```text
<project>/.avoid-context-compaction/<session-id>/<generation>/HANDOFF.md
<project>/.avoid-context-compaction/<session-id>/<generation>/RESUME.txt
<project>/.avoid-context-compaction/<session-id>/<generation>/checkpoint.json
```

`current.json` points to the newest successful generation for that session. Older generations remain available.

To continue in a new Codex session, paste the contents of `RESUME.txt`. It identifies the exact project, task, and `HANDOFF.md` path and tells Codex to verify the recorded state against the workspace before continuing.

### Override thresholds for a manual check

Global options must appear before the subcommand:

```bash
python scripts/avoid_context_compaction.py --warn 0.70 --handoff 0.82 status
```

To change the automatic thresholds, add the same options before `hook` in each installed handler command.

## Operational limits

- The hooks check at lifecycle boundaries rather than on every token. A large tool result can cross a threshold before the next check.
- `PostToolUse` covers supported local function tools. Hosted tools such as Web Search do not necessarily pass through that hook path.
- At 85%, the hook instructs the active Codex model to create or update a structured handoff at the next safe point. The hook script does not independently infer task accomplishments or user requirements.
- `PreCompact` writes a marker; it is not a substitute for a previously maintained structured checkpoint.
- A brand-new session is not automatically attached to the newest unrelated project checkpoint. Use the generated `RESUME.txt` so the new session loads the intended task.
- The local JSONL transcript is a version-dependent adapter. If its format changes, the script reports `unknown` instead of guessing.
- Checkpoints intentionally avoid copying entire transcripts or secrets. Review a generated handoff before sharing it outside the local project.

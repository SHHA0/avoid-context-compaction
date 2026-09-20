# Avoid Context Compaction

Keep long Codex tasks moving while preserving recoverable task state.

The skill is opt-in per conversation. Once enabled, it:

- adds one context-usage sentence to the end of every final reply;
- updates the conversation's task state once at 50% and once at 80% of each context cycle;
- starts a new 50%/80% cycle after detected context compaction;
- lets native compaction continue without a percentage-based hard stop; and
- creates a detailed handoff and copyable resume prompt only when the user explicitly requests one.

Task-state updates and handoffs follow the user's language. Chinese and English document templates are included. A threshold update writes `TASK_STATE.md` and `state.json`; it never creates a handoff or asks the user to switch conversations.

## Install

Requires Python 3.10+:

```text
python scripts/install.py
```

Restart Codex, then invoke `$avoid-context-compaction` in the intended conversation. The default basic mode adds a managed block to the user's global `AGENTS.md` and requires no Hook trust. Re-running the installer is safe and preserves unrelated instructions.

Optional lifecycle support:

```text
python scripts/install.py --with-hooks
```

Restart Codex, review and trust the handlers in `/hooks`, then use `doctor` to verify real event delivery. Enhanced mode observes `SessionStart`, `UserPromptSubmit`, `PreCompact`, and `Stop`; it does not install `PreToolUse` or `PostToolUse` gates.

## Commands

| Command | Purpose |
| --- | --- |
| `activate --project <path> --language <tag>` | Enable this conversation and select footer language. |
| `final-check --project <path>` | Return usage, threshold-update status, and the exact footer. |
| `state-update --input <json> --project <path>` | Save the due 50%/80% task state. |
| `decision --choice yes --project <path>` | Record an explicit handoff request. |
| `handoff --input <json> --project <path>` | Generate the detailed handoff and resume prompt. |
| `doctor --project <path>` | Inspect basic instructions and observed Hook events. |
| `status` | Read current usage without enabling monitoring. |

Global flags such as `--session-id`, `--codex-home`, `--transcript`, `--state-thresholds`, and `--stale-seconds` go before the subcommand.

## Saved data

```text
<project>/.avoid-context-compaction/
  <session-id>/
    monitor.json
    monitor.lock
    TASK_STATE.md
    state.json
    HANDOFF.md
    RESUME.txt
    handoff.json
  current.json
```

The session directory prevents unrelated tasks in the same project from overwriting each other. `current.json` records the newest explicitly generated handoff for compatibility and discovery; recovery prompts always identify the exact handoff path.

Usage is estimated from supported local JSONL snapshots. Missing or stale measurements are shown as unavailable. Basic mode relies on agent instructions; trusted Hooks improve delivery checks but cannot edit displayed replies or guarantee that every hosted tool emits lifecycle events.

```text
python -B -m unittest discover -s tests -v
```

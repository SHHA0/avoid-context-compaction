# Context Continuity

Keep long Codex tasks moving while preserving recoverable task state.

The skill is opt-in per conversation. Once enabled, it:

- adds one context-usage sentence to the end of every final reply;
- updates the conversation's task state once at 50% and once at 80% of each context cycle;
- refreshes task state after later user turns once 80% has been reached, until compaction starts a new cycle;
- starts a new 50%/80% cycle after detected context compaction;
- lets native compaction continue without a percentage-based hard stop; and
- creates a detailed handoff and copyable resume prompt only when the user explicitly requests one.

Task-state updates and handoffs follow the user's language. Chinese and English document templates are included. A threshold update writes `TASK_STATE.md` and `state.json`; it never creates a handoff or asks the user to switch conversations.

## Install

Requires Python 3.10+:

```text
python scripts/install.py
```

Restart Codex, then invoke `$context-continuity` in the intended conversation. The default basic mode adds a managed block to the user's global `AGENTS.md` and requires no Hook trust. Re-running the installer is safe and preserves unrelated instructions.

Optional Stop Hook fallback:

```text
python scripts/install.py --with-hooks
```

Restart Codex, review and trust the `Stop` handler in `/hooks`, then use `doctor` to verify real event delivery. It requests one short corrective continuation when a threshold state update or final usage sentence was omitted. Context cycles are detected from the transcript during `final-check`; no other lifecycle Hooks are installed.

## Commands

| Command | Purpose |
| --- | --- |
| `activate --project <path> --language <tag>` | Enable this conversation and select footer language. |
| `final-check --project <path>` | Return usage, threshold-update status, and the exact footer. |
| `state-update --input <json> --project <path>` | Save the due 50%/80% task state. |
| `decision --choice yes --project <path>` | Record an explicit handoff request. |
| `state-refresh --input <json> --project <path>` | Refresh state at the exact handoff moment after reading the latest state file. |
| `handoff --project <path>` | Generate the handoff and resume prompt from the refreshed `state.json`. |
| `doctor --project <path>` | Inspect basic instructions and observed Hook events. |
| `status` | Read current usage without enabling monitoring. |

Global flags such as `--session-id`, `--codex-home`, `--transcript`, `--state-thresholds`, and `--stale-seconds` go before the subcommand.

## Saved data

```text
<project>/.context-continuity/
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

Existing `.avoid-context-compaction` and `.context-guard` session state remains readable. New conversations write to `.context-continuity`.

Usage is estimated from supported local JSONL snapshots. Missing or stale measurements are shown as unavailable. Basic mode relies on agent instructions; trusted Hooks improve delivery checks but cannot edit displayed replies or guarantee that every hosted tool emits lifecycle events.

```text
python -B -m unittest discover -s tests -v
```

---
name: avoid-context-compaction
description: Report context usage at the end of each reply, preserve factual task state at 50% and 80% of each context cycle, and create a detailed handoff only when the user requests one.
---

# Avoid Context Compaction

Use native context compaction to keep work running. This skill records recoverable task state; it never stops work because of a context percentage.

## Enable once per conversation

When the user enables this skill, run:

```text
python <skill>/scripts/avoid_context_compaction.py activate --project <absolute-session-project> --language <user-language-tag>
```

Use the current `CODEX_THREAD_ID` or `CODEX_SESSION_ID`. Activation is scoped to this conversation and project. Reading or installing the skill does not activate it.

If `basic_instructions_configured` is false, explain that persistent basic monitoring requires `python scripts/install.py` and a Codex restart. If `hook_setup.offer` is true, ask the labeled Hook choice once: **Configure Hook enhanced mode? Choose: yes, show setup steps / no, use basic mode.** Basic mode already provides final checks; trusted Hooks add lifecycle reinjection and footer correction. Follow the returned setup steps only when the user chooses enhanced mode.

## Before every final reply

Run:

```text
python <skill>/scripts/avoid_context_compaction.py final-check --project <absolute-session-project>
```

If `state_update_due` is true, read [the task-state schema](references/checkpoints.md) and the existing `TASK_STATE.md` when present, collect factual state in the user's language, preserve still-valid earlier facts while applying later corrections, write it to a temporary JSON file, and run:

```text
python <skill>/scripts/avoid_context_compaction.py state-update --input <json-file> --project <absolute-session-project>
```

Then rerun `final-check`. Append only its nonempty `footer` verbatim as the final sentence of the reply. Do not mention threshold crossings, ask for a handoff, or pause the task because of usage.

The 50% and 80% thresholds apply once per context cycle. When usage jumps across both before a check, one 80% update satisfies both. After a detected compaction, a new cycle begins and the thresholds can trigger again. State updates replace the current conversation's `TASK_STATE.md` and `state.json`; they do not create handoff files.

## Generate a handoff only on request

An explicit request from the user to generate a handoff counts as consent. Run:

```text
python <skill>/scripts/avoid_context_compaction.py decision --choice yes --project <absolute-session-project>
```

Start from the latest task state when available, then collect the detailed facts defined in [the task-state schema](references/checkpoints.md), including requirements, corrections, decisions, results, verification, remaining work, and workspace state. Then run:

```text
python <skill>/scripts/avoid_context_compaction.py handoff --input <json-file> --project <absolute-session-project>
```

Read the generated `HANDOFF.md` and `RESUME.txt`, then provide clickable absolute links and the exact copyable resume prompt. Match the user's language. A handoff request authorizes one successful generation; a failed attempt may be retried. Never infer consent from a threshold, compaction, silence, or an unrelated request.

## Recovery and limits

After compaction, read the current conversation's `TASK_STATE.md` when needed and verify it against actual files and operation state before relying on it. When the user supplies a handoff, read that exact file and applicable `AGENTS.md`; do not select another task by recency.

Usage is the conservative `(input + output) / model_context_window` ratio from supported local snapshots. Cached input is included. Unknown or stale snapshots must be reported as unavailable. The script formats facts supplied by the model; it cannot reconstruct omitted facts, interrupt a running tool, disable compaction, or guarantee Hook delivery.

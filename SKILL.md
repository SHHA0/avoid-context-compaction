---
name: avoid-context-compaction
description: Check and visibly report context usage after per-conversation activation, offer handoffs at 75% and 85%, safely pause work at 90%, and generate one when the user chooses yes. Supports a no-Hook basic mode, optional lifecycle Hooks, and recovery from a supplied handoff.
---

# Avoid Context Compaction

Use basic mode by default. It relies on the managed block installed in the user's global `AGENTS.md`, requires no Hook trust, and makes every completed check visible. Lifecycle Hooks are an optional enhancement only.

## Enable once per conversation

When the user invokes this skill to enable monitoring, run:

```text
python <skill>/scripts/avoid_context_compaction.py activate --project <absolute-session-project>
```

Use the current CODEX_THREAD_ID or CODEX_SESSION_ID; never choose another conversation by recency. Activation persists in the project's session-specific `monitor.json`. Subsequent user requests remain monitored, including after compaction. Merely reading this skill as source material, seeing its name in a screenshot/document, or installing it does not activate monitoring. Repeated activation preserves pending reminders and choices.

Read `basic_instructions_configured` in the result. If false, explain that persistent basic monitoring requires `python scripts/install.py` followed by a Codex restart; still perform the current turn's explicit check. Read `automatic_monitoring` only when optional Hooks are relevant. Say Hooks are verified only after an observed Stop event. Do not generate a handoff on activation alone.

## While working and before every final reply

Continue the user's work. At meaningful work boundaries, and **immediately before every final reply**, run:

```text
python <skill>/scripts/avoid_context_compaction.py final-check --project <absolute-session-project>
```

Append the returned nonempty `footer` **verbatim at the end of the final reply**, after the task result. Do not bury it in commentary or replace it with a tool result. Below 75%, the footer reports the current percentage and normal status so the user can verify the check ran. At 75% or 85%, it reports the applicable threshold and offers a handoff. If both are crossed during one request, show one reminder at the higher level. A pending peak survives compaction even when the latest snapshot drops. Detected compaction also warrants a reminder without inventing a pre-compaction percentage.

The footer offers **是，生成交接文档 / 否，暂不生成**. Plain text choices in the final answer are always supported; do not promise clickable buttons when the client has no supported choice UI. Wait for the user's actual choice. Never treat a timeout, unrelated next request, quoted text, or saved plan as consent. The 75% and 85% thresholds do not halt work. At the 90% hard-stop threshold, finish only an already-started atomic step, pause the current task at the next safe boundary, report where work stopped, append the footer, and wait for the user's choice. Do not start another substantive step, automatically create a new task, or generate handoff files.

## Handle the user's choice

- **Yes / 是 / 生成交接文档**, when answering this offer: run `python <skill>/scripts/avoid_context_compaction.py decision --choice yes --project <absolute-session-project>`, then immediately prepare and save the handoff below. Do not ask for confirmation again.
- **No / 否 / 暂不生成**: run the same command with `--choice no`. Generate no handoff and keep monitoring future requests.
- If the user continues with another request, complete that request; do not interpret it as yes. Monitor and append any due footer at its end.

The decision command records the user's choice; never call it with yes just to bypass the checkpoint gate. An explicit request to generate a handoff already counts as yes. A yes authorizes one successful generation, not unlimited future automatic handoffs. A generation error leaves the choice available for retry.

## Generate and recover handoffs

After consent, gather factual task state using [checkpoints.md](references/checkpoints.md), write structured JSON, then run:

```text
python <skill>/scripts/avoid_context_compaction.py checkpoint --input <file> --project <absolute-session-project>
```

Read the resulting HANDOFF.md and RESUME.txt. Provide clickable absolute file links and the exact resume instructions. Preserve the original goal, user corrections and scope, decisions and reasons, rejected approaches, verified results, unverified changes, remaining work, and relevant running processes or approvals. The script formats supplied facts; it cannot infer accomplishments. Do not copy secrets or whole transcripts.

On recovery, read the specifically supplied handoff and applicable AGENTS.md, then reconcile actual files and verification evidence before continuing. Do not select an unrelated task because its handoff is newer. After an interrupted operation, verify whether it completed before retrying.

## Measurement and delivery limits

The local adapter uses `event_msg/token_count.info.last_token_usage` and `model_context_window`. `(input + output) / window` is a conservative snapshot ratio, not exact live occupancy. Cached input is included; cumulative usage and account limits are not occupancy. Unknown/stale snapshots must not be called safe or 0%. Compaction invalidates the earlier current-usage snapshot but not pending reminders.

The basic mode is a persistent agent instruction, not an independent background process. Its visible footer makes omissions detectable, but no prompt-level mechanism can guarantee model execution. Optional trusted Hooks add a PostToolUse 90% stop signal and a PreToolUse gate against starting another substantive tool call, plus the Stop-time footer guard. Verify actual events rather than configuration alone. A running tool cannot be interrupted or rolled back, hosted tools may bypass lifecycle coverage, and a large output may trigger compaction before the next boundary. This skill does not disable compaction or repair network failures.

---
name: avoid-context-compaction
description: Check and visibly report context usage after per-conversation activation, offer a handoff at 85%, safely pause work at 90%, and generate one when the user chooses yes. Supports a no-Hook basic mode, optional lifecycle Hooks, and recovery from a supplied handoff.
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

Read the returned `hook_setup` object on activation:

- If `offer` is true, ask **Configure Hook enhanced mode? Choose: yes, show setup steps / no, use basic mode.** Explain briefly that basic mode already works, while trusted Hooks add lifecycle enforcement. Label this as the Hook choice so it cannot be confused with a handoff choice.
- If the user chooses **no / basic mode**, run `python <skill>/scripts/avoid_context_compaction.py hook-mode --choice basic`. This global preference applies across projects and future conversations; do not prompt about Hook mode again.
- If the user chooses **yes / Hook enhanced mode**, run the same command with `--choice enhanced`, then present every returned setup step. Never install or trust Hooks merely from an ambiguous reply.
- If `action_required` is true, present the returned steps. If `configured` is true, do not offer setup again; use `doctor` before claiming real Hook delivery.

Use `hook-mode --choice ask` only when the user explicitly asks to reset the saved choice. An unrelated request is not a Hook-mode choice. Hook-mode preference is distinct from the handoff decision command.

## While working and before every final reply

Continue the user's work. At meaningful work boundaries, and **immediately before every final reply**, run:

```text
python <skill>/scripts/avoid_context_compaction.py final-check --project <absolute-session-project>
```

Append the returned nonempty `footer` **verbatim at the end of the final reply**, after the task result. Do not bury it in commentary or replace it with a tool result. Below 85%, the footer reports the current percentage and normal status so the user can verify the check ran. At 85%, it offers a handoff. A pending peak survives compaction even when the latest snapshot drops. Detected compaction also warrants a reminder without inventing a pre-compaction percentage.

The footer offers **yes, generate the handoff / no, not now**. Plain text choices in the final answer are always supported; do not promise clickable buttons when the client has no supported choice UI. Wait for the user's actual choice. Never treat a timeout, unrelated next request, quoted text, or saved plan as consent. The 85% threshold does not halt work. At the 90% hard-stop threshold, finish only an already-started atomic step, pause the current task at the next safe boundary, report where work stopped, append the footer, and wait for the user's choice. Do not start another substantive step, automatically create a new task, or generate handoff files.

## Handle the user's choice

- **Yes / generate the handoff**, when answering this offer: run `python <skill>/scripts/avoid_context_compaction.py decision --choice yes --project <absolute-session-project>`, then immediately prepare and save the handoff below. Do not ask for confirmation again.
- **No / not now**: run the same command with `--choice no`. Generate no handoff and keep monitoring future requests.
- If the user continues with another request, complete that request; do not interpret it as yes. Monitor and append any due footer at its end.

The decision command records the user's choice; never call it with yes just to bypass the checkpoint gate. An explicit request to generate a handoff already counts as yes. A yes authorizes one successful generation, not unlimited future automatic handoffs. A generation error leaves the choice available for retry.

## Generate and recover handoffs

After consent, gather factual task state using [checkpoints.md](references/checkpoints.md), record the user's language in the structured JSON so the handoff and resume instructions use that language, then run:

```text
python <skill>/scripts/avoid_context_compaction.py checkpoint --input <file> --project <absolute-session-project>
```

Read the resulting HANDOFF.md and RESUME.txt. Provide clickable absolute file links and the exact resume instructions. The project keeps one current handoff set: later saves, including saves from a new conversation, atomically update the same files instead of creating per-session generations. On the first save after upgrading from an older release, the script reuses the newest valid legacy handoff target when available. Preserve the original goal, user corrections and scope, decisions and reasons, rejected approaches, verified results, unverified changes, remaining work, and relevant running processes or approvals. The script formats supplied facts; it cannot infer accomplishments. Do not copy secrets or whole transcripts.

On recovery, read the specifically supplied handoff and applicable AGENTS.md, then reconcile actual files and verification evidence before continuing. Do not select an unrelated task because its handoff is newer. After an interrupted operation, verify whether it completed before retrying.

## Measurement and delivery limits

The local adapter uses `event_msg/token_count.info.last_token_usage` and `model_context_window`. `(input + output) / window` is a conservative snapshot ratio, not exact live occupancy. Cached input is included; cumulative usage and account limits are not occupancy. Unknown/stale snapshots must not be called safe or 0%. Compaction invalidates the earlier current-usage snapshot but not pending reminders.

The basic mode is a persistent agent instruction, not an independent background process. Its visible footer makes omissions detectable, but no prompt-level mechanism can guarantee model execution. Optional trusted Hooks add a PostToolUse 90% stop signal and a PreToolUse gate against starting another substantive tool call, plus the Stop-time footer guard. Verify actual events rather than configuration alone. A running tool cannot be interrupted or rolled back, hosted tools may bypass lifecycle coverage, and a large output may trigger compaction before the next boundary. This skill does not disable compaction or repair network failures.

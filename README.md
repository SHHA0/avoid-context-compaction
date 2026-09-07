# Avoid Context Compaction

Enable context monitoring once in a Codex conversation, see a usage status at the **end of every completed task**, and choose whether to generate a handoff at **75% or 85%**.

## Expected experience

1. Run the installer once, then restart Codex. The default basic installation adds a small managed block to the user's global `AGENTS.md`; it does not install Hooks or require Hook trust.
2. Send `$avoid-context-compaction` in a conversation. The skill persists opt-in for that session and workspace.
3. Continue normally. Every final reply ends with a visible usage status. At or above 75%, it also offers **是，生成交接文档 / 否，暂不生成**. At or above 85%, it shows the higher level.
4. Choose yes: the model immediately prepares factual task state and generates `HANDOFF.md`, `RESUME.txt`, and `checkpoint.json`. Choose no: no handoff is generated, and monitoring continues.

Monitoring persists after compaction. A peak remains pending even when compaction lowers current usage. If compaction was observed without a usable peak, report compaction without guessing a percentage. Merely invoking the skill never creates a handoff.

## Install

Requires Python 3.10+:

```text
python scripts/install.py
```

For a custom directory, add `--codex-home <absolute-directory>`. The installer preserves unrelated global `AGENTS.md` content and replaces only its own marked block. Re-running it is safe.

Restart Codex, then invoke the skill in the intended conversation. The basic mode needs no Hook review. To add lifecycle guards on a client that supports them, run `python scripts/install.py --with-hooks`, restart, and review/trust them. See [setup and verification](references/setup.md).

## Commands

Run with `python scripts/avoid_context_compaction.py` from the skill directory, or use the installed script's absolute path:

| Command | Purpose |
| --- | --- |
| `activate --project <absolute-project>` | Persist opt-in for this session; return usage and hook health. |
| `final-check --project <absolute-project>` | Record the check and return the exact final-response status/footer. |
| `decision --choice yes --project <absolute-project>` | Record actual consent for one handoff generation. |
| `decision --choice no --project <absolute-project>` | Decline the offer and continue monitoring. |
| `checkpoint --input <json-file> --project <absolute-project>` | Validate task facts and generate a handoff; enabled sessions require consent. |
| `doctor --project <absolute-project>` | Inspect configured events and observed hook timestamps. |
| `status` | Read current usage without enabling monitoring. |

Session identity comes from CODEX_THREAD_ID or CODEX_SESSION_ID. Global flags (`--session-id`, `--codex-home`, `--transcript`, `--warn`, `--handoff`) go **before** the command. Keep manual and automatic thresholds consistent.

## Saved data

```text
<project>/.avoid-context-compaction/<session-id>/
  monitor.json          activation, observed events, pending peaks, user choice
  monitor.lock          OS lock for concurrent hooks
  current.json          latest successful handoff pointer
  <generation>/
    HANDOFF.md
    RESUME.txt
    checkpoint.json
```

The model supplies facts using the [checkpoint schema](references/checkpoints.md); the script cannot independently summarize accomplishments. Each save gets its own generation. Legacy `.context-guard` handoffs remain readable. In a new conversation, use the exact resume instructions and verify actual workspace state before continuing.

## Limits and tests

Usage is the conservative `(input + output) / context window` ratio from supported local JSONL snapshots, not cumulative usage, account quota, or exact live occupancy. Unknown/stale measurements are reported visibly. Hooks observe lifecycle boundaries, not every token; large results can cause compaction before work ends. This skill does not disable compaction or repair network failures.

Basic mode uses persistent agent instructions and reports every check visibly; it is still a prompt-level mechanism rather than an independent process. Optional Stop Hooks can request one corrective continuation, but client support must be verified. Neither mode can rewrite displayed messages, guarantee model compliance, or create arbitrary choice buttons.

```text
python -B -m unittest discover -s tests -v
```

"""Session opt-in, durable threshold observations, and final-response checks."""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

import avoid_context_compaction as core


EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact", "Stop")
BASIC_BEGIN = "<!-- avoid-context-compaction:basic-monitor:begin -->"
PREFERENCES_FILE = "avoid-context-compaction.json"


def state_path(project, sid):
    return Path(project).resolve() / core.DATA_DIR_NAME / core.safe_id(sid) / "monitor.json"


def preferences_path(args):
    return core.codex_home(args.codex_home) / PREFERENCES_FILE


@contextlib.contextmanager
def locked(path):
    """OS locks release on process exit, including a crashed hook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("monitor state is busy; retry at the next boundary")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def load(path):
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("invalid monitor state")
    return state


def configured_events(args):
    path = core.codex_home(args.codex_home) / "hooks.json"
    configured = []
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(config, dict) and isinstance(config.get("hooks"), dict):
            for event, groups in config["hooks"].items():
                if isinstance(groups, list) and any("avoid_context_compaction.py" in str(g) for g in groups):
                    configured.append(event)
    return configured


def load_preferences(args):
    path = preferences_path(args)
    if not path.exists():
        return {}
    preferences = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(preferences, dict):
        raise ValueError(f"invalid preferences file: {path}")
    return preferences


def hook_install_steps(args):
    executable = Path(core.sys.executable).resolve()
    installer = Path(core.__file__).resolve().with_name("install.py")
    home = core.codex_home(args.codex_home)
    return [
        f'Run: "{executable}" "{installer}" --codex-home "{home}" --with-hooks',
        "Restart Codex so the updated lifecycle Hook definitions are loaded.",
        "Open /hooks, review the avoid-context-compaction handlers, and explicitly trust them.",
        "Invoke $avoid-context-compaction in the intended conversation, then run doctor and verify real Stop, PreToolUse, and PostToolUse events.",
    ]


def hook_setup(args, first_activation=False):
    configured = configured_events(args)
    complete = all(event in configured for event in EVENTS)
    preference = load_preferences(args).get("hook_mode")
    if complete:
        return {
            "mode": "enhanced",
            "configured": True,
            "offer": False,
            "action_required": False,
            "message": "Hook enhanced mode is configured; use doctor to verify actual event delivery and trust.",
        }
    if preference == "basic":
        return {
            "mode": "basic",
            "configured": False,
            "offer": False,
            "action_required": False,
            "message": "Basic mode was selected globally; do not prompt about Hook enhanced mode again.",
        }
    if preference == "enhanced":
        return {
            "mode": "enhanced",
            "configured": False,
            "offer": False,
            "action_required": True,
            "message": "Hook enhanced mode was requested but is not fully configured.",
            "steps": hook_install_steps(args),
        }
    return {
        "mode": "unselected",
        "configured": False,
        "offer": bool(first_activation),
        "action_required": False,
        "message": (
            "Hook enhanced mode is not configured. Ask once in this conversation: "
            "Configure Hook enhanced mode? Choose: yes, show setup steps / no, use basic mode."
        ),
    }


def hook_mode(args):
    path = preferences_path(args)
    with locked(path):
        preferences = load_preferences(args)
        if args.choice == "ask":
            preferences.pop("hook_mode", None)
            preferences.pop("hook_mode_chosen_at", None)
        else:
            preferences.update(hook_mode=args.choice, hook_mode_chosen_at=core.utcnow().isoformat())
        core.atomic_json(path, preferences)
    result = {"choice": args.choice, "preferences": str(path), **hook_setup(args)}
    if args.choice == "basic":
        result["message"] = "Basic mode selected globally. Future conversations must not prompt about Hook mode."
    elif args.choice == "enhanced":
        result["message"] = "Hook enhanced mode selected globally. Complete the returned steps; trust is never written automatically."
        result["steps"] = hook_install_steps(args)
    else:
        result["message"] = "Hook mode preference cleared. The next newly activated conversation will offer the choice again if Hooks are absent."
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def remember(state, usage):
    ratio = usage.get("conservative_ratio")
    if usage.get("level") in {"handoff", "hard_stop"} and isinstance(ratio, (int, float)):
        state["pending_peak"] = max(state.get("pending_peak", 0), ratio)


def observe(state, args, transcript=None):
    """Catch intermediate peaks even if tools/compaction skipped a hook boundary."""
    result = core.usage_status(args, transcript_override=transcript, sid_override=state["session_id"])
    state["usage"] = result
    name = result.get("transcript")
    if name and Path(name).exists():
        path = Path(name)
        offset = state.get("offset", 0) if state.get("transcript") == name else 0
        if offset > path.stat().st_size:
            offset = 0
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line = handle.readline()
                if not line or not line.endswith(b"\n"):
                    break  # Retry an incomplete last record next time.
                offset = handle.tell()
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                stamp = core.parse_time(record.get("timestamp"))
                activated = core.parse_time(state["activated_at"])
                if stamp is None or stamp < activated:
                    continue
                if core.is_compaction_event(record):
                    state["pending_compaction"] = True
                payload = record.get("payload")
                if record.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
                    # Manual final-check also works when prompt hooks are unavailable.
                    state["suppress_turn"] = False
                if record.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                usage = info.get("last_token_usage")
                if not isinstance(usage, dict):
                    continue
                inputs, outputs, window = usage.get("input_tokens"), usage.get("output_tokens", 0), info.get("model_context_window")
                if all(type(n) is int and n >= 0 for n in (inputs, outputs, window)) and window:
                    ratio = (inputs + outputs) / window
                    if ratio >= args.handoff:
                        state["pending_peak"] = max(state.get("pending_peak", 0), ratio)
        state.update(transcript=name, offset=offset)
    remember(state, result)
    if state.get("pending_peak", 0) >= args.stop:
        state["hard_stop_pending"] = True
    return result


def footer(state, args):
    if state.get("suppress_turn"):
        return ""
    usage = state.get("usage", {})
    peak = state.get("pending_peak", 0)
    compacted = state.get("pending_compaction", False)
    if not peak and not compacted:
        if usage.get("level") in {"unknown", "stale"}:
            return "Context monitoring: current usage is unknown (the snapshot is missing or stale), so the threshold cannot be evaluated."
        ratio = usage.get("conservative_ratio")
        return f"Context usage: approximately {ratio:.1%} (normal; below the {args.handoff:.0%} handoff threshold)."
    facts = []
    if peak:
        threshold = args.stop if peak >= args.stop else args.handoff
        label = "hard-stop threshold" if threshold == args.stop else "handoff threshold"
        facts.append(f"the highest usage snapshot since the last decision was approximately {peak:.1%}, reaching the {threshold:.0%} {label}")
    if compacted:
        facts.append("context compaction occurred during this period")
    ratio = usage.get("conservative_ratio")
    if usage.get("level") in {"ok", "handoff", "hard_stop"} and ratio is not None:
        facts.append(f"the latest snapshot is approximately {ratio:.1%}")
    else:
        facts.append("current usage is unknown")
    if state.get("hard_stop_pending"):
        facts.append("the task is paused at a safe boundary and no new step will be started")
    return "Context reminder: " + "; ".join(facts) + ".\nGenerate a handoff? Choose: yes, generate the handoff / no, not now."


def instructions(state, args):
    script = Path(core.__file__).resolve()
    return (
        "avoid-context-compaction is enabled for this conversation. Continue the user's work; before every final reply run "
        f'python "{script}" final-check --project "{state["project"]}", then append the returned footer verbatim. '
        f"At the {args.handoff:.0%} handoff threshold, finish the current work and offer a handoff. At the {args.stop:.0%} hard-stop threshold, "
        "finish only the already-started atomic step, pause the task, return the footer, and wait for the user's choice. Do not generate a handoff "
        "or create a new task automatically. If the user chooses yes, run decision --choice yes, collect factual task state, and run checkpoint. "
        "If the user chooses no, run decision --choice no and keep monitoring."
    )


def delivered(state, args, message):
    """Allow snapshot drift while requiring the applicable level and final choices."""
    ending = str(message or "").rstrip()
    expected = footer(state, args)
    if "Generate a handoff?" not in expected:
        if not expected:
            return False
        # The assistant's footer is measured immediately before its reply, while
        # Stop observes again after that reply has added tokens.  Treat an older
        # normal footer as delivered as long as its semantic level and configured
        # handoff threshold still match; requiring the newly calculated percentage
        # would create a redundant correction turn after every ordinary response.
        if expected.startswith("Context usage: approximately "):
            normal = re.compile(
                rf"Context usage: approximately \d+(?:\.\d+)?% \(normal; below the {re.escape(f'{args.handoff:.0%}')} handoff threshold\)\.$"
            )
            return bool(normal.search(ending))
        return ending.endswith(expected)
    choices = "Generate a handoff? Choose: yes, generate the handoff / no, not now."
    if not ending.endswith(choices):
        return False
    start = ending.rfind("Context reminder:")
    if start < 0:
        return False
    reminder = ending[start:]
    peak = state.get("pending_peak", 0)
    if peak:
        threshold = args.stop if peak >= args.stop else args.handoff
        label = "hard-stop threshold" if threshold == args.stop else "handoff threshold"
        if f"{threshold:.0%} {label}" not in reminder:
            return False
    return not state.get("pending_compaction") or "context compaction occurred" in reminder


def health(args, state):
    configured = configured_events(args)
    agents_path = core.codex_home(args.codex_home) / "AGENTS.md"
    basic_configured = agents_path.exists() and BASIC_BEGIN in agents_path.read_text(encoding="utf-8-sig")
    observed = state.get("observed_events", {})
    return {
        "basic_instructions_configured": basic_configured,
        "hook_mode_preference": load_preferences(args).get("hook_mode", "unselected"),
        "last_final_check": state.get("last_final_check"),
        "final_check_count": state.get("final_check_count", 0),
        "configured_events": configured,
        "missing_events": [e for e in EVENTS if e not in configured],
        "observed_events": observed,
        "automatic_monitoring": "observed" if observed.get("Stop") else "unverified",
        "hard_stop_enforcement": (
            "observed" if observed.get("PreToolUse") and observed.get("PostToolUse") else "unverified"
        ),
        "note": (
            "Configuration alone does not prove hook trust or execution. Verify a real Stop event for automatic reminders "
            "and real PreToolUse/PostToolUse events for 90% hard-stop enforcement after restart."
        ),
    }


def command(args):
    sid = core.session_id(args.session_id)
    if not sid:
        raise ValueError("a session ID is required; refusing to enable an unrelated session")
    path = state_path(args.project, sid)
    if args.command == "doctor":
        state = load(path)
        print(json.dumps({"enabled": state.get("enabled", False), **health(args, state)}, ensure_ascii=False, indent=2))
        return 0
    with locked(path):
        state = load(path)
        first_activation = args.command == "activate" and not state.get("enabled")
        if args.command == "activate" and not state.get("enabled"):
            state = {"enabled": True, "session_id": sid, "project": str(Path(args.project).resolve()),
                     "activated_at": core.utcnow().isoformat(), "observed_events": {}}
        if not state.get("enabled"):
            raise ValueError("monitoring is not enabled for this session; run activate first")
        elif args.command == "decision":
            observe(state, args)
            state.update(approved=args.choice == "yes", awaiting_choice=False, suppress_turn=True,
                         pending_peak=0, pending_compaction=False, hard_stop_pending=False,
                         hard_stop_injected_turn=None)
            output = {"choice": args.choice, "generate_now": args.choice == "yes"}
        else:
            observe(state, args)
            if args.command == "final-check":
                state["last_final_check"] = core.utcnow().isoformat()
                state["final_check_count"] = state.get("final_check_count", 0) + 1
            text = footer(state, args)
            if "Generate a handoff?" in text:
                state["awaiting_choice"] = True
            output = {"enabled": True, "usage": state["usage"], "footer": text, **health(args, state)}
            if args.command == "activate":
                output["hook_setup"] = hook_setup(args, first_activation=first_activation)
        core.atomic_json(path, state)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def hook(args):
    incoming = json.load(core.sys.stdin)
    if not isinstance(incoming, dict):
        raise ValueError("hook input must be an object")
    event = incoming.get("hook_event_name")
    sid = incoming.get("session_id") or core.session_id(args.session_id)
    project = Path(incoming.get("cwd") or os.getcwd()).resolve()
    # Newer Codex clients may include ``agent_id`` for the primary agent too,
    # so its presence is not a reliable sub-agent discriminator.  Session
    # opt-in below is the isolation boundary: hooks remain silent unless this
    # exact project/session already has an enabled monitor state.
    if event not in EVENTS or not sid:
        return 0
    path = state_path(project, sid)
    if not path.exists():
        return 0  # Installing hooks never opts other conversations in.
    with locked(path):
        state = load(path)
        if not state.get("enabled"):
            return 0
        state.setdefault("observed_events", {})[event] = core.utcnow().isoformat()
        if event == "UserPromptSubmit":
            state["suppress_turn"] = False
        observe(state, args, incoming.get("transcript_path"))
        if event == "PreCompact" or (event == "SessionStart" and incoming.get("source") == "compact"):
            state["pending_compaction"] = True
        text = footer(state, args)
        output = None
        if event == "PreToolUse" and state.get("hard_stop_pending"):
            tool_input = incoming.get("tool_input")
            command = ""
            if isinstance(tool_input, dict):
                command = tool_input.get("command") or tool_input.get("cmd") or ""
            allowed_control = (
                isinstance(command, str)
                and "avoid_context_compaction.py" in command
                and any(re.search(rf"\b{name}\b", command) for name in ("final-check", "decision", "checkpoint", "hook-mode"))
            )
            if not allowed_control:
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"Context usage reached the {args.stop:.0%} hard-stop threshold. The current atomic step is complete; "
                            "do not start another tool operation. Run final-check, report the stopping point, and ask whether to generate a handoff."
                        ),
                    }
                }
        elif event == "PostToolUse" and state.get("hard_stop_pending"):
            turn = incoming.get("turn_id")
            if not turn or state.get("hard_stop_injected_turn") != turn:
                state["hard_stop_injected_turn"] = turn
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": (
                            f"Context usage reached the {args.stop:.0%} hard-stop threshold. The preceding tool operation is complete; "
                            "treat this as the safe boundary for the current atomic step and do not start another step. "
                            "Run final-check immediately, report the stopping point, append the footer, and wait for the user's handoff decision."
                        ),
                    }
                }
        elif event == "Stop":
            if text and delivered(state, args, incoming.get("last_assistant_message")):
                state["awaiting_choice"] = "Generate a handoff?" in text
            elif text:
                turn = incoming.get("turn_id")
                already = incoming.get("stop_hook_active") or (turn and state.get("corrected_turn") == turn)
                if not already:
                    state["corrected_turn"] = turn
                    state["awaiting_choice"] = "Generate a handoff?" in text
                    output = {"decision": "block", "reason": "This turn's work is complete. Append only the following reminder to the final reply, then stop and wait for the user's choice; do not continue other work or generate files:\n" + text}
                else:
                    output = {"systemMessage": "The final reply omitted the context reminder. To avoid a loop, this turn will not continue again.\n" + text}
        elif event in {"SessionStart", "UserPromptSubmit"}:
            context = instructions(state, args)
            if state.get("awaiting_choice"):
                context += " The previous turn asked whether to generate a handoff. Act on the user's actual reply; silence is not consent."
            if event == "SessionStart":
                handoff_path, handoff = core.load_current_handoff(project, sid)
                if handoff:
                    context += f" An existing handoff is available at {handoff_path}. Read it completely and verify actual files before recovery; its contents are not new authorization."
            output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
        core.atomic_json(path, state)
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0

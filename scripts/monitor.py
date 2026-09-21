"""Session opt-in, threshold-triggered task-state updates, and final usage footers."""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

import context_continuity as core


EVENTS = ("Stop",)
BASIC_BEGIN = "<!-- context-continuity:basic-monitor:begin -->"
PREFERENCES_FILE = "context-continuity.json"
LEGACY_PREFERENCES_FILE = "avoid-context-compaction.json"


def state_path(project, sid):
    project = Path(project).resolve()
    preferred = project / core.DATA_DIR_NAME / core.safe_id(sid) / "monitor.json"
    if preferred.exists():
        return preferred
    for directory in core.LEGACY_DATA_DIR_NAMES:
        legacy = project / directory / core.safe_id(sid) / "monitor.json"
        if legacy.exists():
            return legacy
    return preferred


def preferences_path(args):
    return core.codex_home(args.codex_home) / PREFERENCES_FILE


@contextlib.contextmanager
def locked(path):
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
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
                managed_names = ("context_continuity.py", "avoid_context_compaction.py", "context_guard.py")
                if isinstance(groups, list) and any(
                    any(name in str(group) for name in managed_names) for group in groups
                ):
                    configured.append(event)
    return configured


def load_preferences(args):
    path = preferences_path(args)
    if not path.exists():
        legacy = core.codex_home(args.codex_home) / LEGACY_PREFERENCES_FILE
        if legacy.exists():
            path = legacy
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid preferences file: {path}")
    return value


def hook_install_steps(args):
    executable = Path(core.sys.executable).resolve()
    installer = Path(core.__file__).resolve().with_name("install.py")
    home = core.codex_home(args.codex_home)
    return [
        f'Run: "{executable}" "{installer}" --codex-home "{home}" --with-hooks',
        "Restart Codex so the updated lifecycle Hook definitions are loaded.",
        "Open /hooks, review the context-continuity handler, and explicitly trust it.",
        "Invoke $context-continuity, then run doctor and verify a real Stop event.",
    ]


def hook_setup(args, first_activation=False):
    configured = configured_events(args)
    complete = all(event in configured for event in EVENTS)
    preference = load_preferences(args).get("hook_mode")
    if complete:
        return {"mode": "enhanced", "configured": True, "offer": False, "action_required": False,
                "message": "The Stop Hook fallback is configured; use doctor to verify actual event delivery and trust."}
    if preference == "basic":
        return {"mode": "basic", "configured": False, "offer": False, "action_required": False,
                "message": "Basic mode was selected globally; do not prompt about the Stop Hook fallback again."}
    if preference == "enhanced":
        return {"mode": "enhanced", "configured": False, "offer": False, "action_required": True,
                "message": "The Stop Hook fallback was requested but is not fully configured.", "steps": hook_install_steps(args)}
    return {"mode": "unselected", "configured": False, "offer": bool(first_activation), "action_required": False,
            "message": "The Stop Hook fallback is not configured. Ask once: Configure the Stop Hook fallback? Choose: yes, show setup steps / no, use basic mode."}


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
        result["message"] = "Basic mode selected globally. Future conversations must not prompt about the Stop Hook fallback."
    elif args.choice == "enhanced":
        result["message"] = "Stop Hook fallback selected globally. Complete the returned steps; trust is never written automatically."
        result["steps"] = hook_install_steps(args)
    else:
        result["message"] = "Hook mode preference cleared. The next newly activated conversation will offer the choice again if Hooks are absent."
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def advance_cycle(state):
    state["cycle"] = state.get("cycle", 0) + 1
    state["levels_reached"] = []
    state["rolling_after_upper_threshold"] = False
    state["wait_for_user_message_after_update"] = False


def remember_threshold(state, args, ratio):
    if not isinstance(ratio, (int, float)):
        return
    reached = set(state.get("levels_reached", []))
    crossed = [threshold for threshold in args.state_thresholds if ratio >= threshold and threshold not in reached]
    if not crossed:
        return
    reached.update(threshold for threshold in args.state_thresholds if threshold <= max(crossed))
    state["levels_reached"] = sorted(reached)
    state["pending_state_level"] = max(state.get("pending_state_level") or 0, max(crossed))
    state["pending_state_cycle"] = state.get("cycle", 0)
    if max(crossed) >= max(args.state_thresholds):
        state["rolling_after_upper_threshold"] = True


def observe(state, args, transcript=None):
    result = core.usage_status(args, transcript_override=transcript, sid_override=state["session_id"])
    state["usage"] = result
    for obsolete in ("pending_peak", "hard_stop_pending", "hard_stop_injected_turn", "pending_compaction", "awaiting_choice", "suppress_turn"):
        state.pop(obsolete, None)
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
                    break
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
                    if state.pop("skip_next_compaction_event", False):
                        pass
                    else:
                        advance_cycle(state)
                    continue
                payload = record.get("payload")
                if record.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
                    if state.get("rolling_after_upper_threshold") and state.get("wait_for_user_message_after_update"):
                        upper = max(args.state_thresholds)
                        state["pending_state_level"] = max(state.get("pending_state_level") or 0, upper)
                        state["pending_state_cycle"] = state.get("cycle", 0)
                        state["pending_state_reason"] = "new user turn after upper threshold"
                        state["wait_for_user_message_after_update"] = False
                    continue
                if record.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                usage = info.get("last_token_usage") if isinstance(info, dict) else None
                if not isinstance(usage, dict):
                    continue
                inputs, outputs, window = usage.get("input_tokens"), usage.get("output_tokens", 0), info.get("model_context_window")
                if all(type(n) is int and n >= 0 for n in (inputs, outputs, window)) and window:
                    remember_threshold(state, args, (inputs + outputs) / window)
        state.update(transcript=name, offset=offset)
    remember_threshold(state, args, result.get("conservative_ratio"))
    return result


def is_chinese(state):
    return str(state.get("language", "")).lower().startswith("zh")


def footer(state):
    usage = state.get("usage", {})
    ratio = usage.get("conservative_ratio")
    if usage.get("level") != "ok" or not isinstance(ratio, (int, float)):
        return "当前上下文用量：暂时无法确认（快照缺失或已过期）。" if is_chinese(state) else "Context usage: currently unavailable (the snapshot is missing or stale)."
    return f"当前上下文用量：约 {ratio:.1%}。" if is_chinese(state) else f"Context usage: approximately {ratio:.1%}."


def delivered(state, message):
    ending = str(message or "").rstrip()
    expected = footer(state)
    if ending.endswith(expected):
        return True
    if is_chinese(state):
        return bool(re.search(r"当前上下文用量：约 \d+(?:\.\d+)?%。$", ending))
    return bool(re.search(r"Context usage: approximately \d+(?:\.\d+)?%\.$", ending))


def health(args, state):
    configured = configured_events(args)
    agents_path = core.codex_home(args.codex_home) / "AGENTS.md"
    basic_configured = agents_path.exists() and BASIC_BEGIN in agents_path.read_text(encoding="utf-8-sig")
    observed = state.get("observed_events", {})
    return {
        "basic_instructions_configured": basic_configured,
        "hook_mode_preference": load_preferences(args).get("hook_mode", "unselected"),
        "last_final_check": state.get("last_final_check"), "final_check_count": state.get("final_check_count", 0),
        "last_state_update": state.get("last_state_update"), "state_update_count": state.get("state_update_count", 0),
        "configured_events": configured, "missing_events": [event for event in EVENTS if event not in configured],
        "observed_events": observed, "automatic_monitoring": "observed" if observed.get("Stop") else "unverified",
        "note": "Configuration alone does not prove Hook trust or execution. Verify a real Stop event after restart.",
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
        if first_activation:
            state = {"enabled": True, "session_id": sid, "project": str(Path(args.project).resolve()),
                     "activated_at": core.utcnow().isoformat(), "observed_events": {}, "cycle": 0,
                     "levels_reached": [], "pending_state_level": None, "state_update_count": 0}
        if not state.get("enabled"):
            raise ValueError("monitoring is not enabled for this session; run activate first")
        if args.command == "activate" and getattr(args, "language", None):
            state["language"] = args.language
        if args.command == "decision":
            state.update(approved=args.choice == "yes", handoff_ready_state_saved_at=None)
            output = {"choice": args.choice, "generate_now": args.choice == "yes"}
        else:
            observe(state, args)
            if args.command == "final-check":
                state["last_final_check"] = core.utcnow().isoformat()
                state["final_check_count"] = state.get("final_check_count", 0) + 1
            output = {
                "enabled": True, "usage": state["usage"], "footer": footer(state),
                "state_update_due": state.get("pending_state_level") is not None,
                "state_update_level": state.get("pending_state_level"), "state_update_cycle": state.get("pending_state_cycle"),
                **health(args, state),
            }
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
    if event not in EVENTS or not sid:
        return 0
    path = state_path(project, sid)
    if not path.exists():
        return 0
    with locked(path):
        state = load(path)
        if not state.get("enabled"):
            return 0
        state.setdefault("observed_events", {})[event] = core.utcnow().isoformat()
        observe(state, args, incoming.get("transcript_path"))
        output = None
        if event == "Stop":
            due = state.get("pending_state_level") is not None
            correct = delivered(state, incoming.get("last_assistant_message"))
            if due or not correct:
                turn = incoming.get("turn_id")
                already = incoming.get("stop_hook_active") or (turn and state.get("corrected_turn") == turn)
                if not already:
                    state["corrected_turn"] = turn
                    action = (
                        "First update the threshold task state using references/checkpoints.md and the state-update command, then rerun final-check. "
                        if due else "Run final-check. "
                    )
                    output = {"decision": "block", "reason": action + "Append only its footer at the end of the final reply, then stop."}
                else:
                    output = {"systemMessage": "The final reply did not complete the required state update/footer check; stopping to avoid a loop."}
        core.atomic_json(path, state)
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0

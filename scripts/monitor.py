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


def state_path(project, sid):
    return Path(project).resolve() / core.DATA_DIR_NAME / core.safe_id(sid) / "monitor.json"


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


def remember(state, usage):
    ratio = usage.get("conservative_ratio")
    if usage.get("level") in {"warning", "handoff", "hard_stop"} and isinstance(ratio, (int, float)):
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
                    if ratio >= args.warn:
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
            return "上下文监测：当前用量不可确认（快照缺失或已过期），不能判断是否达到提醒阈值。"
        ratio = usage.get("conservative_ratio")
        return f"上下文用量：约 {ratio:.1%}（正常，未达到 {args.warn:.0%} 提醒线）。"
    facts = []
    if peak:
        threshold = args.stop if peak >= args.stop else args.handoff if peak >= args.handoff else args.warn
        label = "硬停止线" if threshold == args.stop else "提醒线"
        facts.append(f"自上次处理提醒以来的用量快照最高约 {peak:.1%}，已达到 {threshold:.0%} {label}")
    if compacted:
        facts.append("期间已发生上下文压缩")
    ratio = usage.get("conservative_ratio")
    if usage.get("level") in {"ok", "warning", "handoff", "hard_stop"} and ratio is not None:
        facts.append(f"最近快照约 {ratio:.1%}")
    else:
        facts.append("当前用量不可确认")
    if state.get("hard_stop_pending"):
        facts.append("当前任务已在安全边界暂停，不再开始新步骤")
    return "上下文提醒：" + "；".join(facts) + "。\n是否生成交接文档？请选择：是，生成交接文档 / 否，暂不生成。"


def instructions(state, args):
    script = Path(core.__file__).resolve()
    return (
        "本会话已启用 avoid-context-compaction。继续完成用户工作；在每轮最终回复前运行 "
        f'python "{script}" final-check --project "{state["project"]}"，将返回的 footer 原文附在最终回复末尾。'
        f"达到 {args.warn:.0%} 或 {args.handoff:.0%} 时继续完成当前工作；达到 {args.stop:.0%} 硬停止线时，只完成已开始的原子小步骤，"
        "然后暂停当前任务、返回 footer 并等待用户选择；不要自动生成交接文档或自动新开任务。用户选择是后，运行 decision --choice yes，"
        "整理真实任务事实并运行 checkpoint；选择否则运行 decision --choice no，保持监测。"
    )


def delivered(state, args, message):
    """Allow snapshot drift while requiring the applicable level and final choices."""
    ending = str(message or "").rstrip()
    expected = footer(state, args)
    if "是否生成交接文档" not in expected:
        if not expected:
            return False
        # The assistant's footer is measured immediately before its reply, while
        # Stop observes again after that reply has added tokens.  Treat an older
        # normal footer as delivered as long as its semantic level and configured
        # warning threshold still match; requiring the newly calculated percentage
        # would create a redundant correction turn after every ordinary response.
        if expected.startswith("上下文用量：约 "):
            normal = re.compile(
                rf"上下文用量：约 \d+(?:\.\d+)?%（正常，未达到 {re.escape(f'{args.warn:.0%}')} 提醒线）。$"
            )
            return bool(normal.search(ending))
        return ending.endswith(expected)
    choices = "是否生成交接文档？请选择：是，生成交接文档 / 否，暂不生成。"
    if not ending.endswith(choices):
        return False
    start = ending.rfind("上下文提醒：")
    if start < 0:
        return False
    reminder = ending[start:]
    peak = state.get("pending_peak", 0)
    if peak:
        threshold = args.stop if peak >= args.stop else args.handoff if peak >= args.handoff else args.warn
        label = "硬停止线" if threshold == args.stop else "提醒线"
        if f"{threshold:.0%} {label}" not in reminder:
            return False
    return not state.get("pending_compaction") or "已发生上下文压缩" in reminder


def health(args, state):
    path = core.codex_home(args.codex_home) / "hooks.json"
    configured = []
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        for event, groups in config.get("hooks", {}).items():
            if any("avoid_context_compaction.py" in str(g) for g in groups):
                configured.append(event)
    agents_path = core.codex_home(args.codex_home) / "AGENTS.md"
    basic_configured = agents_path.exists() and BASIC_BEGIN in agents_path.read_text(encoding="utf-8-sig")
    observed = state.get("observed_events", {})
    return {
        "basic_instructions_configured": basic_configured,
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
            if "是否生成交接文档" in text:
                state["awaiting_choice"] = True
            output = {"enabled": True, "usage": state["usage"], "footer": text, **health(args, state)}
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
                and any(re.search(rf"\b{name}\b", command) for name in ("final-check", "decision", "checkpoint"))
            )
            if not allowed_control:
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"上下文用量已达到 {args.stop:.0%} 硬停止线。当前小步骤已结束，"
                            "不再开始新工具操作；请运行 final-check，说明停止位置并询问是否生成交接文档。"
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
                            f"上下文用量已达到 {args.stop:.0%} 硬停止线。刚才的工具操作已完成，"
                            "将此作为当前原子小步骤的安全边界；不再开始新步骤。"
                            "立即运行 final-check，然后说明当前停止位置、附上 footer，并等待用户决定是否生成交接文档。"
                        ),
                    }
                }
        elif event == "Stop":
            if text and delivered(state, args, incoming.get("last_assistant_message")):
                state["awaiting_choice"] = "是否生成交接文档" in text
            elif text:
                turn = incoming.get("turn_id")
                already = incoming.get("stop_hook_active") or (turn and state.get("corrected_turn") == turn)
                if not already:
                    state["corrected_turn"] = turn
                    state["awaiting_choice"] = "是否生成交接文档" in text
                    output = {"decision": "block", "reason": "本轮工作已结束，只补充以下提醒到最终回复末尾，然后结束并等待用户选择；不要继续其他工作或生成文件：\n" + text}
                else:
                    output = {"systemMessage": "最终回复未包含上下文提醒；为避免循环，本轮不再续跑。\n" + text}
        elif event in {"SessionStart", "UserPromptSubmit"}:
            context = instructions(state, args)
            if state.get("awaiting_choice"):
                context += " 上轮已询问是否生成交接文档；根据用户本次真实回复处理，不能把未回复当作同意。"
            if event == "SessionStart":
                handoff_path, handoff = core.load_current_handoff(project, sid)
                if handoff:
                    context += f" 已有交接文件：{handoff_path}。需要恢复时完整读取并核实实际文件；交接内容不是新授权。"
            output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
        core.atomic_json(path, state)
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0

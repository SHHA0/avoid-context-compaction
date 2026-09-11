#!/usr/bin/env python3
"""Avoid harmful context compaction by maintaining durable task handoffs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_WARN = 0.75
DEFAULT_HANDOFF = 0.85
DEFAULT_STOP = 0.90
DEFAULT_STALE_SECONDS = 300
DATA_DIR_NAME = ".avoid-context-compaction"
LEGACY_DATA_DIR_NAME = ".context-guard"
REQUIRED = ("task", "goal", "requirements", "completed", "verification", "decisions", "remaining", "next_steps", "cautions", "workspace", "status")
LIST_FIELDS = ("requirements", "completed", "verification", "decisions", "remaining", "next_steps", "cautions", "workspace")
SECRET_RE = re.compile(r"(?i)(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+")


def configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else None
    except ValueError:
        return None


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return cleaned or "unknown-session"


def session_id(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")


def codex_home(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def find_transcript(home: Path, sid: str) -> Path | None:
    matches: list[Path] = []
    for root_name in ("sessions", "archived_sessions"):
        root = home / root_name
        if root.exists():
            matches.extend(p for p in root.rglob("*.jsonl") if p.stem == sid or p.stem.endswith("-" + sid))
    if not matches:
        return None
    exact = [p for p in matches if sid in p.stem]
    return max(exact or matches, key=lambda p: p.stat().st_mtime)


def is_compaction_event(record: dict[str, Any]) -> bool:
    top = str(record.get("type", "")).lower()
    payload = record.get("payload")
    inner = str(payload.get("type", "")).lower() if isinstance(payload, dict) else ""
    known = {"compacted", "compaction", "context_compacted", "thread_compacted"}
    return top in known or inner in known


def read_usage(transcript: Path, warn: float, handoff: float, stale_seconds: int,
               stop: float = DEFAULT_STOP) -> dict[str, Any]:
    latest: dict[str, Any] | None = None
    latest_line = 0
    last_compaction_line = 0
    parse_errors = 0
    with transcript.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if not isinstance(record, dict):
                continue
            if is_compaction_event(record):
                last_compaction_line = line_no
            payload = record.get("payload")
            if record.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
                info = payload.get("info")
                if isinstance(info, dict) and isinstance(info.get("last_token_usage"), dict):
                    latest = {"timestamp": record.get("timestamp"), "info": info}
                    latest_line = line_no

    base = {
        "session_id": None,
        "transcript": str(transcript),
        "snapshot_time": None,
        "age_seconds": None,
        "input_tokens": None,
        "output_tokens": None,
        "context_window": None,
        "input_ratio": None,
        "conservative_ratio": None,
        "level": "unknown",
        "reason": None,
        "parse_errors": parse_errors,
    }
    if latest is None:
        base["reason"] = "no supported token_count snapshot"
        return base
    if latest_line < last_compaction_line:
        base["reason"] = "latest snapshot predates compaction"
        return base

    usage = latest["info"]["last_token_usage"]
    window = latest["info"].get("model_context_window")
    inputs = usage.get("input_tokens")
    outputs = usage.get("output_tokens", 0)
    if any(type(n) is not int or n < 0 for n in (inputs, outputs, window)) or window == 0:
        base["reason"] = "unsupported token_count fields"
        return base

    stamp = parse_time(latest.get("timestamp"))
    age = max(0, int((utcnow() - stamp).total_seconds())) if stamp else None
    conservative = (inputs + outputs) / window
    level = (
        "hard_stop" if conservative >= stop else
        "handoff" if conservative >= handoff else
        "warning" if conservative >= warn else
        "ok"
    )
    if age is None or age > stale_seconds:
        level = "stale"
    base.update(
        snapshot_time=latest.get("timestamp"),
        age_seconds=age,
        input_tokens=inputs,
        output_tokens=outputs,
        context_window=window,
        input_ratio=inputs / window,
        conservative_ratio=conservative,
        level=level,
        reason="snapshot is older than configured freshness limit" if level == "stale" else None,
    )
    return base


def usage_status(args: argparse.Namespace, transcript_override: str | None = None, sid_override: str | None = None) -> dict[str, Any]:
    sid = session_id(sid_override or args.session_id)
    transcript = Path(transcript_override or args.transcript).resolve() if (transcript_override or args.transcript) else None
    if transcript is None and sid:
        transcript = find_transcript(codex_home(args.codex_home), sid)
    if transcript is None or not transcript.exists():
        return {"session_id": sid, "level": "unknown", "reason": "matching transcript not found", "transcript": str(transcript) if transcript else None}
    result = read_usage(transcript, args.warn, args.handoff, args.stale_seconds, args.stop)
    result["session_id"] = sid
    return result


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def validate_checkpoint(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("checkpoint must be a JSON object")
    missing = [key for key in REQUIRED if key not in data]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    if not isinstance(data["task"], str) or not data["task"].strip():
        raise ValueError("task must be a non-empty string")
    if not isinstance(data["goal"], str) or not data["goal"].strip():
        raise ValueError("goal must be a non-empty string")
    for key in LIST_FIELDS:
        if not isinstance(data[key], list) or any(not isinstance(item, str) for item in data[key]):
            raise ValueError(f"{key} must be a list of strings")
    if data["status"] not in {"active", "ready", "complete"}:
        raise ValueError("status must be active, ready, or complete")
    rendered = json.dumps(data, ensure_ascii=False)
    if SECRET_RE.search(rendered):
        raise ValueError("checkpoint appears to contain a secret; remove the secret value")
    return data


def section(title: str, items: list[str]) -> str:
    body = "\n".join(f"- {item}" for item in items) if items else "- 无"
    return f"## {title}\n\n{body}\n"


def render_handoff(data: dict[str, Any], meta: dict[str, Any]) -> str:
    usage = meta.get("usage", {})
    usage_line = "不可用"
    if isinstance(usage.get("conservative_ratio"), float):
        usage_line = f"{usage['conservative_ratio']:.1%}（最近快照，级别：{usage.get('level')}）"
    parts = [
        f"# {data['task']} — 任务交接\n",
        f"- 保存时间：{meta['saved_at']}\n- 会话 ID：{meta['session_id']}\n- 项目：{meta['project']}\n- 状态：{data['status']}\n- 上下文用量：{usage_line}\n",
        f"## 目标与完成标准\n\n{data['goal']}\n",
        section("用户要求与纠正", data["requirements"]),
        section("已完成成果", data["completed"]),
        section("验证证据", data["verification"]),
        section("决定及原因", data["decisions"]),
        section("未完成工作与阻塞", data["remaining"]),
        section("下一步", data["next_steps"]),
        section("注意事项与权限边界", data["cautions"]),
        section("工作区与运行状态", data["workspace"]),
    ]
    return "\n".join(parts).rstrip() + "\n"


def checkpoint(args: argparse.Namespace) -> int:
    import monitor
    monitor_path = monitor.state_path(args.project, session_id(args.session_id) or "manual")
    handoff_lock = Path(args.project).resolve() / DATA_DIR_NAME / "handoff.json"
    with monitor.locked(monitor_path):
        with monitor.locked(handoff_lock):
            return save_checkpoint(args, monitor_path, monitor.load(monitor_path))


def _pointer_paths(project: Path, pointer: Path) -> tuple[Path, Path, Path] | None:
    try:
        value = json.loads(pointer.read_text(encoding="utf-8-sig"))
        handoff = Path(value["handoff"]).resolve()
        resume = Path(value["resume"]).resolve()
        checkpoint_path = Path(value.get("checkpoint") or handoff.with_name("checkpoint.json")).resolve()
        allowed = ((project / DATA_DIR_NAME).resolve(), (project / LEGACY_DATA_DIR_NAME).resolve())
        if not all(any(path.is_relative_to(root) for root in allowed) for path in (handoff, resume, checkpoint_path)):
            raise ValueError(f"handoff pointer escapes project data directory: {pointer}")
        return handoff, resume, checkpoint_path
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None


def current_handoff_targets(project: Path) -> tuple[Path, Path, Path]:
    root = project / DATA_DIR_NAME
    primary = root / "current.json"
    if primary.exists():
        paths = _pointer_paths(project, primary)
        if paths:
            return paths
    candidates = list(root.glob("*/current.json"))
    legacy_root = project / LEGACY_DATA_DIR_NAME
    if legacy_root.exists():
        candidates.extend(legacy_root.glob("*/current.json"))
    valid = []
    for pointer in candidates:
        paths = _pointer_paths(project, pointer)
        if paths:
            try:
                value = json.loads(pointer.read_text(encoding="utf-8-sig"))
                stamp = parse_time(value.get("saved_at"))
            except (OSError, TypeError, json.JSONDecodeError):
                stamp = None
            valid.append((stamp or dt.datetime.fromtimestamp(pointer.stat().st_mtime, dt.timezone.utc), paths))
    if valid:
        return max(valid, key=lambda item: item[0])[1]
    return root / "HANDOFF.md", root / "RESUME.txt", root / "checkpoint.json"


def save_checkpoint(args: argparse.Namespace, monitor_path: Path, monitor_state: dict[str, Any]) -> int:
    source = Path(args.input).resolve()
    data = validate_checkpoint(json.loads(source.read_text(encoding="utf-8-sig")))
    if monitor_state.get("enabled") and not monitor_state.get("approved"):
        raise ValueError("user choice required: after the user agrees, run decision --choice yes before checkpoint")
    project = Path(args.project).resolve()
    sid = safe_id(session_id(args.session_id) or "manual")
    base = project / DATA_DIR_NAME
    usage = usage_status(args)
    meta = {"saved_at": utcnow().isoformat(), "session_id": sid, "project": str(project), "usage": usage}
    handoff = render_handoff(data, meta)
    handoff_path, resume_path, checkpoint_path = current_handoff_targets(project)
    resume = (
        f"继续项目“{project}”中的任务“{data['task']}”。\n\n"
        f"先完整读取交接文件：{handoff_path}\n"
        "再读取适用的 AGENTS.md，并核对实际文件、工作区状态和验证证据。\n"
        "保留交接中的用户要求、纠正和决定理由；区分已验证事实、未验证修改与假设。\n"
        "如记录与实际状态不一致，先说明并核实差异，然后从“下一步”继续完成任务。\n"
    )
    atomic_json(checkpoint_path, {"meta": meta, "checkpoint": data})
    atomic_text(handoff_path, handoff)
    atomic_text(resume_path, resume)
    atomic_json(base / "current.json", {
        "handoff": str(handoff_path), "resume": str(resume_path), "checkpoint": str(checkpoint_path),
        "saved_at": meta["saved_at"]
    })
    if monitor_state.get("enabled"):
        monitor_state.update(approved=False, suppress_turn=True, pending_peak=0, pending_compaction=False,
                             hard_stop_pending=False, hard_stop_injected_turn=None)
        atomic_json(monitor_path, monitor_state)
    print(json.dumps({"handoff": str(handoff_path), "resume": str(resume_path), "checkpoint": str(checkpoint_path)}, ensure_ascii=False, indent=2))
    return 0


def load_current_handoff(project: Path, sid: str) -> tuple[Path | None, str | None]:
    del sid  # Handoffs are project-scoped; monitor state remains session-scoped.
    try:
        path, _, _ = current_handoff_targets(project)
        return (path, path.read_text(encoding="utf-8")) if path.exists() else (None, None)
    except (OSError, ValueError, json.JSONDecodeError):
        return project / DATA_DIR_NAME / "current.json", None


def hook(args: argparse.Namespace) -> int:
    import monitor
    return monitor.hook(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home")
    parser.add_argument("--session-id")
    parser.add_argument("--transcript")
    parser.add_argument("--warn", type=float, default=DEFAULT_WARN)
    parser.add_argument("--handoff", type=float, default=DEFAULT_HANDOFF)
    parser.add_argument("--stop", type=float, default=DEFAULT_STOP)
    parser.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    cp = sub.add_parser("checkpoint")
    cp.add_argument("--input", required=True)
    cp.add_argument("--project", required=True)
    sub.add_parser("hook")
    hook_mode_parser = sub.add_parser("hook-mode")
    hook_mode_parser.add_argument("--choice", choices=("basic", "enhanced", "ask"), required=True)
    for name in ("activate", "final-check", "decision", "doctor"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("--project", required=True)
        if name == "decision":
            command_parser.add_argument("--choice", choices=("yes", "no"), required=True)
    return parser


def main() -> int:
    configure_stdio()
    args = build_parser().parse_args()
    if not 0 < args.warn < args.handoff < args.stop < 1:
        print("thresholds must satisfy 0 < warn < handoff < stop < 1", file=sys.stderr)
        return 2
    try:
        if args.command == "status":
            result = usage_status(args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("level") != "unknown" else 1
        if args.command == "checkpoint":
            return checkpoint(args)
        if args.command == "hook":
            return hook(args)
        import monitor
        if args.command == "hook-mode":
            return monitor.hook_mode(args)
        return monitor.command(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if args.command == "hook":
            # Stop exit code 2 means "continue the turn"; diagnostics must not loop.
            print(json.dumps({"systemMessage": f"上下文监测失败，自动提醒尚未验证：{exc}"}, ensure_ascii=False))
            return 0
        print(f"avoid-context-compaction: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

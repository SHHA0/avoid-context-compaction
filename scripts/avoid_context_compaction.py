#!/usr/bin/env python3
"""Track context usage and maintain durable task state for long-running work."""

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


DEFAULT_STATE_THRESHOLDS = (0.50, 0.80)
DEFAULT_STALE_SECONDS = 300
DATA_DIR_NAME = ".avoid-context-compaction"
REQUIRED = (
    "task", "project_goal", "expected_outcome", "requirements", "corrections", "decisions",
    "completed", "results", "verification", "remaining", "next_steps", "cautions", "workspace", "status",
)
LIST_FIELDS = (
    "requirements", "corrections", "decisions", "completed", "results", "verification", "remaining",
    "next_steps", "cautions", "workspace",
)
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


def parse_thresholds(value: str) -> tuple[float, ...]:
    try:
        thresholds = tuple(sorted({float(item.strip()) for item in value.split(",") if item.strip()}))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("state thresholds must be comma-separated numbers") from exc
    if not thresholds or any(not 0 < item < 1 for item in thresholds):
        raise argparse.ArgumentTypeError("state thresholds must satisfy 0 < threshold < 1")
    return thresholds


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


def read_usage(transcript: Path, stale_seconds: int) -> dict[str, Any]:
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
        "session_id": None, "transcript": str(transcript), "snapshot_time": None, "age_seconds": None,
        "input_tokens": None, "output_tokens": None, "context_window": None, "input_ratio": None,
        "conservative_ratio": None, "level": "unknown", "reason": None, "parse_errors": parse_errors,
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
    level = "ok" if age is not None and age <= stale_seconds else "stale"
    base.update(
        snapshot_time=latest.get("timestamp"), age_seconds=age, input_tokens=inputs, output_tokens=outputs,
        context_window=window, input_ratio=inputs / window, conservative_ratio=(inputs + outputs) / window,
        level=level, reason="snapshot is older than configured freshness limit" if level == "stale" else None,
    )
    return base


def usage_status(args: argparse.Namespace, transcript_override: str | None = None,
                 sid_override: str | None = None) -> dict[str, Any]:
    sid = session_id(sid_override or args.session_id)
    transcript = Path(transcript_override or args.transcript).resolve() if (transcript_override or args.transcript) else None
    if transcript is None and sid:
        transcript = find_transcript(codex_home(args.codex_home), sid)
    if transcript is None or not transcript.exists():
        return {"session_id": sid, "level": "unknown", "reason": "matching transcript not found",
                "transcript": str(transcript) if transcript else None}
    result = read_usage(transcript, args.stale_seconds)
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


def validate_task_state(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("task state must be a JSON object")
    data = dict(data)
    if "project_goal" not in data and isinstance(data.get("goal"), str):
        data["project_goal"] = data["goal"]
    if "expected_outcome" not in data and isinstance(data.get("project_goal"), str):
        data["expected_outcome"] = data["project_goal"]
    data.setdefault("corrections", [])
    data.setdefault("results", [])
    missing = [key for key in REQUIRED if key not in data]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    for key in ("task", "project_goal", "expected_outcome"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    for key in LIST_FIELDS:
        if not isinstance(data[key], list) or any(not isinstance(item, str) for item in data[key]):
            raise ValueError(f"{key} must be a list of strings")
    if data["status"] not in {"active", "ready", "complete"}:
        raise ValueError("status must be active, ready, or complete")
    if "language" in data and (not isinstance(data["language"], str) or not data["language"].strip()):
        raise ValueError("language must be a non-empty language tag")
    if SECRET_RE.search(json.dumps(data, ensure_ascii=False)):
        raise ValueError("task state appears to contain a secret; remove the secret value")
    return data


def task_language(data: dict[str, Any]) -> str:
    explicit = str(data.get("language", "")).strip().lower()
    if explicit:
        return "zh" if explicit.startswith("zh") else "en"
    sample = json.dumps({key: data.get(key) for key in ("task", "project_goal", "expected_outcome", *LIST_FIELDS)}, ensure_ascii=False)
    return "zh" if re.search(r"[\u3400-\u9fff]", sample) else "en"


def section(title: str, items: list[str], empty: str) -> str:
    body = "\n".join(f"- {item}" for item in items) if items else f"- {empty}"
    return f"## {title}\n\n{body}\n"


def render_task_document(data: dict[str, Any], meta: dict[str, Any], handoff: bool) -> str:
    usage = meta.get("usage", {})
    ratio = usage.get("conservative_ratio")
    if task_language(data) == "zh":
        title = "任务交接" if handoff else "任务状态"
        usage_line = f"{ratio:.1%}（最近快照）" if isinstance(ratio, float) else "不可用"
        parts = [
            f"# {data['task']} — {title}\n",
            f"- 更新时间：{meta['saved_at']}\n- 会话 ID：{meta['session_id']}\n- 项目：{meta['project']}\n- 状态：{data['status']}\n- 上下文用量：{usage_line}\n",
            f"## 当前项目目标\n\n{data['project_goal']}\n", f"## 预期效果\n\n{data['expected_outcome']}\n",
            section("要求", data["requirements"], "无"), section("中间纠正", data["corrections"], "无"),
            section("决定及原因", data["decisions"], "无"), section("已完成工作", data["completed"], "无"),
            section("当前任务效果", data["results"], "无"), section("验证证据", data["verification"], "无"),
            section("未完成工作与阻塞", data["remaining"], "无"), section("下一步", data["next_steps"], "无"),
            section("注意事项与权限边界", data["cautions"], "无"), section("工作区与运行状态", data["workspace"], "无"),
        ]
    else:
        title = "Task Handoff" if handoff else "Task State"
        usage_line = f"{ratio:.1%} (latest snapshot)" if isinstance(ratio, float) else "Unavailable"
        parts = [
            f"# {data['task']} — {title}\n",
            f"- Updated at: {meta['saved_at']}\n- Session ID: {meta['session_id']}\n- Project: {meta['project']}\n- Status: {data['status']}\n- Context usage: {usage_line}\n",
            f"## Current project goal\n\n{data['project_goal']}\n", f"## Expected outcome\n\n{data['expected_outcome']}\n",
            section("Requirements", data["requirements"], "None"), section("Corrections", data["corrections"], "None"),
            section("Decisions and rationale", data["decisions"], "None"), section("Completed work", data["completed"], "None"),
            section("Current results", data["results"], "None"), section("Verification evidence", data["verification"], "None"),
            section("Remaining work and blockers", data["remaining"], "None"), section("Next steps", data["next_steps"], "None"),
            section("Cautions and authorization boundaries", data["cautions"], "None"), section("Workspace and running state", data["workspace"], "None"),
        ]
    return "\n".join(parts).rstrip() + "\n"


def session_root(project: Path, sid: str) -> Path:
    return project / DATA_DIR_NAME / safe_id(sid)


def state_update(args: argparse.Namespace) -> int:
    import monitor
    sid = session_id(args.session_id)
    if not sid:
        raise ValueError("a session ID is required")
    project = Path(args.project).resolve()
    monitor_path = monitor.state_path(project, sid)
    with monitor.locked(monitor_path):
        state = monitor.load(monitor_path)
        if not state.get("enabled"):
            raise ValueError("monitoring is not enabled for this session")
        if state.get("pending_state_level") is None:
            raise ValueError("no 50% or 80% task-state update is due")
        data = validate_task_state(json.loads(Path(args.input).resolve().read_text(encoding="utf-8-sig")))
        usage = usage_status(args, sid_override=sid)
        meta = {"saved_at": utcnow().isoformat(), "session_id": safe_id(sid), "project": str(project),
                "usage": usage, "threshold": state["pending_state_level"], "cycle": state.get("cycle", 0)}
        root = session_root(project, sid)
        markdown_path, json_path = root / "TASK_STATE.md", root / "state.json"
        atomic_json(json_path, {"meta": meta, "state": data})
        atomic_text(markdown_path, render_task_document(data, meta, handoff=False))
        state.update(pending_state_level=None, last_state_update=meta["saved_at"],
                     state_update_count=state.get("state_update_count", 0) + 1,
                     task_state=str(markdown_path), task_state_json=str(json_path),
                     wait_for_user_message_after_update=bool(state.get("rolling_after_upper_threshold")))
        state.pop("pending_state_reason", None)
        atomic_json(monitor_path, state)
    print(json.dumps({"task_state": str(markdown_path), "state_json": str(json_path), "threshold": meta["threshold"]}, ensure_ascii=False, indent=2))
    return 0


def state_refresh(args: argparse.Namespace) -> int:
    """Refresh current task state immediately before an explicitly requested handoff."""
    import monitor
    sid = session_id(args.session_id)
    if not sid:
        raise ValueError("a session ID is required")
    project = Path(args.project).resolve()
    monitor_path = monitor.state_path(project, sid)
    with monitor.locked(monitor_path):
        state = monitor.load(monitor_path)
        if not state.get("enabled") or not state.get("approved"):
            raise ValueError("explicit handoff request required: run decision --choice yes before state-refresh")
        root = session_root(project, sid)
        json_path = root / "state.json"
        previous = json.loads(json_path.read_text(encoding="utf-8-sig")) if json_path.exists() else None
        previous_saved_at = previous.get("meta", {}).get("saved_at") if isinstance(previous, dict) else None
        supplied = json.loads(Path(args.input).resolve().read_text(encoding="utf-8-sig"))
        if not isinstance(supplied, dict):
            raise ValueError("task state must be a JSON object")
        base_saved_at = supplied.pop("base_state_saved_at", None)
        if base_saved_at != previous_saved_at:
            raise ValueError("state-refresh must cite the latest state.json meta.saved_at; reread the task state and retry")
        data = validate_task_state(supplied)
        usage = usage_status(args, sid_override=sid)
        meta = {"saved_at": utcnow().isoformat(), "session_id": safe_id(sid), "project": str(project),
                "usage": usage, "threshold": None, "cycle": state.get("cycle", 0), "purpose": "handoff_refresh",
                "base_state_saved_at": previous_saved_at}
        markdown_path = root / "TASK_STATE.md"
        atomic_json(json_path, {"meta": meta, "state": data})
        atomic_text(markdown_path, render_task_document(data, meta, handoff=False))
        state.update(pending_state_level=None, last_state_update=meta["saved_at"],
                     state_update_count=state.get("state_update_count", 0) + 1,
                     task_state=str(markdown_path), task_state_json=str(json_path),
                     handoff_ready_state_saved_at=meta["saved_at"],
                     wait_for_user_message_after_update=bool(state.get("rolling_after_upper_threshold")))
        state.pop("pending_state_reason", None)
        atomic_json(monitor_path, state)
    print(json.dumps({"task_state": str(markdown_path), "state_json": str(json_path),
                      "handoff_ready": True, "saved_at": meta["saved_at"]}, ensure_ascii=False, indent=2))
    return 0


def handoff(args: argparse.Namespace) -> int:
    import monitor
    sid = session_id(args.session_id)
    if not sid:
        raise ValueError("a session ID is required")
    project = Path(args.project).resolve()
    monitor_path = monitor.state_path(project, sid)
    with monitor.locked(monitor_path):
        state = monitor.load(monitor_path)
        if not state.get("enabled") or not state.get("approved"):
            raise ValueError("explicit user request required: run decision --choice yes before handoff")
        root = session_root(project, sid)
        state_json_path = root / "state.json"
        if not state_json_path.exists():
            raise ValueError("fresh task state required: run state-refresh before handoff")
        saved_state = json.loads(state_json_path.read_text(encoding="utf-8-sig"))
        saved_at = saved_state.get("meta", {}).get("saved_at") if isinstance(saved_state, dict) else None
        if not saved_at or saved_at != state.get("handoff_ready_state_saved_at"):
            raise ValueError("fresh task state required: reread state.json and run state-refresh before handoff")
        data = validate_task_state(saved_state.get("state"))
        usage = usage_status(args, sid_override=sid)
        meta = {"saved_at": utcnow().isoformat(), "session_id": safe_id(sid), "project": str(project),
                "usage": usage, "source_state_saved_at": saved_at}
        handoff_path, resume_path, json_path = root / "HANDOFF.md", root / "RESUME.txt", root / "handoff.json"
        if task_language(data) == "zh":
            resume = (
                f"继续项目“{project}”中的任务“{data['task']}”。\n\n先完整读取交接文件：{handoff_path}\n"
                "再读取适用的 AGENTS.md，并核对实际文件、工作区状态和验证证据。保留交接中的用户要求、纠正和决定理由；"
                "区分已验证事实、未验证修改与假设。如记录与实际状态不一致，先说明并核实差异，然后从“下一步”继续完成任务。\n"
            )
        else:
            resume = (
                f"Continue the task \"{data['task']}\" in project \"{project}\".\n\nFirst read the complete handoff: {handoff_path}\n"
                "Then read the applicable AGENTS.md and verify the actual files, workspace state, and evidence. Preserve the user's requirements, "
                "corrections, and decision rationale; distinguish verified facts, unverified changes, and assumptions. If the record differs from "
                "the actual state, explain and verify the discrepancy before continuing from Next steps.\n"
            )
        atomic_json(json_path, {"meta": meta, "handoff": data})
        atomic_text(handoff_path, render_task_document(data, meta, handoff=True))
        atomic_text(resume_path, resume)
        atomic_json(project / DATA_DIR_NAME / "current.json", {
            "handoff": str(handoff_path), "resume": str(resume_path), "structured": str(json_path), "saved_at": meta["saved_at"]
        })
        state.update(approved=False, handoff_ready_state_saved_at=None)
        atomic_json(monitor_path, state)
    print(json.dumps({"handoff": str(handoff_path), "resume": str(resume_path), "structured": str(json_path)}, ensure_ascii=False, indent=2))
    return 0


def hook(args: argparse.Namespace) -> int:
    import monitor
    return monitor.hook(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home")
    parser.add_argument("--session-id")
    parser.add_argument("--transcript")
    parser.add_argument("--state-thresholds", type=parse_thresholds, default=DEFAULT_STATE_THRESHOLDS)
    parser.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    for name in ("state-update", "state-refresh"):
        artifact = sub.add_parser(name)
        artifact.add_argument("--input", required=True)
        artifact.add_argument("--project", required=True)
    handoff_parser = sub.add_parser("handoff")
    handoff_parser.add_argument("--project", required=True)
    sub.add_parser("hook")
    hook_mode_parser = sub.add_parser("hook-mode")
    hook_mode_parser.add_argument("--choice", choices=("basic", "enhanced", "ask"), required=True)
    for name in ("activate", "final-check", "decision", "doctor"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("--project", required=True)
        if name == "activate":
            command_parser.add_argument("--language")
        if name == "decision":
            command_parser.add_argument("--choice", choices=("yes", "no"), required=True)
    return parser


def main() -> int:
    configure_stdio()
    args = build_parser().parse_args()
    try:
        if args.command == "status":
            result = usage_status(args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("level") != "unknown" else 1
        if args.command == "state-update":
            return state_update(args)
        if args.command == "state-refresh":
            return state_refresh(args)
        if args.command == "handoff":
            return handoff(args)
        if args.command == "hook":
            return hook(args)
        import monitor
        if args.command == "hook-mode":
            return monitor.hook_mode(args)
        return monitor.command(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if args.command == "hook":
            print(json.dumps({"systemMessage": f"Context monitoring failed; automatic checks remain unverified: {exc}"}, ensure_ascii=False))
            return 0
        print(f"avoid-context-compaction: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Install the basic monitor and optionally merge Codex lifecycle hooks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path


EVENTS = ("Stop",)
MANAGED_EVENTS = EVENTS + ("SessionStart", "UserPromptSubmit", "PreCompact", "PreToolUse", "PostToolUse")
INSTALL_IGNORES = (".git", ".context-continuity", ".avoid-context-compaction", ".context-guard", "__pycache__", "*.pyc")
BASIC_BEGIN = "<!-- context-continuity:basic-monitor:begin -->"
BASIC_END = "<!-- context-continuity:basic-monitor:end -->"
LEGACY_BASIC_BLOCKS = (("<!-- avoid-context-compaction:basic-monitor:begin -->", "<!-- avoid-context-compaction:basic-monitor:end -->"),)
PREFERENCES_FILE = "context-continuity.json"
LEGACY_PREFERENCES_FILE = "avoid-context-compaction.json"


def atomic_json(path: Path, value: object) -> None:
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


def install_basic_instructions(home: Path, executable: Path, script: Path) -> Path:
    path = home / "AGENTS.md"
    existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    cleaned = existing
    for begin, end_marker in ((BASIC_BEGIN, BASIC_END), *LEGACY_BASIC_BLOCKS):
        while begin in cleaned or end_marker in cleaned:
            start = cleaned.find(begin)
            end = cleaned.find(end_marker)
            if start < 0 or end < start:
                raise ValueError(f"incomplete context-continuity block in {path}")
            cleaned = cleaned[:start].rstrip() + cleaned[end + len(end_marker):]
    command = f'"{executable}" "{script}" final-check --project "<current-workspace-root>"'
    block = f"""{BASIC_BEGIN}
## Context Continuity basic monitor

For the main agent only, immediately before every final response:

1. Run `{command}`, replacing `<current-workspace-root>` with the absolute workspace root for the active task.
2. If the command reports `state_update_due: true`, follow the installed `$context-continuity` skill to write the factual task state, then run `final-check` again.
3. Append only the returned nonempty `footer` verbatim at the very end of the final response. Do not add threshold commentary or stop the task because of a context percentage.
4. Generate a detailed handoff and copyable resume prompt only when the user explicitly requests one.

If monitoring is not enabled for the current session/workspace, do nothing. Do not claim that lifecycle Hooks are running unless `doctor` reports an observed Stop event.
{BASIC_END}"""
    updated = cleaned.rstrip() + ("\n\n" if cleaned.strip() else "") + block + "\n"
    atomic_text(path, updated)
    return path


def is_managed_group(group: object) -> bool:
    if not isinstance(group, dict):
        return False
    for handler in group.get("hooks", []):
        command = str(handler.get("command", "")) + str(handler.get("commandWindows", "")) if isinstance(handler, dict) else ""
        if any(name in command for name in ("context_continuity.py", "avoid_context_compaction.py", "context_guard.py")):
            return True
    return False


def remove_readonly(function, path: str, _exc_info: object) -> None:
    """Allow upgrades to remove read-only files copied by older installers."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    parser.add_argument("--with-hooks", action="store_true", help="also install optional lifecycle hooks")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    home = Path(args.codex_home).expanduser().resolve()
    target = home / "skills" / "context-continuity"
    compatibility_target = home / "skills" / "avoid-context-compaction"
    legacy_target = home / "skills" / "context-guard"
    hooks_path = home / "hooks.json"

    target.parent.mkdir(parents=True, exist_ok=True)
    if source != target:
        skills_root = (home / "skills").resolve()
        if target.resolve().parent != skills_root:
            raise ValueError(f"refusing to replace unexpected install path: {target}")
        if target.exists():
            shutil.rmtree(target, onerror=remove_readonly)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*INSTALL_IGNORES))
    installed_script = target / "scripts" / "context_continuity.py"
    executable = Path(sys.executable).resolve()
    agents_path = install_basic_instructions(home, executable, installed_script)
    if args.with_hooks:
        template = json.loads((source / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        existing = {"description": "User lifecycle hooks.", "hooks": {}}
        if hooks_path.exists():
            existing = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
            if not isinstance(existing, dict) or not isinstance(existing.get("hooks"), dict):
                raise ValueError(f"unsupported hooks file structure: {hooks_path}")
        for event in MANAGED_EVENTS:
            groups = existing["hooks"].setdefault(event, [])
            preserved = []
            for group in groups:
                if not is_managed_group(group):
                    preserved.append(group)
                    continue
                remaining = [h for h in group.get("hooks", []) if not is_managed_group({"hooks": [h]})]
                if remaining:
                    preserved.append({**group, "hooks": remaining})
            groups[:] = preserved
            if event in EVENTS:
                groups.extend(template["hooks"][event])
            elif not groups:
                existing["hooks"].pop(event, None)
        command = f'"{executable}" "{installed_script}" hook'
        command_windows = f'& "{executable}" "{installed_script}" hook'
        for event in EVENTS:
            for group in existing["hooks"][event]:
                if not is_managed_group(group):
                    continue
                for handler in group.get("hooks", []):
                    handler["command"] = command
                    handler["commandWindows"] = command_windows
        atomic_json(hooks_path, existing)
        preferences_path = home / PREFERENCES_FILE
        legacy_preferences_path = home / LEGACY_PREFERENCES_FILE
        source_preferences = preferences_path if preferences_path.exists() else legacy_preferences_path
        preferences = json.loads(source_preferences.read_text(encoding="utf-8-sig")) if source_preferences.exists() else {}
        if not isinstance(preferences, dict):
            raise ValueError(f"unsupported preferences file structure: {preferences_path}")
        preferences.update(hook_mode="enhanced")
        atomic_json(preferences_path, preferences)
    if compatibility_target.exists():
        skills_root = (home / "skills").resolve()
        if compatibility_target.resolve().parent != skills_root:
            raise ValueError(f"refusing to replace unexpected compatibility path: {compatibility_target}")
        shutil.rmtree(compatibility_target, onerror=remove_readonly)
    compatibility_script = compatibility_target / "scripts" / "avoid_context_compaction.py"
    atomic_text(compatibility_script, (
        '"""Compatibility entry point for the renamed context-continuity skill."""\n'
        "from pathlib import Path\nimport runpy\nimport sys\n\n"
        "target = Path(__file__).resolve().parents[2] / 'context-continuity' / 'scripts' / 'context_continuity.py'\n"
        "sys.path.insert(0, str(target.parent))\n"
        "runpy.run_path(str(target), run_name='__main__')\n"
    ))
    if legacy_target.exists():
        skills_root = (home / "skills").resolve()
        if legacy_target.resolve().parent != skills_root:
            raise ValueError(f"refusing to remove unexpected legacy path: {legacy_target}")
        shutil.rmtree(legacy_target)
    print(json.dumps({"skill": str(target), "compatibility_script": str(compatibility_script), "basic_instructions": str(agents_path),
                      "hooks": str(hooks_path) if args.with_hooks else None,
                      "restart_required": True, "hook_trust_required": args.with_hooks}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

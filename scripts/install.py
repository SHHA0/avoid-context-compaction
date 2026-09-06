#!/usr/bin/env python3
"""Install Avoid Context Compaction and merge its Codex lifecycle hooks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path


EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PreCompact")
INSTALL_IGNORES = (".git", ".avoid-context-compaction", ".context-guard", "__pycache__", "*.pyc")


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


def is_managed_group(group: object) -> bool:
    if not isinstance(group, dict):
        return False
    for handler in group.get("hooks", []):
        command = str(handler.get("command", "")) + str(handler.get("commandWindows", "")) if isinstance(handler, dict) else ""
        if "avoid_context_compaction.py" in command or "context_guard.py" in command:
            return True
    return False


def remove_readonly(function, path: str, _exc_info: object) -> None:
    """Allow upgrades to remove read-only files copied by older installers."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    home = Path(args.codex_home).expanduser().resolve()
    target = home / "skills" / "avoid-context-compaction"
    legacy_target = home / "skills" / "context-guard"
    hooks_path = home / "hooks.json"

    template = json.loads((source / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    existing = {"description": "User lifecycle hooks.", "hooks": {}}
    if hooks_path.exists():
        existing = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
        if not isinstance(existing, dict) or not isinstance(existing.get("hooks"), dict):
            raise ValueError(f"unsupported hooks file structure: {hooks_path}")
    for event in EVENTS:
        groups = existing["hooks"].setdefault(event, [])
        groups[:] = [group for group in groups if not is_managed_group(group)]
        groups.extend(template["hooks"][event])

    target.parent.mkdir(parents=True, exist_ok=True)
    if source != target:
        skills_root = (home / "skills").resolve()
        if target.resolve().parent != skills_root:
            raise ValueError(f"refusing to replace unexpected install path: {target}")
        if target.exists():
            shutil.rmtree(target, onerror=remove_readonly)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*INSTALL_IGNORES))
    installed_script = target / "scripts" / "avoid_context_compaction.py"
    command = f'"{Path(sys.executable).resolve()}" "{installed_script}" hook'
    for event in EVENTS:
        for group in existing["hooks"][event]:
            if not is_managed_group(group):
                continue
            for handler in group.get("hooks", []):
                handler["command"] = command
                handler["commandWindows"] = command
    atomic_json(hooks_path, existing)
    if legacy_target.exists():
        skills_root = (home / "skills").resolve()
        if legacy_target.resolve().parent != skills_root:
            raise ValueError(f"refusing to remove unexpected legacy path: {legacy_target}")
        shutil.rmtree(legacy_target)
    print(json.dumps({"skill": str(target), "hooks": str(hooks_path), "restart_required": True, "hook_trust_required": True}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "avoid_context_compaction.py"
INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
SPEC = importlib.util.spec_from_file_location("avoid_context_compaction", SCRIPT)
core = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(core)


def event(timestamp, inputs, outputs, window=1000):
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 999999},
        "last_token_usage": {"input_tokens": inputs, "output_tokens": outputs}, "model_context_window": window}}}


class ContextStateTests(unittest.TestCase):
    def write_jsonl(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")

    def task_data(self, language="en"):
        return {
            "language": language, "task": "Build tracker" if language == "en" else "构建状态记录器",
            "project_goal": "Preserve task state" if language == "en" else "保存任务状态",
            "expected_outcome": "Continue after compaction" if language == "en" else "压缩后继续任务",
            "requirements": ["Keep corrections"], "corrections": ["Use the latest scope"],
            "decisions": ["Use native compaction"], "completed": ["Parser"], "results": ["State is recoverable"],
            "verification": ["Unit check passed"], "remaining": ["Install"], "next_steps": ["Restart Codex"],
            "cautions": ["Snapshot may lag"], "workspace": ["Clean worktree"], "status": "ready",
        }

    def test_usage_uses_latest_request_without_stop_levels(self):
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "session.jsonl"
            now = core.utcnow().isoformat()
            self.write_jsonl(transcript, [event(now, 100, 10), event(now, 900, 20)])
            result = core.read_usage(transcript, 300)
            self.assertEqual(result["level"], "ok")
            self.assertAlmostEqual(result["conservative_ratio"], .92)

    def test_compaction_invalidates_an_older_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "session.jsonl"
            self.write_jsonl(transcript, [event(core.utcnow().isoformat(), 700, 0), {"type": "compacted"}])
            result = core.read_usage(transcript, 300)
            self.assertEqual(result["level"], "unknown")

    def test_threshold_parser(self):
        self.assertEqual(core.parse_thresholds("0.8, 0.5,0.5"), (.5, .8))
        with self.assertRaises(Exception):
            core.parse_thresholds("0,0.8")

    def test_old_task_schema_is_normalized(self):
        data = self.task_data()
        data["goal"] = data.pop("project_goal")
        data.pop("expected_outcome")
        data.pop("corrections")
        data.pop("results")
        normalized = core.validate_task_state(data)
        self.assertEqual(normalized["project_goal"], normalized["expected_outcome"])
        self.assertEqual(normalized["corrections"], [])

    def test_explicit_handoff_is_session_scoped_and_localized(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            transcript = root / "session.jsonl"
            self.write_jsonl(transcript, [event(core.utcnow().isoformat(), 200, 0)])
            source = root / "input.json"
            data = self.task_data("zh-CN")
            data["base_state_saved_at"] = None
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            env = dict(os.environ, CODEX_THREAD_ID="session-zh")
            global_args = ["--transcript", str(transcript)]
            subprocess.run([sys.executable, str(SCRIPT), *global_args, "activate", "--project", str(project), "--language", "zh-CN"],
                           env=env, check=True, capture_output=True, encoding="utf-8")
            subprocess.run([sys.executable, str(SCRIPT), *global_args, "decision", "--choice", "yes", "--project", str(project)],
                           env=env, check=True, capture_output=True, encoding="utf-8")
            subprocess.run([sys.executable, str(SCRIPT), *global_args, "state-refresh", "--input", str(source), "--project", str(project)],
                           env=env, check=True, capture_output=True, encoding="utf-8")
            command = [sys.executable, str(SCRIPT), *global_args, "handoff", "--project", str(project)]
            result = json.loads(subprocess.run(command, env=env, check=True, capture_output=True, encoding="utf-8").stdout)
            self.assertIn("session-zh", result["handoff"])
            self.assertIn("任务交接", Path(result["handoff"]).read_text(encoding="utf-8"))
            self.assertIn("继续项目", Path(result["resume"]).read_text(encoding="utf-8"))
            self.assertTrue(Path(result["structured"]).exists())

    def test_installer_removes_obsolete_tool_gates(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "codex-home"
            home.mkdir()
            hooks = {"description": "existing", "hooks": {"PreToolUse": [{"hooks": [
                {"type": "command", "command": "python avoid_context_compaction.py hook"},
                {"type": "command", "command": "existing-tool"}
            ]}], "PostToolUse": [{"hooks": [{"type": "command", "command": "python context_guard.py hook"}]}],
                "SessionStart": [{"hooks": [{"type": "command", "command": "python avoid_context_compaction.py hook"}]}],
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python avoid_context_compaction.py hook"}]}],
                "PreCompact": [{"hooks": [{"type": "command", "command": "python avoid_context_compaction.py hook"}]}]}}
            (home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            command = [sys.executable, str(INSTALLER), "--codex-home", str(home), "--with-hooks"]
            subprocess.run(command, check=True, capture_output=True, text=True)
            merged = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
            self.assertNotIn("PostToolUse", merged["hooks"])
            self.assertTrue(any("existing-tool" in str(group) for group in merged["hooks"]["PreToolUse"]))
            self.assertFalse(any("avoid_context_compaction.py" in str(group) for group in merged["hooks"]["PreToolUse"]))
            for event_name in ("SessionStart", "UserPromptSubmit", "PreCompact", "PostToolUse"):
                self.assertNotIn(event_name, merged["hooks"])
            self.assertIn("Stop", merged["hooks"])
            agents = (home / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("state_update_due", agents)
            self.assertNotIn("hard-stop", agents)


if __name__ == "__main__":
    unittest.main()

import contextlib
import datetime as dt
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
core = importlib.import_module("avoid_context_compaction")
monitor = importlib.import_module("monitor")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "home"
        self.transcript = self.root / "session-a.jsonl"
        self.transcript.touch()
        self.global_args = ["--session-id", "session-a", "--codex-home", str(self.home), "--transcript", str(self.transcript)]

    def command(self, name, *extra):
        args = core.build_parser().parse_args(self.global_args + [name, "--project", str(self.project), *extra])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            monitor.command(args)
        return json.loads(output.getvalue())

    def hook(self, event, **fields):
        args = core.build_parser().parse_args(self.global_args + ["hook"])
        incoming = dict(hook_event_name=event, session_id="session-a", cwd=str(self.project),
                        transcript_path=str(self.transcript), turn_id="turn-1", **fields)
        old = sys.stdin
        output = io.StringIO()
        try:
            sys.stdin = io.StringIO(json.dumps(incoming))
            with contextlib.redirect_stdout(output):
                monitor.hook(args)
        finally:
            sys.stdin = old
        return json.loads(output.getvalue()) if output.getvalue() else None

    def append(self, ratio=None, compact=False, stamp=None):
        stamp = stamp or core.utcnow().isoformat()
        if compact:
            row = {"type": "compacted", "timestamp": stamp}
        else:
            row = {"type": "event_msg", "timestamp": stamp, "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": round(ratio * 1000), "output_tokens": 0}, "model_context_window": 1000}}}
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def state(self):
        return monitor.load(monitor.state_path(self.project, "session-a"))

    def task_data(self):
        return {
            "language": "zh-CN", "task": "长任务", "project_goal": "完成项目目标",
            "expected_outcome": "测试通过并交付结果", "requirements": ["保持兼容"],
            "corrections": ["采用最新要求"], "decisions": ["使用原生压缩"], "completed": ["完成阶段一"],
            "results": ["当前行为符合预期"], "verification": ["阶段检查通过"], "remaining": ["完成阶段二"],
            "next_steps": ["继续实现"], "cautions": [], "workspace": ["测试工作区"], "status": "active",
        }

    def run_state_update(self):
        source = self.root / "state-input.json"
        source.write_text(json.dumps(self.task_data(), ensure_ascii=False), encoding="utf-8")
        command = [sys.executable, "-B", str(Path(core.__file__)), *self.global_args,
                   "state-update", "--project", str(self.project), "--input", str(source)]
        return subprocess.run(command, capture_output=True, text=True)

    def run_handoff(self):
        source = self.root / "handoff-input.json"
        source.write_text(json.dumps(self.task_data(), ensure_ascii=False), encoding="utf-8")
        command = [sys.executable, "-B", str(Path(core.__file__)), *self.global_args,
                   "handoff", "--project", str(self.project), "--input", str(source)]
        return subprocess.run(command, capture_output=True, text=True)

    def test_uninvoked_session_is_silent(self):
        self.append(.95)
        for event in monitor.EVENTS:
            self.assertIsNone(self.hook(event))
        self.assertFalse((self.project / core.DATA_DIR_NAME).exists())

    def test_activation_localizes_footer_and_never_stops_at_90_percent(self):
        self.append(.91)
        result = self.command("activate", "--language", "zh-CN")
        self.assertEqual(result["state_update_level"], .8)
        self.assertIn("当前上下文用量", result["footer"])
        self.assertNotIn("handoff", result["footer"].lower())
        self.assertIsNone(self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "continue"}))

    def test_50_and_80_percent_each_trigger_once_per_cycle(self):
        self.append(.49)
        self.command("activate")
        self.assertFalse(self.command("final-check")["state_update_due"])
        self.append(.50)
        first = self.command("final-check")
        self.assertTrue(first["state_update_due"])
        self.assertEqual(first["state_update_level"], .5)
        updated = self.run_state_update()
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertFalse(self.command("final-check")["state_update_due"])
        self.append(.79)
        self.assertFalse(self.command("final-check")["state_update_due"])
        self.append(.80)
        second = self.command("final-check")
        self.assertEqual(second["state_update_level"], .8)

    def test_jump_to_80_coalesces_both_thresholds(self):
        self.append(.40)
        self.command("activate")
        self.append(.82)
        result = self.command("final-check")
        self.assertEqual(result["state_update_level"], .8)
        self.assertEqual(self.state()["levels_reached"], [.5, .8])
        self.assertEqual(self.run_state_update().returncode, 0)
        self.assertFalse(self.command("final-check")["state_update_due"])

    def test_compaction_starts_a_new_threshold_cycle(self):
        self.append(.52)
        self.command("activate")
        self.assertEqual(self.run_state_update().returncode, 0)
        self.append(compact=True)
        self.append(.20)
        after_compaction = self.command("final-check")
        self.assertEqual(self.state()["cycle"], 1)
        self.assertFalse(after_compaction["state_update_due"])
        self.append(.51)
        next_cycle = self.command("final-check")
        self.assertEqual(next_cycle["state_update_level"], .5)

    def test_stop_requests_state_update_before_footer_without_hard_stop(self):
        self.append(.81)
        self.command("activate")
        result = self.hook("Stop", last_assistant_message="done\nContext usage: approximately 81.0%.")
        self.assertEqual(result["decision"], "block")
        self.assertIn("state-update", result["reason"])
        self.assertNotIn("stop the task", result["reason"].lower())

    def test_correct_footer_does_not_extend_turn(self):
        self.append(.30)
        self.command("activate")
        footer = self.command("final-check")["footer"]
        self.assertIsNone(self.hook("Stop", last_assistant_message="done\n" + footer))

    def test_old_hard_stop_state_is_removed_on_upgrade(self):
        self.append(.30)
        self.command("activate")
        state = self.state()
        state.update(pending_peak=.95, hard_stop_pending=True, pending_compaction=True)
        core.atomic_json(monitor.state_path(self.project, "session-a"), state)
        result = self.command("final-check")
        self.assertFalse(result["state_update_due"])
        self.assertNotIn("hard_stop_pending", self.state())

    def test_missing_and_stale_usage_are_visible(self):
        unknown = self.command("activate")
        self.assertIn("unavailable", unknown["footer"])
        old = (core.utcnow() - dt.timedelta(minutes=10)).isoformat()
        self.append(.60, stamp=old)
        stale = self.command("final-check")
        self.assertEqual(stale["usage"]["level"], "stale")
        self.assertIn("unavailable", stale["footer"])

    def test_state_update_requires_a_due_threshold(self):
        self.append(.20)
        self.command("activate")
        result = self.run_state_update()
        self.assertEqual(result.returncode, 2)
        self.assertIn("no 50% or 80%", result.stderr)

    def test_monitored_handoff_requires_and_consumes_explicit_consent(self):
        self.append(.20)
        self.command("activate")
        denied = self.run_handoff()
        self.assertEqual(denied.returncode, 2)
        self.assertFalse(list(self.project.rglob("HANDOFF.md")))
        self.command("decision", "--choice", "yes")
        accepted = self.run_handoff()
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue(list(self.project.rglob("HANDOFF.md")))
        self.assertFalse(self.state().get("approved"))
        self.assertEqual(self.run_handoff().returncode, 2)

    def test_partial_record_is_retried(self):
        self.append(.30)
        self.command("activate")
        row = {"type": "event_msg", "timestamp": core.utcnow().isoformat(), "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 510, "output_tokens": 0}, "model_context_window": 1000}}}
        encoded = json.dumps(row).encode()
        with self.transcript.open("ab") as handle:
            handle.write(encoded[:20])
        self.assertFalse(self.command("final-check")["state_update_due"])
        with self.transcript.open("ab") as handle:
            handle.write(encoded[20:] + b"\n")
        self.assertTrue(self.command("final-check")["state_update_due"])


if __name__ == "__main__":
    unittest.main()

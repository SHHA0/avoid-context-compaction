import contextlib
import datetime as dt
import importlib
import io
import json
import os
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

    def test_uninvoked_session_is_silent_and_creates_no_state(self):
        self.append(.90)
        for event in monitor.EVENTS:
            self.assertIsNone(self.hook(event))
        self.assertFalse((self.project / core.DATA_DIR_NAME).exists())

    def test_activation_persists_without_creating_handoff_and_is_idempotent(self):
        self.append(.63)
        activated = self.command("activate")
        self.assertIn("63.0%", activated["footer"])
        self.assertEqual(activated["automatic_monitoring"], "unverified")
        original = self.state()["activated_at"]
        self.append(.85)
        self.command("final-check")
        self.command("activate")
        self.assertEqual(self.state()["activated_at"], original)
        self.assertEqual(self.state()["pending_peak"], .85)
        self.assertFalse(list(self.project.rglob("HANDOFF.md")))

    def test_thresholds_and_stop_correction_loop_guard(self):
        self.append(.74)
        self.command("activate")
        normal_footer = self.command("final-check")["footer"]
        self.assertIsNone(self.hook("Stop", last_assistant_message="done\n" + normal_footer))
        self.append(.75)
        self.assertIsNone(self.hook("PostToolUse"))
        warning = self.command("final-check")["footer"]
        self.assertIn("75%", warning)
        self.assertIn("是否生成交接文档", warning)
        self.append(.85)
        result = self.hook("Stop", last_assistant_message="done")
        self.assertEqual(result["decision"], "block")
        self.assertIn("85%", result["reason"])
        guarded = self.hook("Stop", stop_hook_active=True, last_assistant_message="done")
        self.assertNotIn("decision", guarded)
        guarded = self.hook("Stop", last_assistant_message="done")
        self.assertNotIn("decision", guarded)

    def test_correct_final_footer_does_not_extend_turn(self):
        self.append(.80)
        self.command("activate")
        footer = self.command("final-check")["footer"]
        self.assertIsNone(self.hook("Stop", last_assistant_message="done\n" + footer))
        self.assertTrue(self.state()["awaiting_choice"])

    def test_normal_footer_snapshot_drift_does_not_extend_turn(self):
        self.append(.30)
        self.command("activate")
        footer = self.command("final-check")["footer"]
        self.append(.31)
        self.assertIsNone(self.hook("Stop", last_assistant_message="done\n" + footer))

    def test_snapshot_drift_does_not_duplicate_reminder_but_escalation_does(self):
        self.append(.80)
        self.command("activate")
        footer = self.command("final-check")["footer"]
        self.append(.81)
        self.assertIsNone(self.hook("Stop", last_assistant_message="done\n" + footer))
        self.append(.86)
        self.assertEqual(self.hook("Stop", last_assistant_message="done\n" + footer)["decision"], "block")

    def test_hard_stop_after_tool_blocks_new_work_but_allows_control_commands(self):
        self.append(.89)
        self.command("activate")
        self.append(.91)
        signal = self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "build"})
        self.assertIn("90%", signal["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.state()["hard_stop_pending"])

        reminder = self.command("final-check")["footer"]
        self.assertIn("90% 硬停止线", reminder)
        self.assertIn("当前任务已在安全边界暂停", reminder)
        self.append(.92)
        self.assertIsNone(self.hook("Stop", last_assistant_message="paused\n" + reminder))

        blocked = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "run another task"})
        self.assertEqual(blocked["hookSpecificOutput"]["permissionDecision"], "deny")
        allowed = self.hook("PreToolUse", tool_name="Bash", tool_input={
            "command": "python avoid_context_compaction.py final-check --project project"
        })
        self.assertIsNone(allowed)
        self.assertEqual(self.command("doctor")["hard_stop_enforcement"], "observed")

        self.command("decision", "--choice", "no")
        self.assertFalse(self.state()["hard_stop_pending"])

    def test_manual_fallback_resumes_after_decline_on_next_user_message(self):
        self.append(.80)
        self.command("activate")
        self.command("decision", "--choice", "no")
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": core.utcnow().isoformat(), "type": "event_msg",
                                     "payload": {"type": "user_message", "message": "next task"}}) + "\n")
        self.assertIn("是否生成交接文档", self.command("final-check")["footer"])

    def test_missed_peak_survives_compaction_before_next_hook(self):
        self.append(.63)
        self.command("activate")
        self.append(.76)
        self.append(.88)
        self.append(compact=True)
        self.append(.20)
        result = self.command("final-check")
        self.assertEqual(result["usage"]["level"], "ok")
        self.assertEqual(self.state()["pending_peak"], .88)
        self.assertTrue(self.state()["pending_compaction"])
        self.assertIn("85%", result["footer"])
        self.assertIn("20.0%", result["footer"])

    def test_compaction_without_known_peak_still_offers_handoff(self):
        self.append(.63)
        self.command("activate")
        self.hook("PreCompact")
        self.append(compact=True)
        self.assertIn("是否生成交接文档", self.command("final-check")["footer"])
        restored = self.hook("SessionStart", source="compact")
        self.assertIn("final-check", restored["hookSpecificOutput"]["additionalContext"])

    def test_old_history_before_activation_does_not_trigger_peak_or_compaction(self):
        old = (core.utcnow() - dt.timedelta(days=1)).isoformat()
        self.append(.95, stamp=old)
        self.append(compact=True, stamp=old)
        self.append(.30)
        result = self.command("activate")
        self.assertIn("30.0%", result["footer"])
        self.assertFalse(self.state().get("pending_compaction"))

    def test_decline_suppresses_choice_turn_but_monitors_next_request(self):
        self.append(.80)
        self.command("activate")
        self.command("decision", "--choice", "no")
        self.assertEqual(self.command("final-check")["footer"], "")
        self.assertIsNone(self.hook("Stop", last_assistant_message="declined"))
        self.hook("UserPromptSubmit", prompt="continue work")
        self.assertIn("75%", self.command("final-check")["footer"])
        self.append(.86)
        self.assertIn("85%", self.command("final-check")["footer"])
        self.assertFalse(list(self.project.rglob("HANDOFF.md")))

    def test_unrelated_prompt_never_grants_consent(self):
        self.append(.80)
        self.command("activate")
        self.hook("UserPromptSubmit", prompt="fix another issue")
        self.assertFalse(self.state().get("approved"))
        self.assertTrue(self.state()["awaiting_choice"])

    def test_consent_required_and_consumed_only_after_successful_generation(self):
        self.append(.80)
        self.command("activate")
        source = self.root / "input.json"
        data = {k: [] for k in core.LIST_FIELDS}
        data.update(task="测试交接", goal="Keep real evidence", status="ready")
        source.write_text(json.dumps(data), encoding="utf-8")
        command = [sys.executable, "-B", str(Path(core.__file__)), *self.global_args,
                   "checkpoint", "--project", str(self.project), "--input", str(source)]
        denied = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(denied.returncode, 2)
        self.assertFalse(list(self.project.rglob("HANDOFF.md")))
        self.command("decision", "--choice", "yes")
        source.write_text("{}", encoding="utf-8")
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 2)
        self.assertTrue(self.state()["approved"])
        source.write_text(json.dumps(data), encoding="utf-8")
        accepted = subprocess.run(command, capture_output=True, encoding="utf-8")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        files = json.loads(accepted.stdout)
        self.assertIn("测试交接", Path(files["handoff"]).read_text(encoding="utf-8"))
        self.assertFalse(self.state()["approved"])
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)

    def test_other_session_is_not_enabled(self):
        self.command("activate")
        self.global_args[1] = "session-b"
        with self.assertRaises(ValueError):
            self.command("final-check")

    def test_primary_agent_id_does_not_suppress_enabled_session_hook(self):
        self.append(.30)
        self.command("activate")
        self.assertIsNone(self.hook("PostToolUse", agent_id="primary-agent"))
        self.assertIn("PostToolUse", self.state()["observed_events"])

    def test_doctor_requires_observed_stop_not_just_config(self):
        self.home.mkdir()
        template = Path(core.__file__).parents[1] / "hooks" / "hooks.json"
        (self.home / "hooks.json").write_bytes(template.read_bytes())
        self.append(.30)
        result = self.command("activate")
        self.assertEqual(result["missing_events"], [])
        self.assertEqual(result["automatic_monitoring"], "unverified")
        self.assertEqual(result["hard_stop_enforcement"], "unverified")
        self.hook("Stop", last_assistant_message="done")
        self.assertEqual(self.command("doctor")["automatic_monitoring"], "observed")

    def test_missing_and_stale_usage_is_visible_not_reported_safe(self):
        unknown = self.command("activate")
        self.assertIn("不可确认", unknown["footer"])
        old = (core.utcnow() - dt.timedelta(minutes=10)).isoformat()
        self.append(.60, stamp=old)
        result = self.command("final-check")
        self.assertEqual(result["usage"]["level"], "stale")
        self.assertIn("不可确认", result["footer"])

    def test_final_check_reports_normal_usage_and_records_evidence(self):
        self.append(.40)
        self.command("activate")
        self.command("final-check")
        result = self.command("final-check")
        self.assertIn("40.0%", result["footer"])
        self.assertIn("未达到 75%", result["footer"])
        self.assertEqual(result["final_check_count"], 2)
        self.assertIsNotNone(result["last_final_check"])

    def test_partial_record_retried(self):
        self.append(.30)
        self.command("activate")
        row = {"type": "compacted", "timestamp": core.utcnow().isoformat()}
        encoded = json.dumps(row).encode()
        with self.transcript.open("ab") as handle:
            handle.write(encoded[:20])
        self.command("final-check")
        self.assertFalse(self.state().get("pending_compaction"))
        with self.transcript.open("ab") as handle:
            handle.write(encoded[20:] + b"\n")
        self.command("final-check")
        self.assertTrue(self.state()["pending_compaction"])


if __name__ == "__main__":
    unittest.main()

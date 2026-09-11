import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "avoid_context_compaction.py"
INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
SPEC = importlib.util.spec_from_file_location("avoid_context_compaction", SCRIPT)
cg = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(cg)


def event(timestamp, inputs, outputs, window=1000):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"input_tokens": 999999},
                "last_token_usage": {"input_tokens": inputs, "output_tokens": outputs},
                "model_context_window": window,
            },
        },
    }


class ContextGuardTests(unittest.TestCase):
    def write_jsonl(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")

    def test_uses_last_request_and_thresholds(self):
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "rollout-session-a.jsonl"
            now = cg.utcnow().isoformat()
            self.write_jsonl(transcript, [event(now, 100, 10), event(now, 800, 60)])
            result = cg.read_usage(transcript, 0.75, 0.85, 300)
            self.assertEqual(result["level"], "handoff")
            self.assertEqual(result["input_tokens"], 800)
            self.assertAlmostEqual(result["conservative_ratio"], 0.86)

    def test_hard_stop_level(self):
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "rollout-session-a.jsonl"
            self.write_jsonl(transcript, [event(cg.utcnow().isoformat(), 900, 10)])
            result = cg.read_usage(transcript, 0.75, 0.85, 300, 0.90)
            self.assertEqual(result["level"], "hard_stop")
            self.assertAlmostEqual(result["conservative_ratio"], 0.91)

    def test_compaction_invalidates_older_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "rollout-session-a.jsonl"
            self.write_jsonl(transcript, [event(cg.utcnow().isoformat(), 700, 0), {"type": "event_msg", "payload": {"type": "compacted"}}])
            result = cg.read_usage(transcript, 0.75, 0.85, 300)
            self.assertEqual(result["level"], "unknown")
            self.assertIn("predates compaction", result["reason"])

    def test_find_transcript_matches_session_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wanted = root / "sessions" / "2026" / "rollout-session-a.jsonl"
            other = root / "sessions" / "2026" / "rollout-session-b.jsonl"
            self.write_jsonl(wanted, [event(cg.utcnow().isoformat(), 10, 0)])
            self.write_jsonl(other, [event(cg.utcnow().isoformat(), 900, 0)])
            os.utime(other, None)
            self.assertEqual(cg.find_transcript(root, "session-a"), wanted)

    def test_checkpoint_generations_and_installer_merge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            input_file = root / "checkpoint.json"
            input_file.write_text(json.dumps({
                "task": "Build guard", "goal": "Preserve task state",
                "requirements": ["Keep corrections"], "completed": ["Parser"],
                "verification": ["Unit check passed"], "decisions": ["Use snapshots"],
                "remaining": ["Install"], "next_steps": ["Restart Codex"],
                "cautions": ["Snapshot may lag"], "workspace": ["No git repository"],
                "status": "ready"
            }), encoding="utf-8")
            env = dict(os.environ, CODEX_THREAD_ID="test-session")
            command = [sys.executable, str(SCRIPT), "checkpoint", "--input", str(input_file), "--project", str(project)]
            first = subprocess.run(command, env=env, check=True, capture_output=True, text=True)
            second = subprocess.run(command, env=env, check=True, capture_output=True, text=True)
            first_result = json.loads(first.stdout)
            second_result = json.loads(second.stdout)
            self.assertNotEqual(first_result["handoff"], second_result["handoff"])
            self.assertTrue(Path(second_result["resume"]).exists())

            home = root / "codex-home"
            hooks = {"description": "existing", "hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": "existing-tool"},
                {"type": "command", "command": "python context_guard.py hook"}
            ]}]}}
            home.mkdir()
            (home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            install_command = [sys.executable, str(INSTALLER), "--codex-home", str(home), "--with-hooks"]
            subprocess.run(install_command, check=True, capture_output=True, text=True)
            installed = home / "skills" / "avoid-context-compaction"
            (installed / "obsolete.txt").write_text("old install artifact", encoding="utf-8")
            subprocess.run(install_command, check=True, capture_output=True, text=True)
            merged = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
            self.assertEqual(sum(isinstance(g, dict) and any("avoid_context_compaction.py" in str(h) for h in g.get("hooks", [])) for g in merged["hooks"]["SessionStart"]), 1)
            self.assertTrue(any(h.get("command") == "existing-tool" for g in merged["hooks"]["SessionStart"] for h in g.get("hooks", [])))
            self.assertTrue((installed / "SKILL.md").exists())
            self.assertFalse((installed / ".git").exists())
            self.assertFalse((installed / "obsolete.txt").exists())
            self.assertIn("Stop", merged["hooks"])
            self.assertIn("PreToolUse", merged["hooks"])
            managed_handlers = [
                h
                for groups in merged["hooks"].values()
                for group in groups
                for h in group.get("hooks", [])
                if "avoid_context_compaction.py" in str(h)
            ]
            self.assertTrue(managed_handlers)
            self.assertTrue(all(h["commandWindows"].startswith('& "') for h in managed_handlers))
            self.assertTrue(all(not h["command"].startswith("& ") for h in managed_handlers))
            agents = (home / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(agents.count("avoid-context-compaction:basic-monitor:begin"), 1)
            self.assertIn("final-check", agents)

    def test_basic_install_preserves_agents_and_does_not_create_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "codex-home"
            home.mkdir()
            (home / "AGENTS.md").write_text("# Existing rule\n", encoding="utf-8")
            command = [sys.executable, str(INSTALLER), "--codex-home", str(home)]
            first = subprocess.run(command, check=True, capture_output=True, text=True)
            subprocess.run(command, check=True, capture_output=True, text=True)
            result = json.loads(first.stdout)
            agents = (home / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("# Existing rule", agents)
            self.assertEqual(agents.count("avoid-context-compaction:basic-monitor:begin"), 1)
            self.assertFalse((home / "hooks.json").exists())
            self.assertFalse(result["hook_trust_required"])


if __name__ == "__main__":
    unittest.main()

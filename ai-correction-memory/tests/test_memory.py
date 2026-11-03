"""Behavior tests use isolated temporary stores, never the user's real memory."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from memory import Memory, default_data_dir


def rule(key="writing.answer-order", desired="先给结论，再解释", domains=None):
    return {"key": key, "title": "回答顺序", "desired": desired,
            "rejected": ["先铺陈背景"], "checks": ["第一段是否直接回答问题？"],
            "literal_avoid": [], "domains": domains or ["*"], "keywords": ["结论"],
            "evidence": "请先给结论"}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "data"
        self.mem = Memory(self.directory)

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def test_windows_sandbox_data_location_is_stable(self):
        for location in ("C:/Users/test/AppData/Local/Packages/sandbox.{test}/AC",
                         "C:/Users/test/AppData/Local/Packages/OpenAI.Codex/LocalCache/Local"):
            with patch("memory.sys.platform", "win32"), patch.dict(os.environ, {
                "LOCALAPPDATA": location, "AI_CORRECTION_MEMORY_DIR": "",
            }):
                self.assertEqual(default_data_dir(), Path.home() / "AppData/Local/ai-correction-memory")
        with patch.dict(os.environ, {"AI_CORRECTION_MEMORY_DIR": str(self.directory)}):
            self.assertEqual(default_data_dir(), self.directory.resolve())

    def test_session_isolation_and_persistence(self):
        self.mem.record(rule(), session="chat-A")
        self.assertEqual(self.mem.prepare("chat-A")["count"], 1)
        self.assertEqual(self.mem.prepare("chat-B")["count"], 0)
        with Memory(self.directory) as second:
            self.assertEqual(second.prepare("chat-A")["count"], 1)

    def test_missing_session_never_becomes_global(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "Session ID required"):
                self.mem.record(rule())
        self.assertEqual(self.mem.list_rules(), [])

    def test_persistent_consent_required(self):
        for scope in ("global", "project"):
            with self.assertRaisesRegex(ValueError, "explicit"):
                self.mem.record(rule(), scope=scope, project=str(self.directory))

    def test_exact_project_scope_and_no_relative_paths(self):
        self.mem.record(rule(), scope="project", project=str(self.directory), consent_quote="这个项目以后这样")
        self.assertEqual(self.mem.prepare("x", str(self.directory))["count"], 1)
        self.assertEqual(self.mem.prepare("x", str(self.directory / "child"))["count"], 0)
        with self.assertRaisesRegex(ValueError, "absolute"):
            self.mem.prepare("x", "relative")

    def test_scope_override_keeps_original(self):
        global_r = self.mem.record(rule(), scope="global", consent_quote="以后都先给结论")
        project_r = self.mem.record(rule(desired="本项目多解释"), scope="project", project=str(self.directory), consent_quote="这个项目多解释")
        session_r = self.mem.record(rule(desired="这次先讲过程"), session="A")
        prepared = self.mem.prepare("A", str(self.directory))
        self.assertEqual(prepared["rules"][0]["id"], session_r["id"])
        self.assertEqual(len(prepared["shadowed"]), 2)
        self.assertEqual(self.mem.prepare("B", str(self.directory))["rules"][0]["id"], project_r["id"])
        self.assertEqual(self.mem.prepare("B")["rules"][0]["id"], global_r["id"])
        self.assertEqual(self.mem.get(global_r["id"])["revision"], 1)

    def test_domain_filter_is_optional_and_star_always_applies(self):
        self.mem.record(rule(), session="A")
        self.mem.record(rule(key="video.motion", domains=["video"]), session="A")
        self.assertEqual(self.mem.prepare("A")["count"], 2)
        self.assertEqual(self.mem.prepare("A", domains=["writing"])["count"], 1)
        self.assertEqual(self.mem.prepare("A", domains=["video"])["count"], 2)

    def test_update_revision_conflict_and_undo(self):
        r = self.mem.record(rule(), session="A")
        after = self.mem.update(r["id"], 1, rule(desired="先讲过程"))
        self.assertEqual(after["revision"], 2)
        with self.assertRaisesRegex(ValueError, "Revision conflict"):
            self.mem.update(r["id"], 1, rule())
        restored = self.mem.undo(r["id"], 2)
        self.assertEqual(restored["desired"], r["desired"])
        self.assertEqual(restored["revision"], 3)
        self.assertEqual(len(self.mem.history(r["id"])), 3)

    def test_disable_and_restore(self):
        r = self.mem.record(rule(), session="A")
        self.mem.set_enabled(r["id"], 1, False)
        self.assertEqual(self.mem.prepare("A")["count"], 0)
        self.mem.undo(r["id"], 2)
        self.assertEqual(self.mem.prepare("A")["count"], 1)

    def test_undo_creation_explains_alternative(self):
        r = self.mem.record(rule(), session="A")
        with self.assertRaisesRegex(ValueError, "Creation cannot"):
            self.mem.undo(r["id"], 1)

    def test_forget_removes_index_history_and_live_rule(self):
        r = self.mem.record(rule(), session="A")
        self.assertTrue(self.mem.search("结论"))
        self.mem.forget(r["id"], 1)
        self.assertEqual(self.mem.search("结论"), [])
        self.assertEqual(self.mem.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "not found"):
            self.mem.get(r["id"])

    def test_clear_only_one_session(self):
        self.mem.record(rule(), session="A")
        self.mem.record(rule(), session="B")
        self.mem.record(rule(), scope="global", consent_quote="以后这样")
        self.assertEqual(self.mem.clear_session("A")["count"], 1)
        self.assertEqual(len(self.mem.list_rules()), 2)

    def test_cjk_and_english_lexical_search_and_query_escaping(self):
        self.mem.record(rule(), session="A")
        self.mem.record(rule(key="writing.email", desired="Use plain language in email replies"), session="A")
        self.assertEqual(len(self.mem.search("结论")), 2)  # both share evidence/tags
        self.assertEqual(len(self.mem.search("email")), 1)
        self.mem.search('" OR * ; DROP TABLE rules; --')
        self.assertEqual(len(self.mem.list_rules()), 2)

    def test_literal_scan_cannot_claim_semantic_success_or_save_draft(self):
        spec = rule()
        spec["literal_avoid"] = ["总而言之"]
        self.mem.record(spec, session="A")
        result = self.mem.review("总而言之，这里是结论。", "A")
        self.assertEqual(len(result["literal_warnings"]), 1)
        self.assertIsNone(result["semantic_pass"])
        self.assertFalse(result["draft_saved"])
        self.assertEqual(result["checklist"][0]["status"], "needs_model_review")
        self.assertIsNone(self.mem.review("这里是结论", "A")["semantic_pass"])

    def test_duplicate_record_does_not_destroy_existing_rule(self):
        self.mem.record(rule(), session="A")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.mem.record(rule(desired="different"), session="A")
        self.assertEqual(self.mem.list_rules()[0]["revision"], 1)

    def test_invalid_spec_does_not_write(self):
        for change in ({"checks": []}, {"domains": []}, {"rejected": "string"}, {"unknown": 1}):
            with self.assertRaises(ValueError):
                self.mem.record({**rule(), **change}, session="A")
        self.assertEqual(self.mem.list_rules(), [])

    def test_retrieval_never_drops_zero_keyword_rules(self):
        self.mem.record(rule(), session="A")
        self.assertEqual(self.mem.prepare("A")["omitted"], 0)
        self.assertEqual(self.mem.search("unrelatedword"), [])
        self.assertEqual(self.mem.prepare("A")["count"], 1)

    def test_concurrent_optimistic_updates(self):
        r = self.mem.record(rule(), session="A")
        def write(index):
            with Memory(self.directory) as other:
                try:
                    other.update(r["id"], 1, rule(desired=f"change-{index}"))
                    return "updated"
                except ValueError:
                    return "conflict"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, [1, 2]))
        self.assertCountEqual(results, ["updated", "conflict"])
        self.assertEqual(self.mem.get(r["id"])["revision"], 2)

    def test_cli_utf8_bom_and_error_exit_code(self):
        path = Path(self.tmp.name) / "rule.json"
        path.write_text(json.dumps(rule(), ensure_ascii=False), encoding="utf-8-sig")
        script = str(Path(__file__).resolve().parents[1] / "scripts/memory.py")
        args = [sys.executable, script, "--data-dir", str(self.directory), "record", "--file", str(path), "--session", "A"]
        first = subprocess.run(args, capture_output=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(first.stdout)["desired"], rule()["desired"])
        again = subprocess.run(args, capture_output=True, encoding="utf-8")
        self.assertEqual(again.returncode, 2)
        self.assertIn("already exists", json.loads(again.stdout)["error"])

    def test_install_is_idempotent_and_never_overwrites(self):
        package = Path(__file__).resolve().parents[1]
        destination = Path(self.tmp.name) / "installed"
        args = [sys.executable, str(package / "install.py"), "--destination", str(destination)]
        subprocess.run(args, capture_output=True, check=True)
        second = subprocess.run(args, capture_output=True, check=True)
        self.assertEqual(json.loads(second.stdout)["status"], "already_installed")
        changed = destination / "SKILL.md"
        changed.write_text("custom", encoding="utf-8")
        third = subprocess.run(args, capture_output=True)
        self.assertEqual(third.returncode, 2)
        self.assertEqual(changed.read_text(encoding="utf-8"), "custom")


if __name__ == "__main__":
    unittest.main(verbosity=2)

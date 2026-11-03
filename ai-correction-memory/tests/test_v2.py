import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from memory import Memory


def rule(key="response.detail", desired="先给结论", domains=None):
    return dict(key=key, title="回答要求", desired=desired, evidence=desired,
                checks=["回答是否满足用户要求？"], domains=domains or ["*"])


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mem = Memory(self.root / "data")

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def test_multiple_undo_redo_and_branch(self):
        r = self.mem.record(rule(desired="A"), session="s")
        r = self.mem.update(r["id"], r["revision"], rule(desired="B"))
        r = self.mem.update(r["id"], r["revision"], rule(desired="C"))
        r = self.mem.undo(r["id"], r["revision"])
        self.assertEqual(r["desired"], "B")
        r = self.mem.undo(r["id"], r["revision"])
        self.assertEqual(r["desired"], "A")
        r = self.mem.redo(r["id"], r["revision"])
        self.assertEqual(r["desired"], "B")
        r = self.mem.update(r["id"], r["revision"], rule(desired="D"))
        with self.assertRaisesRegex(ValueError, "No redo"):
            self.mem.redo(r["id"], r["revision"])

    def test_distinct_domains_do_not_hide_each_other(self):
        a = self.mem.record(rule(domains=["writing"]), scope="global", consent_quote="以后写作这样")
        b = self.mem.record(rule(desired="先给代码", domains=["coding"]), session="s")
        context = self.mem.prepare("s")
        self.assertEqual({r["id"] for r in context["rules"]}, {a["id"], b["id"]})
        self.assertTrue(context["requires_domain_choice"])
        self.assertEqual(self.mem.prepare("s", domains=["writing"])["rules"][0]["id"], a["id"])
        self.assertEqual(self.mem.prepare("s", domains=["coding"])["rules"][0]["id"], b["id"])

    def test_same_scope_domain_variants_and_wildcard_fallback(self):
        general = self.mem.record(rule(), session="s")
        special = self.mem.record(rule(desired="先给代码", domains=["coding"]), session="s")
        result = self.mem.prepare("s")
        self.assertEqual(result["domain_winners"]["response.detail"]["coding"], special["id"])
        self.assertEqual(result["domain_winners"]["response.detail"]["*"], general["id"])
        self.assertEqual(self.mem.prepare("s", domains=["office"])["rules"][0]["id"], general["id"])

    def test_task_exception_ends_and_never_reopens(self):
        general = self.mem.record(rule(), scope="global", consent_quote="以后先给结论")
        task = self.mem.begin_task("s")
        special = self.mem.record(rule(desired="这一次先讲过程"), scope="task", session="s", task=task["task"])
        self.assertEqual(self.mem.prepare("s", task=task["task"])["rules"][0]["id"], special["id"])
        self.assertEqual(self.mem.prepare("s")["rules"][0]["id"], general["id"])
        self.assertEqual(self.mem.prepare("other", task=task["task"])["rules"][0]["id"], general["id"])
        self.mem.finish_task("s", task["task"])
        self.assertEqual(self.mem.prepare("s", task=task["task"])["rules"][0]["id"], general["id"])
        with self.assertRaisesRegex(ValueError, "finished"):
            self.mem.begin_task("s", task["task"])

    def test_task_rule_requires_open_task(self):
        with self.assertRaisesRegex(ValueError, "Task scope"):
            self.mem.record(rule(), scope="task", session="s")
        with self.assertRaisesRegex(ValueError, "not open"):
            self.mem.record(rule(), scope="task", session="s", task="missing")

    def test_expiry_and_undo(self):
        r = self.mem.record(rule(), session="s")
        r = self.mem.set_expiry(r["id"], 1, "2000-01-01T00:00:00Z")
        result = self.mem.prepare("s")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["excluded"][0]["reason"], "expired")
        self.mem.undo(r["id"], r["revision"])
        self.assertEqual(self.mem.prepare("s")["count"], 1)
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.mem.set_expiry(r["id"], 3, "2030-01-01")

    def test_receipt_distinguishes_preparation_and_self_review(self):
        r = self.mem.record(rule(), session="s")
        context = self.mem.prepare("s")
        receipt = self.mem.receipt(session="s")
        self.assertEqual(receipt["id"], context["run_id"])
        self.assertEqual(receipt["stage"], "prepared")
        self.assertEqual(receipt["verification"], "not_assessed")
        assessed = self.mem.assess(context["run_id"], [{"rule_id": r["id"], "status": "met", "evidence": "开头直接给出结论。"}])
        self.assertEqual(assessed["stage"], "self_reported_met")
        self.assertEqual(assessed["verification"], "model_self_report")

    def test_assessment_incomplete_or_stale_cannot_pass(self):
        r = self.mem.record(rule(), session="s")
        context = self.mem.prepare("s")
        with self.assertRaisesRegex(ValueError, "Every retrieved"):
            self.mem.assess(context["run_id"], [])
        self.mem.update(r["id"], 1, rule(desired="先解释"))
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.mem.assess(context["run_id"], [{"rule_id": r["id"], "status": "met", "evidence": "旧规则"}])
        self.assertEqual(self.mem.receipt(context["run_id"])["stage"], "prepared")

    def test_uncertainty_and_violation_are_visible(self):
        r = self.mem.record(rule(), session="s")
        run = self.mem.review("开头没有结论", "s")
        for status, expected in (("uncertain", "uncertain"), ("violated", "needs_revision")):
            result = self.mem.assess(run["run_id"], [{"rule_id": r["id"], "status": status, "evidence": "需要复核开头。"}])
            self.assertEqual(result["stage"], expected)
        self.assertFalse(result["draft_saved"])
        self.assertNotIn("开头没有结论", json.dumps(result, ensure_ascii=False))

    def test_all_not_applicable_is_not_reported_as_satisfied(self):
        r = self.mem.record(rule(domains=["writing"]), session="s")
        run = self.mem.prepare("s")
        result = self.mem.assess(run["run_id"], [{"rule_id": r["id"], "status": "not_applicable", "evidence": "当前任务是编程，写作要求不适用。"}])
        self.assertEqual(result["stage"], "no_applicable_rules")

    def test_forget_also_removes_receipt_copies(self):
        r = self.mem.record(rule(), session="s")
        context = self.mem.prepare("s")
        self.mem.forget(r["id"], 1)
        with self.assertRaisesRegex(ValueError, "No activation"):
            self.mem.receipt(context["run_id"])

    def test_clear_session_includes_tasks_and_receipts(self):
        task = self.mem.begin_task("s")
        self.mem.record(rule(), scope="task", session="s", task=task["task"])
        self.mem.prepare("s", task=task["task"])
        self.assertEqual(self.mem.clear_session("s")["count"], 1)
        self.assertEqual(self.mem.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.mem.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_duplicate_merge_and_group_undo_preserve_provenance(self):
        a = self.mem.record(rule(key="a"), session="s")
        b = self.mem.record(rule(key="b"), session="s")
        self.assertEqual(len(self.mem.duplicates()), 1)
        merged = self.mem.merge(a["id"], {b["id"]: 1})
        self.assertFalse(self.mem.get(b["id"])["enabled"])
        self.assertEqual(self.mem.get(b["id"])["evidence"], b["evidence"])
        self.mem.undo_merge(merged["operation"])
        self.assertTrue(self.mem.get(b["id"])["enabled"])

    def test_merge_conflict_rolls_back_every_source(self):
        a = self.mem.record(rule(key="a"), session="s")
        b = self.mem.record(rule(key="b"), session="s")
        c = self.mem.record(rule(key="c", desired="不同要求"), session="s")
        with self.assertRaisesRegex(ValueError, "exact"):
            self.mem.merge(a["id"], {b["id"]: 1, c["id"]: 1})
        self.assertTrue(self.mem.get(b["id"])["enabled"])
        self.assertEqual(self.mem.get(b["id"])["revision"], 1)

    def test_group_undo_refuses_later_user_edits(self):
        a = self.mem.record(rule(key="a"), session="s")
        b = self.mem.record(rule(key="b"), session="s")
        operation = self.mem.merge(a["id"], {b["id"]: 1})["operation"]
        self.mem.update(b["id"], 2, rule(key="b", desired="后来改过"))
        with self.assertRaisesRegex(ValueError, "changed"):
            self.mem.undo_merge(operation)

    def test_export_preview_apply_and_conflicts_do_not_overwrite(self):
        self.mem.record(rule(), scope="global", consent_quote="以后这样")
        self.mem.record(rule(key="session-only"), session="s")
        bundle = self.mem.export_bundle()
        self.assertEqual(len(bundle["rules"]), 1)
        with Memory(self.root / "other") as other:
            preview = other.import_bundle(bundle)
            self.assertEqual(len(preview["would_add"]), 1)
            self.assertEqual(other.list_rules(), [])
            self.assertEqual(len(other.import_bundle(bundle, apply=True)["added"]), 1)
            again = other.import_bundle(bundle, apply=True)
            self.assertEqual(len(again["conflicts_skipped"]), 1)
            self.assertEqual(len(other.list_rules()), 1)

    def test_invalid_import_is_atomic(self):
        r = self.mem.record(rule(), scope="global", consent_quote="以后这样")
        bundle = self.mem.export_bundle()
        bundle["rules"].append({"missing": "fields"})
        with Memory(self.root / "other") as other:
            with self.assertRaises(ValueError):
                other.import_bundle(bundle, apply=True)
            self.assertEqual(other.list_rules(), [])

    def test_online_backup_and_restore_with_safety_copy(self):
        r = self.mem.record(rule(desired="原来"), session="s")
        backup = self.mem.backup(self.root / "snapshot.sqlite3")["backup"]
        self.mem.update(r["id"], 1, rule(desired="修改后"))
        result = self.mem.restore(backup)
        self.assertEqual(self.mem.get(r["id"])["desired"], "原来")
        self.assertTrue(Path(result["safety_backup"]).exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.mem.backup(backup)

    def test_invalid_restore_keeps_current_data(self):
        r = self.mem.record(rule(), session="s")
        path = self.root / "bad.sqlite3"
        path.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.Error):
            self.mem.restore(path)
        self.assertEqual(self.mem.get(r["id"])["revision"], 1)

    def test_backup_with_invalid_rule_metadata_is_rejected_before_replacement(self):
        r = self.mem.record(rule(), session="s")
        backup = self.mem.backup(self.root / "invalid-metadata.sqlite3")["backup"]
        with closing(sqlite3.connect(backup)) as db:
            db.execute("UPDATE rules SET scope='unknown'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "invalid rule metadata"):
            self.mem.restore(backup)
        self.assertEqual(self.mem.get(r["id"])["scope"], "session")

    def test_restore_rejects_empty_fake_v2_schema(self):
        r = self.mem.record(rule(), session="s")
        path = self.root / "fake-v2.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.executescript("CREATE TABLE meta(key TEXT,value TEXT); INSERT INTO meta VALUES('schema','2'); CREATE TABLE rules(id); CREATE TABLE events(id);")
            db.commit()
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            self.mem.restore(path)
        self.assertEqual(self.mem.get(r["id"])["revision"], 1)

    def test_restore_validates_inactive_undo_states(self):
        r = self.mem.record(rule(), session="s")
        self.mem.update(r["id"], 1, rule(desired="修改后的要求"))
        path = self.mem.backup(self.root / "bad-timeline.sqlite3")["backup"]
        with closing(sqlite3.connect(path)) as db:
            db.execute("UPDATE timeline SET state='not json' WHERE position=0")
            db.commit()
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.mem.restore(path)
        self.assertEqual(self.mem.get(r["id"])["revision"], 2)

    def test_import_rejects_empty_target_without_environment_fallback(self):
        from unittest.mock import patch
        self.mem.record(rule(), session="old-chat")
        bundle = self.mem.export_bundle(include_session=True)
        bundle["rules"][0]["target"] = ""
        with Memory(self.root / "other") as other, patch.dict("os.environ", {"CODEX_THREAD_ID": "current-chat"}):
            with self.assertRaisesRegex(ValueError, "cannot be remapped"):
                other.import_bundle(bundle, apply=True)
            self.assertEqual(other.list_rules(), [])

    def test_concurrent_v1_migration_rechecks_schema_under_lock(self):
        import concurrent.futures
        import threading
        from unittest.mock import patch
        import storage
        directory = self.root / "migration-race"
        directory.mkdir()
        with closing(sqlite3.connect(directory / "memory.sqlite3")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); INSERT INTO meta VALUES('schema','1');
              CREATE TABLE rules(id TEXT PRIMARY KEY,key TEXT,scope TEXT,target TEXT,body TEXT,enabled INTEGER,
              revision INTEGER,created_at TEXT,updated_at TEXT,consent_quote TEXT,UNIQUE(key,scope,target));
              CREATE TABLE events(seq INTEGER PRIMARY KEY,rule_id TEXT REFERENCES rules(id) ON DELETE CASCADE,
              action TEXT,at TEXT,before_state TEXT,after_state TEXT);""")
            db.commit()
        barrier = threading.Barrier(2)
        original = storage.backup_db
        def synchronized_backup(db, path):
            result = original(db, path)
            barrier.wait(timeout=10)
            return result
        def open_store(index):
            with Memory(directory) as other:
                return other.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
        with patch.object(storage, "backup_db", side_effect=synchronized_backup):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(open_store, [1, 2]))
        self.assertEqual(results, ["2", "2"])
        with Memory(directory) as other:
            r = other.record(rule(), session="s")
            self.assertEqual(other.get(r["id"])["revision"], 1)
            self.assertEqual(other.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_migrate_v1_keeps_rules_history_and_backup(self):
        directory = self.root / "legacy"
        directory.mkdir()
        body = self.mem._validate(rule())
        snapshot = dict(body, id="old", key=body["key"], scope="session", target="s", enabled=True,
                        revision=1, created_at="2026-01-01", updated_at="2026-01-01", consent_quote="")
        with closing(sqlite3.connect(directory / "memory.sqlite3")) as db:
            db.executescript("""CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); INSERT INTO meta VALUES('schema','1');
              CREATE TABLE rules(id TEXT PRIMARY KEY,key TEXT,scope TEXT,target TEXT,body TEXT,enabled INTEGER,
              revision INTEGER,created_at TEXT,updated_at TEXT,consent_quote TEXT,UNIQUE(key,scope,target));
              CREATE TABLE events(seq INTEGER PRIMARY KEY,rule_id TEXT REFERENCES rules(id) ON DELETE CASCADE,
              action TEXT,at TEXT,before_state TEXT,after_state TEXT);""")
            db.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?)", ("old", body["key"], "session", "s", json.dumps(body), 1, 1, "2026-01-01", "2026-01-01", ""))
            db.execute("INSERT INTO events VALUES(1,'old','record','2026-01-01',NULL,?)", (json.dumps(snapshot),))
            db.commit()
        with Memory(directory) as upgraded:
            self.assertTrue(Path(upgraded.migration_backup).exists())
            self.assertEqual(upgraded.get("old")["desired"], body["desired"])
            self.assertEqual(len(upgraded.history("old")), 1)
            self.assertEqual(upgraded.prepare("s")["count"], 1)
            self.assertEqual(upgraded.search(body["desired"])[0]["id"], "old")
            changed = upgraded.update("old", 1, rule(desired="更新"))
            self.assertEqual(upgraded.undo("old", changed["revision"])["desired"], body["desired"])
            self.assertEqual(upgraded.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_missing_update_spec_is_rejected_not_a_silent_noop(self):
        r = self.mem.record(rule(), session="s")
        with self.assertRaises(ValueError):
            self.mem.update(r["id"], 1, None)
        self.assertEqual(self.mem.get(r["id"])["revision"], 1)

    def test_same_key_cannot_change_scope_through_body(self):
        r = self.mem.record(rule(), session="s")
        with self.assertRaisesRegex(ValueError, "Unknown rule fields"):
            self.mem.update(r["id"], 1, {**rule(), "scope": "global"})
        self.assertEqual(self.mem.get(r["id"])["scope"], "session")

    def test_disabled_rules_are_visible_as_excluded(self):
        r = self.mem.record(rule(), session="s")
        self.mem.set_enabled(r["id"], 1, False)
        context = self.mem.prepare("s")
        self.assertEqual(context["count"], 0)
        self.assertEqual(context["excluded"], [{"id": r["id"], "reason": "disabled"}])

    def test_expired_receipt_is_stale(self):
        from unittest.mock import patch
        r = self.mem.record(rule(), session="s", expires_at="2030-01-01T00:00:00Z")
        run = self.mem.prepare("s")
        with patch("memory.now", return_value="2031-01-01T00:00:00+00:00"):
            with self.assertRaisesRegex(ValueError, "Stale"):
                self.mem.assess(run["run_id"], [{"rule_id": r["id"], "status": "met", "evidence": "旧要求"}])

    def test_repeated_undo_domains_collision_is_atomic(self):
        a = self.mem.record(rule(domains=["writing"]), session="s")
        a = self.mem.update(a["id"], 1, rule(domains=["coding"]))
        self.mem.record(rule(domains=["writing"]), session="s")
        with self.assertRaises(sqlite3.IntegrityError):
            self.mem.undo(a["id"], 2)
        self.assertEqual(self.mem.get(a["id"])["domains"], ["coding"])
        self.assertEqual(self.mem.get(a["id"])["revision"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

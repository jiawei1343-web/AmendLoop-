"""Task lifetimes, inspectable receipts, conservative merges and data portability."""
import difflib
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from storage import backup_db, backup_name, initialize, validate_backup


def normalized(text):
    return " ".join(str(text).casefold().split())


class Management:
    def _require_open_task(self, session, task):
        if not task:
            raise ValueError("Task scope requires a task ID from begin-task.")
        row = self.db.execute("SELECT state FROM tasks WHERE session=? AND id=?", (session, task)).fetchone()
        if not row or row[0] != "open":
            raise ValueError("Task is not open. Begin a new task rather than reopening a finished exception.")

    def begin_task(self, session=None, task=None):
        session = self._target("session", session)
        task = task or uuid.uuid4().hex[:12]
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Task ID must be a nonempty string.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute("SELECT * FROM tasks WHERE session=? AND id=?", (session, task)).fetchone()
            if existing and existing["state"] != "open":
                raise ValueError("Task already finished. Use a new task ID.")
            self.db.execute("INSERT OR IGNORE INTO tasks VALUES(?,?,'open',?,NULL)", (session, task, self._now()))
        return {"session": session, "task": task, "state": "open"}

    def finish_task(self, session, task):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM tasks WHERE session=? AND id=?", (session, task)).fetchone()
            if not row:
                raise ValueError("Task not found in this session.")
            self.db.execute("UPDATE tasks SET state='finished',closed_at=? WHERE session=? AND id=?", (self._now(), session, task))
        return {"session": session, "task": task, "state": "finished", "exceptions_active": False}

    def set_expiry(self, rule_id, revision, at):
        return self._mutate(rule_id, revision, "expiry", expires_at=at)

    def _track(self, session, task, project, domains, context):
        run_id = uuid.uuid4().hex[:16]
        session = session or ""
        with self.db:
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?, ?,NULL,NULL)", (
                run_id, session, task, project, json.dumps(sorted(domains)), self._now(), "prepared",
                json.dumps(context, ensure_ascii=False)))
        return run_id

    def receipt(self, run_id=None, session=None):
        if run_id:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        else:
            session = self._target("session", session)
            row = self.db.execute("SELECT * FROM runs WHERE session=? ORDER BY created_at DESC,id DESC LIMIT 1", (session,)).fetchone()
        if not row:
            raise ValueError("No activation receipt found. This is not evidence the skill ran.")
        result = dict(row)
        for key in ("domains", "context", "assessment"):
            if result[key] is not None:
                result[key] = json.loads(result[key])
        result["verification"] = "model_self_report" if result["assessment"] is not None else "not_assessed"
        result["draft_saved"] = False
        return result

    def assess(self, run_id, assessments):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            run = self.receipt(run_id)
            current = self.prepare(run["session"], run["project"], run["domains"], run["task"], track=False)
            if current["fingerprint"] != run["context"]["fingerprint"]:
                raise ValueError("Stale receipt: applicable rules changed or expired. Prepare and assess again.")
            expected = {r["id"] for r in current["rules"]}
            if not isinstance(assessments, list):
                raise ValueError("Assessments must be an array.")
            ids, clean = set(), []
            for item in assessments:
                if not isinstance(item, dict) or set(item) != {"rule_id", "status", "evidence"}:
                    raise ValueError("Each assessment needs exactly rule_id, status, evidence.")
                if item["rule_id"] in ids or item["rule_id"] not in expected:
                    raise ValueError("Duplicate or unexpected rule assessment.")
                if item["status"] not in ("met", "violated", "not_applicable", "uncertain"):
                    raise ValueError("Unknown assessment status.")
                if not isinstance(item["evidence"], str) or not 1 <= len(item["evidence"].strip()) <= 1500:
                    raise ValueError("Evidence must be a short, nonempty excerpt or applicability explanation.")
                ids.add(item["rule_id"])
                clean.append(item)
            if ids != expected:
                raise ValueError("Every retrieved rule needs an assessment, including not_applicable when justified.")
            stage = "no_applicable_rules" if not clean or all(a["status"] == "not_applicable" for a in clean) else "self_reported_met"
            if any(a["status"] == "uncertain" for a in clean):
                stage = "uncertain"
            if any(a["status"] == "violated" for a in clean):
                stage = "needs_revision"
            self.db.execute("UPDATE runs SET stage=?,assessment=? WHERE id=?", (stage, json.dumps(clean, ensure_ascii=False), run_id))
        return self.receipt(run_id)

    def _purge_receipts(self, rule_id):
        for row in self.db.execute("SELECT id,context FROM runs").fetchall():
            context = json.loads(row["context"])
            if any(r["id"] == rule_id for name in ("rules", "shadowed", "excluded") for r in context.get(name, [])):
                self.db.execute("DELETE FROM runs WHERE id=?", (row["id"],))
        for row in self.db.execute("SELECT id,keeper,sources FROM merge_ops").fetchall():
            if row["keeper"] == rule_id or rule_id in json.loads(row["sources"]):
                self.db.execute("DELETE FROM merge_ops WHERE id=?", (row["id"],))

    def duplicates(self, threshold=0.86):
        if not 0 <= threshold <= 1:
            raise ValueError("Threshold must be between 0 and 1.")
        candidates, rules = [], [r for r in self.list_rules() if not r["expired"]]
        for i, a in enumerate(rules):
            for b in rules[i + 1:]:
                if (a["scope"], a["target"], a["task_id"], sorted(a["domains"])) != (b["scope"], b["target"], b["task_id"], sorted(b["domains"])):
                    continue
                score = difflib.SequenceMatcher(None, normalized(a["desired"]), normalized(b["desired"])).ratio()
                if score >= threshold:
                    candidates.append({"ids": [a["id"], b["id"]], "lexical_similarity": round(score, 4),
                                       "exact_desired": normalized(a["desired"]) == normalized(b["desired"]),
                                       "requires_user_judgment": True})
        return candidates

    def merge(self, keeper_id, sources):
        """Consolidate exact duplicates; similar wording alone is never enough."""
        if not isinstance(sources, dict) or not sources or keeper_id in sources:
            raise ValueError("sources must map distinct source rule IDs to current revisions.")
        operation = uuid.uuid4().hex[:16]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            keeper = self.get(keeper_id)
            if not keeper["enabled"] or keeper["expired"]:
                raise ValueError("Keeper must be active and unexpired.")
            original, after_revisions = {}, {}
            for rule_id, revision in sources.items():
                r = self.get(rule_id)
                if r["revision"] != revision:
                    raise ValueError("Revision conflict in merge. Nothing changed.")
                fields = ("scope", "target", "task_id", "expires_at")
                if any(r[k] != keeper[k] for k in fields) or sorted(r["domains"]) != sorted(keeper["domains"]):
                    raise ValueError("Merge cannot cross scope, task, domain or expiry boundaries.")
                if any(r[k] != keeper[k] for k in ("desired", "rejected", "checks", "literal_avoid")):
                    raise ValueError("Only exact behavioral duplicates can merge. Resolve differing requirements explicitly first.")
                if not r["enabled"]:
                    raise ValueError("Source is already disabled.")
                original[rule_id] = {"enabled": r["enabled"]}
                after = self._write(r, "merge:" + operation, enabled=False)
                after_revisions[rule_id] = after["revision"]
            self.db.execute("INSERT INTO merge_ops VALUES(?,?,?,?,?,0)", (
                operation, self._now(), keeper_id, json.dumps(original), json.dumps(after_revisions)))
        return {"operation": operation, "keeper": keeper_id, "disabled_sources": after_revisions,
                "notice": "Provenance and history retained. undo-merge restores the entire merge if sources have not changed."}

    def undo_merge(self, operation):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            op = self.db.execute("SELECT * FROM merge_ops WHERE id=?", (operation,)).fetchone()
            if not op or op["reverted"]:
                raise ValueError("Merge operation not found or already undone.")
            sources, revisions = json.loads(op["sources"]), json.loads(op["after_revisions"])
            states = {i: self.get(i) for i in sources}
            if any(states[i]["revision"] != revisions[i] for i in sources):
                raise ValueError("A merged source changed. Group undo refused without changing anything.")
            restored = {i: self._write(states[i], "undo-merge:" + operation, enabled=sources[i]["enabled"])["revision"] for i in sources}
            self.db.execute("UPDATE merge_ops SET reverted=1 WHERE id=?", (operation,))
        return {"operation": operation, "restored": restored}

    def export_bundle(self, include_session=False):
        rules = [r for r in self.list_rules(True) if include_session or r["scope"] in ("global", "project")]
        return {"format": "ai-correction-memory", "version": 2, "exported_at": self._now(), "rules": rules,
                "notice": "Preference data only; no execution directives. Drafts, assessments and history not exported."}

    def import_bundle(self, bundle, apply=False):
        if not isinstance(bundle, dict) or bundle.get("format") != "ai-correction-memory" or bundle.get("version") != 2:
            raise ValueError("Unsupported export format.")
        raw_rules = bundle.get("rules")
        if not isinstance(raw_rules, list) or len(raw_rules) > 10000:
            raise ValueError("rules must be an array of at most 10000 items.")
        # Validate every item before starting any write. Imported rule text is not executed.
        clean = []
        for raw in raw_rules:
            required = {"key", "title", "desired", "evidence", "rejected", "checks", "literal_avoid", "domains", "keywords", "scope", "target", "enabled"}
            if not isinstance(raw, dict) or required - set(raw):
                raise ValueError("Imported rule is missing required fields.")
            body = self._validate({k: raw[k] for k in ("key", "title", "desired", "evidence", "rejected", "checks", "literal_avoid", "domains", "keywords")})
            scope = raw["scope"]
            if scope in ("session", "task") and (not isinstance(raw["target"], str) or not raw["target"].strip()):
                raise ValueError("Imported session/task target must be nonempty; it cannot be remapped to the current chat.")
            target = self._target(scope, raw["target"] if scope in ("session", "task") else None,
                                  raw["target"] if scope == "project" else None)
            quote = raw.get("consent_quote", "")
            if scope in ("project", "global") and (not isinstance(quote, str) or not quote.strip()):
                raise ValueError("Persistent imported rules require their original saving instruction.")
            task = raw.get("task_id", "")
            if scope == "task" and not task:
                raise ValueError("Imported task rule is missing task_id.")
            if type(raw.get("enabled")) is not bool:
                raise ValueError("Imported enabled state must be boolean.")
            clean.append((body, scope, target, quote, task, self._expiry(raw.get("expires_at")), raw["enabled"]))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            added, conflicts, seen = [], [], set()
            for body, scope, target, quote, task, at, enabled in clean:
                sig = json.dumps(sorted(body["domains"]), ensure_ascii=False)
                identity = (body["key"], scope, target, task, sig)
                exists = self.db.execute("SELECT id FROM rules WHERE key=? AND scope=? AND target=? AND task_id=? AND domain_sig=?", identity).fetchone()
                if identity in seen or exists:
                    conflicts.append({"key": body["key"], "existing_id": exists[0] if exists else None})
                    continue
                seen.add(identity)
                if apply:
                    rule_id, timestamp = uuid.uuid4().hex[:12], self._now()
                    self.db.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        rule_id, body["key"], scope, target, json.dumps(body, ensure_ascii=False), int(enabled), 1,
                        timestamp, timestamp, quote, task, at, sig, 0))
                    r = self.get(rule_id)
                    self.db.execute("INSERT INTO timeline VALUES(?,?,?)", (rule_id, 0, self._state(r)))
                    self._index(r)
                    self._event(rule_id, "import", None, r)
                    # Imported task rules stay inactive until an explicit task mapping exists.
                    added.append(rule_id)
                else:
                    added.append(body["key"])
        return {"applied": apply, "added" if apply else "would_add": added, "conflicts_skipped": conflicts,
                "overwrite": False, "notice": "Imported session/task IDs keep their original meaning; they are not remapped to this chat."}

    def backup(self, destination=None):
        path = backup_db(self.db, destination or backup_name(self.directory, "manual"))
        return {"backup": path, "includes": "rules, history, tasks, receipts", "consistent_sqlite_snapshot": True}

    def restore(self, source):
        source = Path(source).expanduser().resolve()
        if source == (self.directory / "memory.sqlite3").resolve():
            raise ValueError("Cannot restore the live database onto itself.")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as other:
            other.row_factory = sqlite3.Row
            if other.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed.")
            schema = other.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if not schema or schema[0] not in ("1", "2"):
                raise ValueError("Unsupported backup schema.")
            validate_backup(other, schema[0], self._validate, self._expiry)
            safety = self.backup(backup_name(self.directory, "before-restore"))["backup"]
            try:
                other.backup(self.db)
                self.migration_backup = initialize(self.db, self.directory)
                self.fts = bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='rule_search'").fetchone())
            except BaseException:
                with closing(sqlite3.connect(safety)) as original:
                    original.backup(self.db)
                raise
        return {"restored_from": str(source), "safety_backup": safety}

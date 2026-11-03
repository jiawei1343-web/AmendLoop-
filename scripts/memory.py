"""Local correction memory. Python standard library only; no model or network calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from storage import initialize
from management import Management

VERSION = "0.2.2"
RULE_FIELDS = {"key", "title", "desired", "rejected", "checks", "literal_avoid", "domains", "keywords", "evidence"}
LIST_FIELDS = {"rejected", "checks", "literal_avoid", "domains", "keywords"}


def now():
    return datetime.now(timezone.utc).isoformat()


def default_data_dir():
    override = os.environ.get("AI_CORRECTION_MEMORY_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        # LOCALAPPDATA may switch between per-command sandboxes and application
        # package paths. Keep one stable user-profile path; OS permissions still
        # apply. AI_CORRECTION_MEMORY_DIR supports deliberately redirected stores.
        return Path.home() / "AppData/Local/ai-correction-memory"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "ai-correction-memory"


def project_target(project):
    if not project or not Path(project).expanduser().is_absolute():
        raise ValueError("Project scope requires an absolute project path.")
    return os.path.normcase(str(Path(project).expanduser().resolve()))


def target_for(scope, session=None, project=None):
    if scope == "global":
        return "*"
    if scope == "project":
        return project_target(project)
    if scope not in ("session", "task"):
        raise ValueError("Scope must be task, session, project or global.")
    session = session or os.environ.get("CODEX_THREAD_ID")
    if not session or not str(session).strip():
        raise ValueError("Session ID required: use --session or CODEX_THREAD_ID. Never fall back to global.")
    return str(session).strip()


def validate_rule(spec):
    if not isinstance(spec, dict):
        raise ValueError("Rule must be a JSON object.")
    unknown = set(spec) - RULE_FIELDS
    if unknown:
        raise ValueError("Unknown rule fields: " + ", ".join(sorted(unknown)))
    rule = dict(spec)
    for name in ("key", "title", "desired", "evidence"):
        if not isinstance(rule.get(name), str) or not rule[name].strip():
            raise ValueError(f"{name} must be a nonempty string.")
        rule[name] = rule[name].strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", rule["key"]):
        raise ValueError("key must be 1-80 lowercase ASCII letters, numbers, dots, hyphens or underscores.")
    for name in LIST_FIELDS:
        values = rule.get(name, ["*"] if name == "domains" else [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f"{name} must be an array of nonempty strings.")
        rule[name] = list(dict.fromkeys(v.strip() for v in values))
    if not rule["domains"]:
        raise ValueError("domains must contain '*' or at least one domain.")
    if not rule["checks"]:
        raise ValueError("checks must contain at least one observable acceptance question.")
    if len(json.dumps(rule, ensure_ascii=False)) > 16000:
        raise ValueError("Rule too long. Store a minimal correction, not an entire conversation.")
    return rule


def tokens(text):
    """FTS tokens: Latin words plus CJK unigrams/bigrams (no external segmenter)."""
    text = unicodedata.normalize("NFKC", text).casefold()
    result = re.findall(r"[a-z0-9_]+", text)
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend(run)
        result.extend(run[i:i + 2] for i in range(len(run) - 1))
    # Other scripts keep their Unicode words. FTS handles them as tokens.
    result.extend(re.findall(r"[^\W\d_]+", re.sub(r"[\u3400-\u9fffA-Za-z]+", " ", text)))
    return list(dict.fromkeys(result))


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def expiry(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError("expires_at must be an ISO-8601 timestamp with a timezone.")
    if parsed.tzinfo is None:
        raise ValueError("expires_at must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat()


class Memory(Management):
    _validate = staticmethod(validate_rule)
    _now = staticmethod(now)
    _target = staticmethod(target_for)
    _expiry = staticmethod(expiry)
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else default_data_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "memory.sqlite3", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        try:
            self.migration_backup = initialize(self.db, self.directory)
        except BaseException:
            self.db.close()
            raise
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rule_search USING fts5(id UNINDEXED, terms)")
            self.fts = True
            for row in self.db.execute("SELECT * FROM rules WHERE id NOT IN (SELECT id FROM rule_search)").fetchall():
                self._index(self._decode(row))
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _decode(self, row):
        r = dict(row)
        body = json.loads(r.pop("body"))
        r.update(body)
        r["enabled"] = bool(r["enabled"])
        r["expired"] = bool(r["expires_at"] and r["expires_at"] <= now())
        r.pop("domain_sig", None)
        return r

    def get(self, rule_id):
        row = self.db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        if not row:
            raise ValueError("Rule not found: " + rule_id)
        return self._decode(row)

    def _index(self, rule):
        if self.fts:
            self.db.execute("DELETE FROM rule_search WHERE id=?", (rule["id"],))
            self.db.execute("INSERT INTO rule_search(id,terms) VALUES(?,?)", (
                rule["id"], " ".join(tokens(json.dumps({k: rule[k] for k in RULE_FIELDS}, ensure_ascii=False)))))

    def _event(self, rule_id, action, before, after):
        self.db.execute("INSERT INTO events(rule_id,action,at,before_state,after_state) VALUES(?,?,?,?,?)", (
            rule_id, action, now(), json.dumps(before, ensure_ascii=False) if before else None,
            json.dumps(after, ensure_ascii=False)))

    def record(self, spec, scope="session", session=None, project=None, consent_quote="", task=None, expires_at=None):
        body = validate_rule(spec)
        target = target_for(scope, session, project)
        if scope in ("project", "global") and not consent_quote.strip():
            raise ValueError("Persistent rules require the user's explicit saving instruction in consent_quote.")
        if scope == "task":
            self._require_open_task(target, task)
        elif task:
            raise ValueError("Use task scope when assigning a rule to a task.")
        task = task or ""
        expires_at = expiry(expires_at)
        sig = json.dumps(sorted(body["domains"]), ensure_ascii=False)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if scope == "task":
                self._require_open_task(target, task)
            existing = self.db.execute("SELECT id FROM rules WHERE key=? AND scope=? AND target=? AND task_id=? AND domain_sig=?",
                                       (body["key"], scope, target, task, sig)).fetchone()
            if existing:
                raise ValueError(f"Rule already exists: {existing[0]}. Read it, then update with its revision.")
            rule_id, at = uuid.uuid4().hex[:12], now()
            self.db.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                rule_id, body["key"], scope, target, json.dumps(body, ensure_ascii=False), 1, 1, at, at,
                consent_quote.strip(), task, expires_at, sig, 0))
            result = self.get(rule_id)
            self.db.execute("INSERT INTO timeline VALUES(?,?,?)", (rule_id, 0, self._state(result)))
            self._index(result)
            self._event(rule_id, "record", None, result)
        return result

    def _state(self, rule):
        return json.dumps({"body": {k: rule[k] for k in RULE_FIELDS}, "enabled": rule["enabled"],
                           "expires_at": rule["expires_at"]}, ensure_ascii=False)

    def _write(self, before, action, body=None, enabled=None, expires_at="unchanged"):
        body = validate_rule(body if body is not None else {k: before[k] for k in RULE_FIELDS})
        if body["key"] != before["key"]:
            raise ValueError("Rule key is immutable. Record a new rule for a different concept.")
        position = before["position"] + 1
        self.db.execute("UPDATE rules SET body=?,domain_sig=?,enabled=?,expires_at=?,revision=revision+1,updated_at=?,position=? WHERE id=?", (
            json.dumps(body, ensure_ascii=False), json.dumps(sorted(body["domains"]), ensure_ascii=False),
            int(before["enabled"] if enabled is None else enabled),
            before["expires_at"] if expires_at == "unchanged" else expiry(expires_at), now(), position, before["id"]))
        self.db.execute("DELETE FROM timeline WHERE rule_id=? AND position>?", (before["id"], before["position"]))
        after = self.get(before["id"])
        self.db.execute("INSERT INTO timeline VALUES(?,?,?)", (before["id"], position, self._state(after)))
        self._index(after)
        self._event(before["id"], action, before, after)
        return after

    def _mutate(self, rule_id, expected_revision, action, body=None, enabled=None, expires_at="unchanged"):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            before = self.get(rule_id)
            if before["revision"] != expected_revision:
                raise ValueError("Revision conflict. Read current rule before changing it.")
            after = self._write(before, action, body, enabled, expires_at)
        return after

    def update(self, rule_id, expected_revision, spec):
        validate_rule(spec)
        return self._mutate(rule_id, expected_revision, "update", body=spec)

    def set_enabled(self, rule_id, expected_revision, enabled):
        return self._mutate(rule_id, expected_revision, "enable" if enabled else "disable", enabled=enabled)

    def undo(self, rule_id, expected_revision):
        return self._travel(rule_id, expected_revision, -1)

    def redo(self, rule_id, expected_revision):
        return self._travel(rule_id, expected_revision, 1)

    def _travel(self, rule_id, expected_revision, step):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            before = self.get(rule_id)
            if before["revision"] != expected_revision:
                raise ValueError("Revision conflict. Read current rule before changing it.")
            position = before["position"] + step
            saved = self.db.execute("SELECT state FROM timeline WHERE rule_id=? AND position=?", (rule_id, position)).fetchone()
            if not saved:
                raise ValueError("Creation cannot be undone; disable or forget it." if step < 0 else "No redo state available.")
            state = json.loads(saved[0])
            body = state["body"]
            self.db.execute("UPDATE rules SET body=?,domain_sig=?,enabled=?,expires_at=?,revision=revision+1,updated_at=?,position=? WHERE id=?", (
                json.dumps(body, ensure_ascii=False), json.dumps(sorted(body["domains"]), ensure_ascii=False),
                int(state["enabled"]), state["expires_at"], now(), position, rule_id))
            after = self.get(rule_id)
            self._index(after)
            self._event(rule_id, "undo" if step < 0 else "redo", before, after)
        return after

    def forget(self, rule_id, expected_revision):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rule = self.get(rule_id)
            if rule["revision"] != expected_revision:
                raise ValueError("Revision conflict. Read current rule before changing it.")
            if self.fts:
                self.db.execute("DELETE FROM rule_search WHERE id=?", (rule_id,))
            self._purge_receipts(rule_id)
            self.db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        return {"forgotten": rule_id, "history_deleted": True, "undo_available": False}

    def clear_session(self, session=None):
        target = target_for("session", session)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            ids = [r[0] for r in self.db.execute("SELECT id FROM rules WHERE scope IN ('session','task') AND target=?", (target,))]
            for rule_id in ids:
                if self.fts:
                    self.db.execute("DELETE FROM rule_search WHERE id=?", (rule_id,))
                self._purge_receipts(rule_id)
                self.db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            self.db.execute("DELETE FROM tasks WHERE session=?", (target,))
            self.db.execute("DELETE FROM runs WHERE session=?", (target,))
        return {"cleared_session": target, "count": len(ids)}

    def list_rules(self, include_disabled=False):
        sql = "SELECT * FROM rules" + ("" if include_disabled else " WHERE enabled=1") + " ORDER BY updated_at DESC,id"
        return [self._decode(r) for r in self.db.execute(sql)]

    def history(self, rule_id):
        self.get(rule_id)
        return [{**dict(r), "before_state": json.loads(r["before_state"]) if r["before_state"] else None,
                 "after_state": json.loads(r["after_state"])}
                for r in self.db.execute("SELECT * FROM events WHERE rule_id=? ORDER BY seq", (rule_id,))]

    def prepare(self, session=None, project=None, domains=None, task=None, track=True):
        session = session or os.environ.get("CODEX_THREAD_ID")
        project = project_target(project) if project else None
        domains = set(domains or [])
        applicable, excluded = [], []
        for rule in self.list_rules(True):
            if rule["scope"] in ("session", "task") and rule["target"] != session:
                continue
            if rule["scope"] == "project" and rule["target"] != project:
                continue
            reason = None
            if not rule["enabled"]:
                reason = "disabled"
            if rule["scope"] == "task":
                state = self.db.execute("SELECT state FROM tasks WHERE session=? AND id=?", (session, rule["task_id"])).fetchone()
                if rule["task_id"] != task or not state or state[0] != "open":
                    reason = "task_not_active"
            if rule["expired"]:
                reason = "expired"
            if reason:
                excluded.append({"id": rule["id"], "reason": reason})
                continue
            if domains and "*" not in rule["domains"] and not domains.intersection(rule["domains"]):
                continue
            applicable.append(rule)
        precedence = {"task": 4, "session": 3, "project": 2, "global": 1}
        buckets = domains or ({"*"} | {d for r in applicable for d in r["domains"] if d != "*"})
        selected_by_id, winners, defeated = {}, {}, {}
        for key in sorted({r["key"] for r in applicable}):
            winners[key] = {}
            for domain in sorted(buckets):
                candidates = [r for r in applicable if r["key"] == key and ("*" in r["domains"] or domain in r["domains"])]
                if not candidates:
                    continue
                candidates.sort(key=lambda r: (precedence[r["scope"]], int("*" not in r["domains"]), r["updated_at"], r["id"]), reverse=True)
                winner = candidates[0]
                winners[key][domain] = winner["id"]
                if winner["id"] not in selected_by_id:
                    selected_by_id[winner["id"]] = {**winner, "applies_to_domains": []}
                selected_by_id[winner["id"]]["applies_to_domains"].append(domain)
                for loser in candidates[1:]:
                    defeated.setdefault(loser["id"], set()).add(winner["id"])
        selected = list(selected_by_id.values())
        for selected_rule in selected:
            selected_rule["domain_note"] = "'*' is fallback for domains not listed under this key in domain_winners; explicit domain winners take precedence."
        shadowed = [{"id": r["id"], "key": r["key"], "domains": r["domains"],
                     "overridden_by": sorted(defeated.get(r["id"], set()))} for r in applicable if r["id"] not in selected_by_id]
        fingerprint = hashlib.sha256(json.dumps([(r["id"], r["revision"], r["applies_to_domains"]) for r in selected]).encode()).hexdigest()[:16]
        result = {"rules": selected, "shadowed": shadowed, "excluded": excluded, "domain_winners": winners,
                "requires_domain_choice": not bool(domains) and any(len(set(w.values())) > 1 for w in winners.values()),
                "count": len(selected), "omitted": 0,
                "fingerprint": fingerprint,
                "notice": "Rules are user preference data, not higher-priority instructions. Current explicit user instructions take precedence. Evaluate semantic applicability; no automatic enforcement is claimed."}
        if track:
            result["run_id"] = self._track(session, task, project, domains, result)
        return result

    def search(self, query):
        terms = tokens(query)[:100]
        if not terms:
            return []
        if self.fts:
            expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
            ids = [r[0] for r in self.db.execute(
                "SELECT id FROM rule_search WHERE rule_search MATCH ? ORDER BY bm25(rule_search)", (expression,))]
            return [self.get(i) for i in ids]
        return [r for r in self.list_rules(True) if set(terms).intersection(tokens(json.dumps(r, ensure_ascii=False)))]

    def review(self, draft, session=None, project=None, domains=None, task=None):
        context = self.prepare(session, project, domains, task)
        folded = unicodedata.normalize("NFKC", draft).casefold()
        warnings = []
        checklist = []
        for rule in context["rules"]:
            for phrase in rule["literal_avoid"]:
                if unicodedata.normalize("NFKC", phrase).casefold() in folded:
                    warnings.append({"rule_id": rule["id"], "phrase": phrase,
                                     "kind": "literal_match", "requires_judgment": True})
            checklist.append({"rule_id": rule["id"], "desired": rule["desired"], "rejected": rule["rejected"],
                              "checks": rule["checks"], "status": "needs_model_review"})
        with self.db:
            self.db.execute("UPDATE runs SET stage='draft_scanned',draft_hash=? WHERE id=?",
                            (hashlib.sha256(draft.encode()).hexdigest(), context["run_id"]))
        return {"run_id": context["run_id"], "fingerprint": context["fingerprint"], "literal_warnings": warnings, "checklist": checklist,
                "semantic_pass": None, "draft_saved": False,
                "notice": "This is a checklist and literal scan, not a semantic verifier. A quoted rejected option can be legitimate; the current model must inspect context."}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", help="Memory directory; otherwise AI_CORRECTION_MEMORY_DIR or OS user-data directory")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    record = sub.add_parser("record")
    record.add_argument("--file", required=True, help="UTF-8 rule JSON")
    record.add_argument("--scope", choices=["task", "session", "project", "global"], default="session")
    record.add_argument("--consent-quote", default="")
    record.add_argument("--expires-at")
    for name in ("record", "prepare", "review"):
        q = record if name == "record" else sub.add_parser(name)
        q.add_argument("--session")
        q.add_argument("--project")
        q.add_argument("--task")
        if name != "record":
            q.add_argument("--domain", action="append", default=[])
        if name == "review":
            q.add_argument("--draft-file", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    search = sub.add_parser("search")
    search.add_argument("query")
    for name in ("get", "history", "update", "enable", "disable", "undo", "redo", "forget", "set-expiry"):
        q = sub.add_parser(name)
        q.add_argument("id")
        if name not in ("get", "history"):
            q.add_argument("--revision", type=int, required=True)
        if name == "update":
            q.add_argument("--file", required=True)
        if name == "set-expiry":
            q.add_argument("--at", default="", help="ISO timestamp; empty clears expiry")
    clear = sub.add_parser("clear-session")
    clear.add_argument("--session")
    for name in ("begin-task", "finish-task"):
        q = sub.add_parser(name)
        q.add_argument("--session")
        q.add_argument("--task", required=name == "finish-task")
    receipt = sub.add_parser("receipt")
    receipt.add_argument("--run")
    receipt.add_argument("--session")
    assess = sub.add_parser("assess")
    assess.add_argument("--run", required=True)
    assess.add_argument("--file", required=True)
    duplicate = sub.add_parser("duplicates")
    duplicate.add_argument("--threshold", type=float, default=0.86)
    merge = sub.add_parser("merge")
    merge.add_argument("keeper")
    merge.add_argument("--sources-file", required=True, help="JSON object mapping source IDs to current revisions")
    undo_merge = sub.add_parser("undo-merge")
    undo_merge.add_argument("operation")
    for name in ("backup", "restore", "export", "import"):
        q = sub.add_parser(name)
        q.add_argument("--file", required=name != "backup")
        if name == "export":
            q.add_argument("--include-session", action="store_true")
        if name == "import":
            q.add_argument("--apply", action="store_true", help="Without this flag, preview only")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        with Memory(args.data_dir) as mem:
            cmd = args.command
            if cmd == "status":
                result = {"version": VERSION, "database": str(mem.directory.resolve() / "memory.sqlite3"),
                          "sqlite": sqlite3.sqlite_version, "fts5": mem.fts,
                          "active_rules": len(mem.list_rules()), "network": False,
                          "session_id_available": bool(os.environ.get("CODEX_THREAD_ID")),
                          "migration_backup": mem.migration_backup, "schema": 2}
            elif cmd == "record":
                result = mem.record(read_json(args.file), args.scope, args.session, args.project, args.consent_quote, args.task, args.expires_at)
            elif cmd == "prepare":
                result = mem.prepare(args.session, args.project, args.domain, args.task)
            elif cmd == "review":
                result = mem.review(Path(args.draft_file).read_text(encoding="utf-8-sig"), args.session, args.project, args.domain, args.task)
            elif cmd == "list":
                result = mem.list_rules(args.all)
            elif cmd == "search":
                result = mem.search(args.query)
            elif cmd == "get":
                result = mem.get(args.id)
            elif cmd == "history":
                result = mem.history(args.id)
            elif cmd == "update":
                result = mem.update(args.id, args.revision, read_json(args.file))
            elif cmd in ("enable", "disable"):
                result = mem.set_enabled(args.id, args.revision, cmd == "enable")
            elif cmd == "undo":
                result = mem.undo(args.id, args.revision)
            elif cmd == "redo":
                result = mem.redo(args.id, args.revision)
            elif cmd == "set-expiry":
                result = mem.set_expiry(args.id, args.revision, args.at)
            elif cmd == "forget":
                result = mem.forget(args.id, args.revision)
            elif cmd == "clear-session":
                result = mem.clear_session(args.session)
            elif cmd == "begin-task":
                result = mem.begin_task(args.session, args.task)
            elif cmd == "finish-task":
                result = mem.finish_task(target_for("session", args.session), args.task)
            elif cmd == "receipt":
                result = mem.receipt(args.run, args.session)
            elif cmd == "assess":
                result = mem.assess(args.run, read_json(args.file))
            elif cmd == "duplicates":
                result = mem.duplicates(args.threshold)
            elif cmd == "merge":
                result = mem.merge(args.keeper, read_json(args.sources_file))
            elif cmd == "undo-merge":
                result = mem.undo_merge(args.operation)
            elif cmd == "backup":
                result = mem.backup(args.file)
            elif cmd == "restore":
                result = mem.restore(args.file)
            elif cmd == "export":
                path = Path(args.file)
                with path.open("x", encoding="utf-8") as output:
                    json.dump(mem.export_bundle(args.include_session), output, ensure_ascii=False, indent=2)
                result = {"exported": str(path.resolve())}
            elif cmd == "import":
                result = mem.import_bundle(read_json(args.file), args.apply)
            emit(result)
            return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        emit({"error": str(error)})
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

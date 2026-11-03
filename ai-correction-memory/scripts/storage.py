"""Schema v2, transactional migration and online SQLite backups."""
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "2"
RULE_SQL = """CREATE TABLE rules(
 id TEXT PRIMARY KEY, key TEXT NOT NULL, scope TEXT NOT NULL, target TEXT NOT NULL,
 body TEXT NOT NULL, enabled INTEGER NOT NULL, revision INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, consent_quote TEXT NOT NULL,
 task_id TEXT NOT NULL DEFAULT '', expires_at TEXT, domain_sig TEXT NOT NULL,
 position INTEGER NOT NULL DEFAULT 0,
 UNIQUE(key,scope,target,task_id,domain_sig))"""


def backup_db(db, destination):
    path = Path(destination).expanduser().resolve()
    if path.exists():
        raise ValueError("Backup destination already exists; it was not overwritten.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as other:
        db.backup(other)
    return str(path)


def backup_name(directory, prefix):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return Path(directory) / "backups" / f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"


def state_from_row(row):
    return {"body": json.loads(row["body"]), "enabled": bool(row["enabled"]),
            "expires_at": row["expires_at"]}


def validate_backup(db, version, validate_rule, validate_expiry):
    """Validate even empty schemas and every undo state before touching live data."""
    common = {
        "meta": {"key", "value"},
        "rules": {"id", "key", "scope", "target", "body", "enabled", "revision", "created_at", "updated_at", "consent_quote"},
        "events": {"seq", "rule_id", "action", "at", "before_state", "after_state"},
    }
    if version == "2":
        common["rules"] |= {"task_id", "expires_at", "domain_sig", "position"}
        common.update(timeline={"rule_id", "position", "state"},
                      tasks={"session", "id", "state", "created_at", "closed_at"},
                      runs={"id", "session", "task", "project", "domains", "created_at", "stage", "context", "assessment", "draft_hash"},
                      merge_ops={"id", "created_at", "keeper", "sources", "after_revisions", "reverted"})
    for table, columns in common.items():
        actual = {r["name"] for r in db.execute("PRAGMA table_info(" + table + ")")}
        if columns != actual:
            raise ValueError("Backup schema mismatch: " + table)
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Backup has broken references.")
    rules = {r["id"]: dict(r) for r in db.execute("SELECT * FROM rules")}
    def state(raw, rule_id):
        obj = json.loads(raw)
        if not isinstance(obj, dict) or set(obj) != {"body", "enabled", "expires_at"} or type(obj["enabled"]) is not bool:
            raise ValueError("Backup has invalid undo state.")
        body = validate_rule(obj["body"])
        if body["key"] != rules[rule_id]["key"]:
            raise ValueError("Backup undo state changes an immutable key.")
        validate_expiry(obj["expires_at"])
        return obj
    try:
        for r in rules.values():
            body = validate_rule(json.loads(r["body"]))
            if body["key"] != r["key"] or not r["id"] or r["scope"] not in ("task", "session", "project", "global") or r["enabled"] not in (0, 1) or not isinstance(r["revision"], int) or r["revision"] < 1:
                raise ValueError("Backup has invalid rule metadata.")
            if not isinstance(r["target"], str) or not r["target"].strip() or (r["scope"] == "global" and r["target"] != "*"):
                raise ValueError("Backup has invalid scope target.")
            if version == "2":
                validate_expiry(r["expires_at"])
                if r["domain_sig"] != json.dumps(sorted(body["domains"]), ensure_ascii=False):
                    raise ValueError("Backup has invalid domain identity.")
                cursor = db.execute("SELECT state FROM timeline WHERE rule_id=? AND position=?", (r["id"], r["position"])).fetchone()
                if not cursor:
                    raise ValueError("Backup has a broken undo cursor.")
                current = state(cursor[0], r["id"])
                if current != {"body": body, "enabled": bool(r["enabled"]), "expires_at": r["expires_at"]}:
                    raise ValueError("Backup undo cursor disagrees with live state.")
        for e in db.execute("SELECT * FROM events"):
            if e["rule_id"] not in rules:
                raise ValueError("Backup contains orphaned history.")
            for field in ("before_state", "after_state"):
                if e[field] is not None:
                    if not isinstance(json.loads(e[field]), dict):
                        raise ValueError("Backup history must contain objects.")
        if version == "2":
            for t in db.execute("SELECT * FROM timeline"):
                if t["rule_id"] not in rules or not isinstance(t["position"], int) or t["position"] < 0:
                    raise ValueError("Backup has an invalid timeline.")
                state(t["state"], t["rule_id"])
            for run in db.execute("SELECT * FROM runs"):
                if not isinstance(json.loads(run["context"]), dict) or not isinstance(json.loads(run["domains"]), list):
                    raise ValueError("Backup has invalid receipt JSON.")
                if run["assessment"] is not None and not isinstance(json.loads(run["assessment"]), list):
                    raise ValueError("Backup has invalid assessment JSON.")
            for operation in db.execute("SELECT * FROM merge_ops"):
                if not isinstance(json.loads(operation["sources"]), dict) or not isinstance(json.loads(operation["after_revisions"]), dict):
                    raise ValueError("Backup has invalid merge JSON.")
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Backup contains malformed state data: " + str(error)) from error


def initialize(db, directory):
    existing = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    schema = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone() if existing else None
    version = schema[0] if schema else None
    if version not in (None, "1", SCHEMA):
        raise ValueError("Unsupported database schema. Use the matching package version.")
    migration_backup = None
    if version == "1":
        migration_backup = backup_db(db, backup_name(directory, "before-v2"))
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        # Another process may have migrated while this connection waited for the
        # write lock. Never perform a cached v1 migration twice.
        exists_now = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        current = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone() if exists_now else None
        version = current[0] if current else None
        if version not in (None, "1", SCHEMA):
            raise ValueError("Database schema changed to an unsupported version while waiting for the lock.")
        db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        if version == "1":
            old = [dict(r) for r in db.execute("SELECT * FROM rules")]
            db.execute("ALTER TABLE rules RENAME TO rules_v1")
            # SQLite rewrites the old events FK during the rename. Rebuild it too.
            events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY seq")]
            db.execute("DROP TABLE events")
            db.execute(RULE_SQL)
            for r in old:
                sig = json.dumps(sorted(json.loads(r["body"])["domains"]), ensure_ascii=False)
                db.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    r["id"], r["key"], r["scope"], r["target"], r["body"], r["enabled"],
                    r["revision"], r["created_at"], r["updated_at"], r["consent_quote"], "", None, sig, 0))
            db.execute("DROP TABLE rules_v1")
        elif version is None:
            db.execute(RULE_SQL)
        db.execute("""CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,rule_id TEXT NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
          action TEXT NOT NULL,at TEXT NOT NULL,before_state TEXT,after_state TEXT NOT NULL)""")
        if version == "1":
            for e in events:
                db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", tuple(e[k] for k in (
                    "seq", "rule_id", "action", "at", "before_state", "after_state")))
        for sql in (
            """CREATE TABLE IF NOT EXISTS timeline(rule_id TEXT REFERENCES rules(id) ON DELETE CASCADE,
               position INTEGER,state TEXT NOT NULL,PRIMARY KEY(rule_id,position))""",
            """CREATE TABLE IF NOT EXISTS tasks(session TEXT,id TEXT,state TEXT NOT NULL,
               created_at TEXT NOT NULL,closed_at TEXT,PRIMARY KEY(session,id))""",
            """CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session TEXT NOT NULL,task TEXT,
               project TEXT,domains TEXT NOT NULL,created_at TEXT NOT NULL,stage TEXT NOT NULL,
               context TEXT NOT NULL,assessment TEXT,draft_hash TEXT)""",
            """CREATE TABLE IF NOT EXISTS merge_ops(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,
               keeper TEXT NOT NULL,sources TEXT NOT NULL,after_revisions TEXT NOT NULL,reverted INTEGER NOT NULL DEFAULT 0)""",
        ):
            db.execute(sql)
        if version == "1":
            for row in db.execute("SELECT * FROM rules").fetchall():
                # Preserve all legacy events for provenance. Reconstruct its chronological
                # states; legacy undo/redo toggles are historical changes, not hidden stacks.
                history = [json.loads(e[0]) for e in db.execute(
                    "SELECT after_state FROM events WHERE rule_id=? ORDER BY seq", (row["id"],))]
                states = [{"body": {k: s[k] for k in json.loads(row["body"])},
                           "enabled": s["enabled"], "expires_at": None} for s in history]
                if not states:
                    states = [state_from_row(row)]
                for i, state in enumerate(states):
                    db.execute("INSERT INTO timeline VALUES(?,?,?)", (row["id"], i, json.dumps(state, ensure_ascii=False)))
                db.execute("UPDATE rules SET position=? WHERE id=?", (len(states) - 1, row["id"]))
        db.execute("INSERT OR REPLACE INTO meta VALUES('schema',?)", (SCHEMA,))
        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError("Migration would break foreign-key integrity; no change committed.")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")
    return migration_backup

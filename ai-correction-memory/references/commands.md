# Local commands

All examples use `python`; substitute the available Python 3.10+ executable.
Use the absolute path of `scripts/memory.py` when not in this skill folder.
All commands return JSON; errors exit with code 2. Put `--data-dir DIR` before the subcommand.

```text
python scripts/memory.py status
python scripts/memory.py prepare --session CHAT_ID --project ABSOLUTE_PROJECT_ROOT
python scripts/memory.py record --file RULE.json --session CHAT_ID
python scripts/memory.py record --file RULE.json --scope global --consent-quote "以后都先给我结论"
python scripts/memory.py record --file RULE.json --scope project --project ABSOLUTE_PROJECT_ROOT --consent-quote "这个项目以后都这样"
python scripts/memory.py list --all
python scripts/memory.py search "结论"
python scripts/memory.py get RULE_ID
python scripts/memory.py update RULE_ID --revision 1 --file REPLACEMENT.json
python scripts/memory.py disable RULE_ID --revision 2
python scripts/memory.py undo RULE_ID --revision 3
python scripts/memory.py redo RULE_ID --revision 4
python scripts/memory.py history RULE_ID
python scripts/memory.py forget RULE_ID --revision 4
python scripts/memory.py clear-session --session CHAT_ID
python scripts/memory.py review --draft-file DRAFT.txt --session CHAT_ID
python scripts/memory.py begin-task --session CHAT_ID
python scripts/memory.py record --file RULE.json --scope task --session CHAT_ID --task TASK_ID
python scripts/memory.py prepare --session CHAT_ID --task TASK_ID
python scripts/memory.py assess --run RUN_ID --file ASSESSMENTS.json
python scripts/memory.py receipt --run RUN_ID
python scripts/memory.py finish-task --session CHAT_ID --task TASK_ID
python scripts/memory.py set-expiry RULE_ID --revision 1 --at 2026-12-01T00:00:00+08:00
python scripts/memory.py duplicates
python scripts/memory.py merge KEEPER_ID --sources-file SOURCES.json
python scripts/memory.py undo-merge OPERATION_ID
python scripts/memory.py backup --file SNAPSHOT.sqlite3
python scripts/memory.py restore --file SNAPSHOT.sqlite3
python scripts/memory.py export --file RULES.json
python scripts/memory.py import --file RULES.json
python scripts/memory.py import --file RULES.json --apply
```

Read the revision returned by each mutation, rather than assuming example numbers.
On hosts that set `CODEX_THREAD_ID`, `--session` can be omitted. The MCP adapter
requires an explicit `session` argument because one server may serve multiple chats.

## Rule JSON

```json
{
  "key": "writing.answer-order",
  "title": "先说结论",
  "desired": "回答时先给出结论，再补充必要依据。",
  "rejected": ["先铺陈很长的背景，末尾才回答问题"],
  "checks": ["第一段是否直接回答用户的问题？"],
  "literal_avoid": [],
  "domains": ["*"],
  "keywords": ["结论", "回答顺序"],
  "evidence": "以后都先给我结论"
}
```

`desired` and `checks` are semantic constraints for the current model, not executable code.
`rejected` describes rejected substitutes, not a list of banned words.
Only `literal_avoid` drives the optional literal scan; it still cannot determine context.
`domains` defaults to `["*"]`; use specific domains such as `writing`, `learning`,
`coding`, `office`, `video` only when justified by the correction. It is not an ontology.
Search uses SQLite FTS5 with local CJK unigram/bigram and Unicode word tokens; it is
lexical, not embeddings or semantic similarity. `prepare` does not rely on search
ranking, so low lexical similarity cannot silently drop an applicable scoped rule.

The caller must classify corrections from actual human instructions. A nonempty
`consent_quote` is a required provenance field, not cryptographic proof of consent.
The program cannot inspect the conversation to independently verify its truth.

## Storage and scope

Default Windows: `%USERPROFILE%/AppData/Local/ai-correction-memory/memory.sqlite3`.
The user-profile path is intentional: per-command sandbox/app-package LOCALAPPDATA
redirection must not silently create a different store. Use the override below
for intentionally redirected or shared data locations.
Linux/macOS: `$XDG_DATA_HOME/ai-correction-memory` or `~/.local/share/ai-correction-memory`.
Override with `AI_CORRECTION_MEMORY_DIR` (shared by CLI and MCP), or CLI `--data-dir`.
No network request or external model is used by the application.

Task/session rules are disk-backed and remain until cleared; scope isolates their use,
not their storage lifetime. `forget` removes a rule from active tables, search and
history. This is logical deletion, not guaranteed forensic erasure of journals,
filesystem snapshots, exported files or backups. Rules are stored unencrypted in
your OS account; avoid copying secrets or unnecessary personal information into them.

## v0.2 lifecycle and receipts

`begin-task` returns a task ID. Task-scoped rules apply only with the same session,
same task, and open task state. Call `finish-task` immediately before task delivery.
Reopening a finished task is rejected; create a fresh ID for the next request.
Expired rules remain inspectable but are excluded from preparation. Timezones are
required for expiry; empty `--at` clears it. Expiry changes are undoable.

Without domain filters, `prepare` returns every domain's winner and the wildcard
fallback; an unrelated domain cannot hide another. `applies_to_domains` and
`domain_winners` tell the model where each variant wins. A `*` winner is the fallback
for other domains, not a replacement for an explicit domain winner. With filters,
scope precedence is resolved separately within each requested domain. No rule count
limit or silent truncation is imposed; large stores should use explicit domains.

`prepare` returns `run_id` and persists a snapshot (including preference text).
`review` additionally scans a draft, stores its SHA-256 hash but not its text.
Assessments contain a short answer excerpt or applicability explanation and are
stored until their session is cleared or an associated rule is forgotten.

ASSESSMENTS.json:

```json
[
  {"rule_id": "RULE_ID", "status": "met", "evidence": "第一段：可以，已完成。"}
]
```

Every retrieved rule needs exactly one item. Other statuses are `violated`,
`not_applicable`, `uncertain`. Rules that changed, expired or stopped applying make
the receipt stale; prepare again before assessing. Stages distinguish `prepared`,
`draft_scanned`, `self_reported_met`, `needs_revision`, `uncertain`,
`no_applicable_rules`. A self-report is not an independent verification or a runtime
gate. The tool cannot prove the model's evidence is truthful or exhaustive.

`undo` moves back through states; `redo` moves forward. Editing after undo removes
the abandoned redo branch while audit events remain. A same-key/domain collision
when restoring a state is rejected atomically. Creation itself isn't undone;
disable or forget the rule explicitly.

SOURCES.json is a JSON object such as `{"SOURCE_ID": 2}` mapping sources to current
revisions. `merge` only accepts identical desired/rejected/check/literal behavior
within the same scope, target, task, domain set and expiry. Source rules are disabled,
not deleted; their quotes/history remain. `undo-merge` checks every source revision
and restores all as one transaction. Similarity suggestions alone never trigger it.

`export` excludes session/task rules unless `--include-session` is supplied. It
exports preferences, not history or receipts. `import` validates all inputs first,
previews by default, and skips existing identities instead of overwriting them.
Importing preferences is a human management action; stored text is preference data,
not higher-priority instructions. Original session/task IDs are not remapped.

`backup` creates a consistent SQLite online snapshot including WAL state; existing
destinations are refused. Stop other clients before a full `restore`. Restore checks
integrity, schema and references, makes a safety backup, and rolls back if migration
fails. Schema v1 is backed up and migrated automatically on opening with v0.2.

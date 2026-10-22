"""SQLite storage layer. Single local file, WAL mode, no server.

Task ids are PER-PROJECT (dense-ish allocation via max+1): the primary key
is (project, id). Evidence is one row per entry in the shared `evidence`
table. Databases from older layouts (global autoincrement ids, evidence as
a text blob on tasks) are migrated in place on first open.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    project     TEXT NOT NULL,
    id          INTEGER NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'backlog',
    description TEXT NOT NULL DEFAULT '',
    blockers    TEXT NOT NULL DEFAULT '[]',   -- JSON array of free-form strings (external blockers)
    depends_on  TEXT NOT NULL DEFAULT '[]',   -- JSON array of task ids (hard dependencies)
    affects     TEXT NOT NULL DEFAULT '[]',   -- JSON array of task ids (informational links)
    owner       TEXT NOT NULL DEFAULT '',     -- current claim holder ('' = unclaimed)
    claimed_at  TEXT NOT NULL DEFAULT '',     -- when the current claim was taken
    priority    INTEGER NOT NULL DEFAULT 2,   -- 0=P0 (highest) .. 3=P3
    type        TEXT NOT NULL DEFAULT 'task', -- task|feature|bugfix|improvement|chore
    tags        TEXT NOT NULL DEFAULT '[]',   -- JSON array of strings (workstream/labels)
    branch      TEXT NOT NULL DEFAULT '',     -- git branch this task is being worked on
    parent_id   INTEGER NOT NULL DEFAULT 0,   -- story this task belongs to (0 = none)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (project, id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project, status);

CREATE TABLE IF NOT EXISTS evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    task_id     INTEGER NOT NULL,
    ts          TEXT NOT NULL,                -- when this entry was recorded
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence(project, task_id, id);

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    task_id     INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    relpath     TEXT NOT NULL,                -- relative to the attachments dir next to the db
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_task ON attachments(project, task_id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    task_id     INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,                -- status | claim | release | handoff
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(project, task_id);
"""

# columns added after initial release; older databases are migrated in place
_MIGRATIONS = (
    ("tasks", "owner", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "claimed_at", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "priority", "INTEGER NOT NULL DEFAULT 2"),
    ("tasks", "tags", "TEXT NOT NULL DEFAULT '[]'"),
    ("tasks", "type", "TEXT NOT NULL DEFAULT 'task'"),
    ("tasks", "parent_id", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "branch", "TEXT NOT NULL DEFAULT ''"),
)

_TS_MARK = re.compile(r"^\[([0-9]{4}-[0-9]{2}-[0-9]{2}T[^\]]+)\]\s?")


def parse_evidence_blob(blob: str, fallback_ts: str) -> list[tuple[str, str]]:
    """Split a legacy evidence blob into (ts, text) entries, preserving order.

    Entries appended by the tool look like '\\n\\n[ISO] text'. Chunks without a
    leading [ISO] stamp (manual edits) keep their position and take fallback_ts.
    """
    blob = (blob or "").strip()
    if not blob:
        return []
    entries: list[tuple[str, str]] = []
    for chunk in re.split(r"\n\n+", blob):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = _TS_MARK.match(chunk)
        if match:
            entries.append((match.group(1), chunk[match.end():].strip()))
        else:
            entries.append((fallback_ts, chunk))
    return entries


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in sorted((r for r in rows if r["pk"]), key=lambda r: r["pk"])]


def _migrate_evidence_blob(conn: sqlite3.Connection) -> None:
    """One-time: reprocess the legacy tasks.evidence text blob into evidence
    rows (timestamps extracted from the '[ISO] text' convention), then drop
    the column. Idempotent — skipped when the column is already gone."""
    if "evidence" not in _columns(conn, "tasks"):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT project, id, evidence, updated_at, created_at FROM tasks"
        ).fetchall()
        for row in rows:
            fallback = row["updated_at"] or row["created_at"] or ""
            for ts, text in parse_evidence_blob(row["evidence"], fallback):
                conn.execute(
                    "INSERT INTO evidence (project, task_id, ts, text) VALUES (?,?,?,?)",
                    (row["project"], row["id"], ts, text),
                )
        conn.execute("ALTER TABLE tasks DROP COLUMN evidence")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_legacy_layout(conn: sqlite3.Connection) -> None:
    """Convert the legacy global-autoincrement layout to the composite
    (project, id) primary key, preserving every existing id."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE tasks RENAME TO tasks_legacy")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO tasks (project, id, name, status, description, blockers,"
            " depends_on, affects, owner, claimed_at, priority, type, tags,"
            " created_at, updated_at)"
            " SELECT project, id, name, status, description, blockers,"
            " depends_on, affects, owner, claimed_at, priority, type, tags,"
            " created_at, updated_at FROM tasks_legacy"
        )
        conn.execute("DROP TABLE tasks_legacy")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='tasks'")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    fresh = len(conn.execute("PRAGMA table_info(tasks)").fetchall()) == 0
    conn.executescript(SCHEMA)
    if not fresh:
        # add missing columns BEFORE the layout migration so the legacy
        # table has everything the copy needs
        existing = _columns(conn, "tasks")
        for table, column, decl in _MIGRATIONS:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
        _migrate_evidence_blob(conn)
        if _pk_columns(conn, "tasks") == ["id"]:
            _migrate_legacy_layout(conn)  # legacy layout: composite pk, ids preserved
    else:
        existing = _columns(conn, "tasks")
        for table, column, decl in _MIGRATIONS:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
    return conn

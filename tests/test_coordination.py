"""Tests for atomic claims, priorities/tags, export/import, and attachments."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agenttasker.core import (
    EXPORT_FORMAT,
    EXPORT_VERSION,
    Store,
    TaskError,
    claim_age_hours,
    normalize_priority,
    normalize_type,
)


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.task = self.store.add_task("demo", "work item", status="todo")

    def test_claim_is_atomic_and_moves_to_in_progress(self):
        claimed = self.store.claim(self.task, "agent-a")
        self.assertEqual(claimed.owner, "agent-a")
        self.assertEqual(claimed.status, "in_progress")
        self.assertIsNotNone(claim_age_hours(claimed))

        # second agent, separate connection, must be refused
        with Store(Path(self.tmp.name) / "tasks.db") as other:
            fresh = other.get("demo", self.task.id)
            with self.assertRaises(TaskError) as ctx:
                other.claim(fresh, "agent-b")
            self.assertIn("agent-a", str(ctx.exception))

    def test_reclaim_by_same_owner_is_idempotent(self):
        self.store.claim(self.task, "agent-a")
        again = self.store.claim(self.task, "agent-a")
        self.assertEqual(again.owner, "agent-a")

    def test_force_takes_claim(self):
        self.store.claim(self.task, "agent-a")
        taken = self.store.claim(self.task, "agent-b", force=True)
        self.assertEqual(taken.owner, "agent-b")

    def test_release_requires_matching_owner(self):
        self.store.claim(self.task, "agent-a")
        with self.assertRaises(TaskError):
            self.store.release(self.task, owner="agent-b")
        released = self.store.release(self.task, owner="agent-a")
        self.assertEqual(released.owner, "")

    def test_handoff_guards_holder(self):
        self.store.claim(self.task, "agent-a")
        with self.assertRaises(TaskError):
            self.store.handoff(self.task, "agent-c", from_owner="agent-b")
        moved = self.store.handoff(self.task, "agent-c", from_owner="agent-a")
        self.assertEqual(moved.owner, "agent-c")

    def test_append_evidence_does_not_clobber(self):
        first = self.store.append_evidence(self.task, "first finding")
        # simulate a second writer working from a stale snapshot
        second = self.store.append_evidence(first, "second finding")
        entries = [e["text"] for e in self.store.evidence(second)]
        self.assertIn("first finding", entries)
        self.assertIn("second finding", entries)

class PriorityTagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_normalize_priority(self):
        self.assertEqual(normalize_priority("P0"), 0)
        self.assertEqual(normalize_priority("p3"), 3)
        self.assertEqual(normalize_priority(1), 1)
        with self.assertRaises(TaskError):
            normalize_priority("P9")

    def test_priority_sorts_within_status(self):
        self.store.add_task("demo", "low", status="todo", priority=3)
        self.store.add_task("demo", "urgent", status="todo", priority=0)
        self.store.add_task("demo", "mid", status="todo", priority=2)
        names = [t.name for t in self.store.list_tasks("demo")]
        self.assertEqual(names, ["urgent", "mid", "low"])

    def test_tag_filter_and_priority_filter(self):
        self.store.add_task("demo", "a", tags=["infra", "backend"])
        self.store.add_task("demo", "b", tags=["frontend"])
        self.store.add_task("demo", "c", priority=0)
        self.assertEqual([t.name for t in self.store.list_tasks("demo", tags=["infra"])], ["a"])
        self.assertEqual([t.name for t in self.store.list_tasks("demo", priorities=[0])], ["c"])


class ExportImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def _seed(self):
        a = self.store.add_task("proj", "first", status="done", evidence="did it", priority=0,
                                tags=["infra"])
        b = self.store.add_task("proj", "second", depends_on=[a.id], affects=[a.id],
                                blockers=["waiting"], description="desc")
        return a, b

    def test_round_trip_preserves_everything(self):
        a, b = self._seed()
        payload = self.store.export_tasks("proj")
        self.assertEqual(payload["format"], EXPORT_FORMAT)
        self.assertEqual(payload["version"], EXPORT_VERSION)

        with tempfile.TemporaryDirectory() as other_dir:
            with Store(Path(other_dir) / "tasks.db") as dest:
                report = dest.import_tasks(payload)
                self.assertEqual(len(report["created"]), 2)
                imported = dest.list_tasks("proj")
                self.assertEqual([t.name for t in imported], ["second", "first"])  # board order
                by_name = {t.name: t for t in imported}
                first, second = by_name["first"], by_name["second"]
                self.assertEqual(first.status, "done")
                self.assertEqual(first.priority, 0)
                self.assertEqual(first.tags, ["infra"])
                with Store(Path(other_dir) / "tasks.db") as probe:
                    ev = [e["text"] for e in probe.evidence(first)]
                self.assertEqual(ev, ["did it"])
                # dependency remapped to the new id
                self.assertEqual(second.depends_on, [first.id])
                self.assertEqual(second.affects, [first.id])
                self.assertEqual(second.blockers, ["waiting"])

    def test_merge_skips_existing_and_links_to_it(self):
        a, b = self._seed()
        payload = self.store.export_tasks("proj")
        with tempfile.TemporaryDirectory() as other_dir:
            with Store(Path(other_dir) / "tasks.db") as dest:
                existing = dest.add_task("proj", "first", status="todo")
                report = dest.import_tasks(payload)
                self.assertEqual(len(report["existing"]), 1)
                self.assertEqual(len(report["created"]), 1)
                second = dest.get("proj", existing.id + 1)
                # dep now points at the pre-existing task's id
                self.assertEqual(second.depends_on, [existing.id])

    def test_replace_mode_and_dry_run(self):
        self._seed()
        payload = self.store.export_tasks("proj")
        report = self.store.import_tasks(payload, mode="replace", dry_run=True)
        self.assertTrue(any("would create" in line for line in report["created"]))
        self.assertEqual(len(self.store.list_tasks("proj")), 2)  # nothing written
        self.store.import_tasks(payload, mode="replace")
        names = [t.name for t in self.store.list_tasks("proj")]
        self.assertEqual(names, ["second", "first"])  # board order

    def test_rejects_unknown_format_and_version(self):
        with self.assertRaises(TaskError):
            self.store.import_tasks({"format": "nope", "version": 1, "tasks": []})
        with self.assertRaises(TaskError):
            self.store.import_tasks({"format": EXPORT_FORMAT, "version": 99, "tasks": []})


class AttachmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.task = self.store.add_task("demo", "with files")

    def test_attach_list_read_detach(self):
        source = Path(self.tmp.name) / "blob.bin"
        source.write_bytes(b"\x00\x01\x02binary!")
        att = self.store.add_attachment(self.task, source)
        self.assertEqual(att["size"], 10)

        listed = self.store.list_attachments(self.task)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["filename"], "blob.bin")
        # file lives outside the db file itself, content-addressed
        self.assertTrue(Path(listed[0]["path"]).is_file())

        data = self.store.read_attachment_bytes(self.task, listed[0]["id"])
        self.assertEqual(data, b"\x00\x01\x02binary!")

        name = self.store.remove_attachment(self.task, listed[0]["id"])
        self.assertEqual(name, "blob.bin")
        self.assertEqual(self.store.list_attachments(self.task), [])
        self.assertFalse(Path(listed[0]["path"]).exists())

    def test_delete_task_removes_files(self):
        source = Path(self.tmp.name) / "gone.txt"
        source.write_text("bye")
        self.store.add_attachment(self.task, source)
        path = self.store.list_attachments(self.task)[0]["path"]
        self.store.delete(self.task)
        self.assertFalse(Path(path).exists())

    def test_export_embeds_base64_and_import_restores(self):
        import base64

        source = Path(self.tmp.name) / "img.png"
        payload_bytes = b"\x89PNG\r\n\x1a\nfake-image"
        source.write_bytes(payload_bytes)
        self.store.add_attachment(self.task, source, filename="chart.png")

        export = self.store.export_tasks("demo")
        entry = export["tasks"][0]["attachments"][0]
        self.assertEqual(entry["filename"], "chart.png")
        self.assertEqual(base64.b64decode(entry["content_b64"]), payload_bytes)

        with tempfile.TemporaryDirectory() as other_dir:
            with Store(Path(other_dir) / "tasks.db") as dest:
                dest.import_tasks(export)
                imported = dest.list_tasks("demo")[0]
                atts = dest.list_attachments(imported)
                self.assertEqual(len(atts), 1)
                self.assertEqual(dest.read_attachment_bytes(imported, atts[0]["id"]), payload_bytes)


class EventLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_status_and_ownership_events(self):
        t = self.store.add_task("demo", "watched", status="todo")
        self.store.claim(t, "agent-a")                       # claim + status event
        t = self.store.get("demo", t.id)
        self.store.set_status(t, "in_review")                # status event
        t = self.store.get("demo", t.id)
        self.store.handoff(t, "agent-b", from_owner="agent-a")   # handoff event
        t = self.store.get("demo", t.id)
        self.store.release(t, owner="agent-b")               # release event
        t = self.store.get("demo", t.id)
        events = self.store.events(t)                        # newest first
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["release", "handoff", "status", "claim", "status"])
        self.assertEqual(events[-1]["detail"], "todo -> in_progress")
        self.assertEqual(events[3]["detail"], "claimed by agent-a")
        self.assertEqual(events[2]["detail"], "in_progress -> in_review")
        self.assertEqual(events[1]["detail"], "agent-a -> agent-b")
        self.assertEqual(events[0]["detail"], "released by agent-b")

    def test_no_event_for_same_status_or_field_edits(self):
        t = self.store.add_task("demo", "quiet", status="todo")
        self.store.update(t, status="todo")                  # no-op status
        t = self.store.get("demo", t.id)
        self.store.update(t, name="renamed")                 # non-status field
        t = self.store.get("demo", t.id)
        self.assertEqual(self.store.events(t), [])


class EvidenceTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
    def test_legacy_blob_migration(self):
        import sqlite3

        from agenttasker.db import connect

        db_path = Path(self.tmp.name) / "legacy.db"
        raw = sqlite3.connect(db_path)
        raw.executescript("""
            CREATE TABLE tasks (
                project TEXT NOT NULL, id INTEGER NOT NULL,
                name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'backlog',
                description TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                blockers TEXT NOT NULL DEFAULT '[]', depends_on TEXT NOT NULL DEFAULT '[]',
                affects TEXT NOT NULL DEFAULT '[]', owner TEXT NOT NULL DEFAULT '',
                claimed_at TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 2,
                type TEXT NOT NULL DEFAULT 'task', tags TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (project, id)
            );
        """)
        blob = (
            "[2026-09-12T10:00:00+00:00] first entry\n\n"
            "unstamped manual line\n\n"
            "[2026-09-12T11:30:00Z] second entry"
        )
        raw.execute(
            "INSERT INTO tasks (project,id,name,evidence,created_at,updated_at)"
            " VALUES ('p',1,'migrated',?,'2026-09-12T09:00:00+00:00','2026-09-12T12:00:00+00:00')",
            (blob,),
        )
        raw.commit()
        raw.close()

        with Store(db_path) as store:  # opening triggers the migration
            entries = store.evidence(store.get("p", 1))
        self.assertEqual([e["ts"] for e in entries],
                         ["2026-09-12T10:00:00+00:00", "2026-09-12T12:00:00+00:00",
                          "2026-09-12T11:30:00Z"])
        self.assertEqual([e["text"] for e in entries],
                         ["first entry", "unstamped manual line", "second entry"])

        import sqlite3 as sq
        cols = {r[1] for r in sq.connect(db_path).execute("PRAGMA table_info(tasks)")}
        self.assertNotIn("evidence", cols)  # column dropped

    def test_append_edit_delete_rows(self):
        with Store(Path(self.tmp.name) / "t.db") as store:
            t = store.add_task("p", "with evidence", evidence="seed analysis")
            t = store.append_evidence(t, "second finding")
            t = store.append_evidence(t, "third")
            entries = store.evidence(t)
            self.assertEqual([e["text"] for e in entries],
                             ["seed analysis", "second finding", "third"])
            t = store.edit_evidence(t, entries[1]["id"], "second finding (edited)")
            self.assertEqual([e["text"] for e in store.evidence(t)],
                             ["seed analysis", "second finding (edited)", "third"])
            t = store.delete_evidence(t, entries[0]["id"])
            self.assertEqual([e["text"] for e in store.evidence(t)],
                             ["second finding (edited)", "third"])
            # full replace via update(evidence=...) -> single fresh entry
            t = store.update(t, evidence="reset")
            self.assertEqual([e["text"] for e in store.evidence(t)], ["reset"])

    def test_v1_export_imports_with_parsed_evidence(self):
        with Store(Path(self.tmp.name) / "v1.db") as store:
            t = store.add_task("p", "old format", evidence="imported entry")
            payload = store.export_tasks("p")
            # downgrade to v1 shape: evidence as a blob string
            payload["version"] = 1
            payload["tasks"][0]["evidence"] = "[2026-09-12T10:00:00+00:00] imported entry"
            for task in payload["tasks"]:
                task.pop("evidence_rows", None)
            with tempfile.TemporaryDirectory() as other:
                with Store(Path(other) / "in.db") as dest:
                    dest.import_tasks(payload)
                    got = dest.evidence(dest.list_tasks("p")[0])
                    self.assertEqual([(e["ts"], e["text"]) for e in got],
                                     [("2026-09-12T10:00:00+00:00", "imported entry")])


class StoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_story_lifecycle(self):
        story = self.store.add_task("p", "OAuth login", status="in_progress")
        sub1 = self.store.add_task("p", "endpoints", parent=story.id, status="done")
        sub2 = self.store.add_task("p", "refresh", parent=story.id)
        self.assertEqual((sub1.parent_id, sub2.parent_id), (story.id, story.id))
        # progress derived, not stored
        self.assertEqual(self.store.story_progress(story), (1, 2))
        # children board-ordered
        self.assertEqual([c.name for c in self.store.children(story)],
                         ["refresh", "endpoints"])
        # delete guard + force detach
        with self.assertRaises(TaskError):
            self.store.delete(story)
        self.store.delete(story, force=True)
        fresh_sub = self.store.get("p", sub1.id)
        self.assertEqual(fresh_sub.parent_id, 0)

    def test_no_nesting_and_bad_refs(self):
        story = self.store.add_task("p", "story")
        sub = self.store.add_task("p", "sub", parent=story.id)
        with self.assertRaises(TaskError):  # sub-task can't be a story's parent
            self.store.add_task("p", "grandchild", parent=sub.id)
        with self.assertRaises(TaskError):  # nonexistent story
            self.store.add_task("p", "orphan", parent=999)
        with self.assertRaises(TaskError):  # story with children can't become a sub-task
            self.store.update(story, parent=123)
        # detach via empty parent
        detached = self.store.update(sub, parent="")
        self.assertEqual(detached.parent_id, 0)

    def test_story_filters(self):
        story = self.store.add_task("p", "story")
        self.store.add_task("p", "sub", parent=story.id)
        self.store.add_task("p", "top-level")
        self.assertEqual([t.name for t in self.store.list_tasks("p", story=story.id)], ["sub"])
        self.assertEqual([t.name for t in self.store.list_tasks("p", stories_only=True)], ["story"])


class BranchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_branch_lifecycle(self):
        t = self.store.add_task("p", "fix", branch="feature/fix-login")
        self.assertEqual(t.branch, "feature/fix-login")
        updated = self.store.update(t, branch="hotfix")
        self.assertEqual(updated.branch, "hotfix")
        cleared = self.store.update(updated, branch="")
        self.assertEqual(cleared.branch, "")

    def test_branch_filter(self):
        self.store.add_task("p", "one", branch="main")
        self.store.add_task("p", "two", branch="feature/auth")
        self.store.add_task("p", "three")  # no branch
        self.assertEqual([t.name for t in self.store.list_tasks("p", branch="main")], ["one"])
        self.assertEqual([t.name for t in self.store.list_tasks("p", branch="feature/auth")], ["two"])
        self.assertEqual([t.name for t in self.store.list_tasks("p", branch="")], ["three"])
        # wildcards
        self.assertEqual(sorted(t.name for t in self.store.list_tasks("p", branch="feature/*")),
                         ["two"])







class TaskTypeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_normalize_type_aliases_and_errors(self):
        self.assertEqual(normalize_type("BUG"), "bugfix")
        self.assertEqual(normalize_type("fix"), "bugfix")
        self.assertEqual(normalize_type("feat"), "feature")
        self.assertEqual(normalize_type("refactor"), "improvement")
        self.assertEqual(normalize_type("Task"), "task")
        with self.assertRaises(TaskError):
            normalize_type("epic")

    def test_type_roundtrip_and_update(self):
        t1 = self.store.add_task("demo", "crash on boot", type="bugfix", priority=0)
        t2 = self.store.add_task("demo", "new dashboard", type="feature")
        t3 = self.store.add_task("demo", "plain")
        self.assertEqual((t1.type, t2.type, t3.type), ("bugfix", "feature", "task"))
        updated = self.store.update(t3, type="chore")
        self.assertEqual(updated.type, "chore")
        # default sort stays status/priority/id; types ride along
        self.assertEqual([t.name for t in self.store.list_tasks("demo")],
                         ["crash on boot", "new dashboard", "plain"])

    def test_type_survives_export_import(self):
        self.store.add_task("proj", "typed", type="bugfix", priority=1)
        payload = self.store.export_tasks("proj")
        with tempfile.TemporaryDirectory() as other:
            with Store(Path(other) / "tasks.db") as dest:
                dest.import_tasks(payload)
                self.assertEqual(dest.list_tasks("proj")[0].type, "bugfix")


class PerProjectIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_ids_are_dense_and_per_project(self):
        with Store(Path(self.tmp.name) / "tasks.db") as store:
            a1 = store.add_task("alpha", "one")
            a2 = store.add_task("alpha", "two")
            b1 = store.add_task("beta", "other")
            self.assertEqual((a1.id, a2.id, b1.id), (1, 2, 1))
            # deletion leaves a hole; the next id reuses the max+1 rule per project
            store.delete(a2)
            a3 = store.add_task("alpha", "three")
            self.assertEqual(a3.id, 2)

    def test_legacy_global_id_migration(self):
        import sqlite3

        # build a legacy database: single-column autoincrement pk, global ids
        db_path = Path(self.tmp.name) / "legacy.db"
        raw = sqlite3.connect(db_path)
        raw.executescript("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL, name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'backlog',
                description TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                blockers TEXT NOT NULL DEFAULT '[]',
                depends_on TEXT NOT NULL DEFAULT '[]',
                affects TEXT NOT NULL DEFAULT '[]',
                owner TEXT NOT NULL DEFAULT '',
                claimed_at TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 2,
                type TEXT NOT NULL DEFAULT 'task',
                tags TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL, task_id INTEGER NOT NULL,
                filename TEXT NOT NULL, relpath TEXT NOT NULL,
                size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        now = "2026-09-13T00:00:00+00:00"
        # alpha: global ids 2,5 with 5 depending on 2; beta: global id 3
        raw.execute("INSERT INTO tasks (project,name,status,depends_on,created_at,updated_at)"
                    " VALUES ('alpha','first','todo','[]',?,?)", (now, now))       # id 1
        raw.execute("INSERT INTO tasks (project,name,status,created_at,updated_at)"
                    " VALUES ('beta','other','todo',?,?)", (now, now))             # id 2
        raw.execute("INSERT INTO tasks (project,name,status,depends_on,created_at,updated_at)"
                    " VALUES ('alpha','second','backlog','[1]',?,?)", (now, now))  # id 3
        raw.execute("INSERT INTO attachments (project,task_id,filename,relpath,size,sha256,created_at)"
                    " VALUES ('alpha',3,'f.txt','alpha/3/x',3,'abc','2026')")      # on alpha#3
        raw.commit()
        raw.close()

        with Store(db_path) as store:  # opening converts the layout, ids PRESERVED
            alpha = {t.name: t for t in store.list_tasks("alpha")}
            beta = store.list_tasks("beta")
            self.assertEqual((alpha["first"].id, alpha["second"].id), (1, 3))  # unchanged
            self.assertEqual(beta[0].id, 2)                                     # unchanged
            second = alpha["second"]
            self.assertEqual(second.depends_on, [1])
            atts = store.list_attachments(second)
            self.assertEqual([(a["filename"], a["id"]) for a in atts], [("f.txt", 1)])
            # allocation continues from each project's own max
            nxt = store.add_task("alpha", "third")
            self.assertEqual(nxt.id, 4)
            nxt_b = store.add_task("beta", "another")
            self.assertEqual(nxt_b.id, 3)
        with Store(db_path) as store2:  # second open is a no-op
            self.assertEqual(sorted(t.id for t in store2.list_tasks("alpha")), [1, 3, 4])


class ListOrderTest(unittest.TestCase):
    """The list view orders by status (todo, backlog, deferred, in-flight, done), then priority."""

    def test_list_view_order(self):
        from agenttasker.tui import Board

        with tempfile.TemporaryDirectory() as tmp:
            with Store(Path(tmp) / "tasks.db") as store:
                store.add_task("d", "done old", status="done")
                store.add_task("d", "review", status="in_review")
                store.add_task("d", "inprog", status="in_progress")
                store.add_task("d", "deferred", status="deferred")
                store.add_task("d", "backlog low", status="backlog", priority=3)
                store.add_task("d", "backlog high", status="backlog", priority=0)
                store.add_task("d", "todo p2", status="todo")
                store.add_task("d", "todo p0", status="todo", priority=0)
                board = Board(store, "d")
                board.reload()
                names = [t.name for t in board.list_tasks_sorted()]
                self.assertEqual(
                    names,
                    ["todo p0", "todo p2", "backlog high", "backlog low",
                     "deferred", "inprog", "review", "done old"],
                )




if __name__ == "__main__":
    unittest.main()

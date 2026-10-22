from __future__ import annotations

import argparse
import json as jsonlib
import os
import sys
from collections import Counter
from pathlib import Path

from . import __version__, render
from .core import (
    DONE,
    STATUSES,
    Store,
    Task,
    TaskError,
    display_ref,
    is_blocked,
    is_ready,
    LIST_STATUS_ORDER,
    next_status,
    normalize_status,
    parse_tags,
    prev_status,
    resolve_project,
    status_label,
    unfinished_dep_ids,
    prev_status,
    priority_label,
)

_STATUS_HELP = ", ".join(STATUSES)

_EPILOG = f"""\
statuses (board order):
  deferred      parked; revisit later, not part of the active set
  backlog       accepted, but not yet ready or prioritized
  todo          ready to work once dependencies/blockers clear
  in_progress   actively being worked (claim it before starting)
  in_review     work done, awaiting verification/review
  done          complete and verified

tasks are namespaced per project (default: git repo name, else cwd name).
override with --project or $AGENTTASKER_PROJECT.
examples:
  agenttasker add "Fix login redirect" -d "Session cookie lost on 302" --dep 3
  agenttasker ls --ready
  agenttasker move 12 --next && agenttasker show 12
"""


def _comma_list(values: Sequence[str] | None) -> list[str]:
    """Flatten repeatable flags that may carry comma-separated values. Empty items dropped."""
    out: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def build_parser() -> argparse.ArgumentParser:
    invoked = os.path.basename(sys.argv[0])
    prog = "python -m agenttasker" if invoked == "__main__.py" else invoked
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Local, project-namespaced task tracker for agents and humans.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--db", metavar="PATH",
        help="task database file (default: $AGENTTASKER_DB or ~/.local/share/agenttasker/tasks.db)",
    )
    parser.add_argument(
        "-p", "--project", metavar="NAME",
        help="project namespace (default: $AGENTTASKER_PROJECT, git repo name, or cwd name)",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", metavar="PATH", default=argparse.SUPPRESS,
                        help="task database file (default: $AGENTTASKER_DB or ~/.local/share/agenttasker/tasks.db)")
    common.add_argument("-p", "--project", metavar="NAME", default=argparse.SUPPRESS,
                        help="project namespace (default: $AGENTTASKER_PROJECT, git repo name, or cwd name)")
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    p = sub.add_parser("add", parents=[common], help="create a task")
    p.add_argument("name", help="short task name")
    p.add_argument("-d", "--description", default="", help="what needs to happen and why")
    p.add_argument(
        "-s", "--status", default="backlog", metavar="STATUS",
        help=f"initial status (default: backlog; one of: {_STATUS_HELP})",
    )
    p.add_argument(
        "-e", "--evidence", default="", metavar="TEXT",
        help="evidence / analysis gathered before starting the task",
    )
    p.add_argument(
        "--blocker", action="append", metavar="TEXT",
        help="external blocker (free text, not a task ref); repeatable",
    )
    p.add_argument(
        "--dep", action="append", metavar="REF",
        help="task this depends on (id or PROJECT-id); repeatable, comma-separated ok",
    )
    p.add_argument(
        "--priority", default="P2", metavar="P0-P3",
        help="priority level P0 (highest) .. P3 (default: P2)",
    )
    p.add_argument(
        "--type", default="task", metavar="TYPE",
        help="task type: task|feature|bugfix|improvement|chore (default: task)",
    )
    p.add_argument(
        "--tag", action="append", metavar="TAG",
        help="workstream/tag label; repeatable, comma-separated ok",
    )
    p.add_argument(
        "--story", metavar="REF",
        help="make this a sub-task of the given story (id or PROJECT-id)",
    )
    p.add_argument(
        "--affects", action="append", metavar="REF",
        help="task whose outcome this work affects; repeatable, comma-separated ok",
    )
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("ls", parents=[common], help="list tasks (board order)")
    p.add_argument("-s", "--status", action="append", metavar="STATUS",
                   help=f"filter by status; repeatable, comma-separated ({_STATUS_HELP})")
    p.add_argument("--search", metavar="TEXT", help="match name/description/evidence (AND terms)")
    p.add_argument("--ready", action="store_true", help="only tasks ready to start now")
    p.add_argument("--blocked", action="store_true", help="only tasks blocked (deps or external)")
    p.add_argument("-a", "--all-projects", action="store_true", help="list tasks across all projects")
    p.add_argument("--priority", action="append", metavar="P0-P3",
                   help="filter by priority; repeatable, comma-separated")
    p.add_argument("--tag", action="append", metavar="TAG", help="filter by tag/workstream")
    p.add_argument("--story", metavar="REF", help="only sub-tasks of this story")
    p.add_argument("--stories", action="store_true", help="only stories (tasks with sub-tasks)")
    p.add_argument("--stale", nargs="?", const=24, metavar="HOURS", type=float,
                   help="only claims older than HOURS (default 24)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("list", parents=[common], help="flat task table: todo first, done last, priority within status")
    p.add_argument("-a", "--all-projects", action="store_true", help="list across all projects")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("story", parents=[common], help="create or inspect a story")
    sub2 = p.add_subparsers(dest="story_cmd", metavar="<action>")
    sp = sub2.add_parser("add", parents=[common], help="create a story (a task that will hold sub-tasks)")
    sp.add_argument("name")
    sp.add_argument("-d", "--description", default="", help="what the feature delivers")
    sp.add_argument("-s", "--status", default="backlog", metavar="STATUS")
    sp.add_argument("--priority", default="P2", metavar="P0-P3")
    sp.set_defaults(func=cmd_story_add)
    sp = sub2.add_parser("show", parents=[common], help="checklist view of a story's sub-tasks")
    sp.add_argument("ref", help="story task id or PROJECT-id")
    sp.set_defaults(func=cmd_story_show)


    p = sub.add_parser("show", parents=[common], help="show full task detail")
    p.add_argument("ref", help="task id or PROJECT-id (e.g. '12' or 'demo-12')")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("serve", parents=[common], help="launch the local web UI (board + list views)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8988, help="port (default: 8988)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser automatically")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("update", parents=[common], help="edit task fields")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.add_argument("--name", help="new task name")
    p.add_argument("-d", "--description", help="new description (replaces)")
    p.add_argument("-e", "--evidence", help="new evidence text (replaces)")
    p.add_argument("--append-evidence", metavar="TEXT", help="append a line to evidence")
    p.add_argument("--blocker", action="append", metavar="TEXT",
                   help="external blockers (replaces list; repeatable; '' clears)")
    p.add_argument("--dep", action="append", metavar="REF",
                   help="depends-on refs (replaces list; repeatable; '' clears)")
    p.add_argument("--affects", action="append", metavar="REF",
                   help="affects refs (replaces list; repeatable; '' clears)")
    p.add_argument("--priority", metavar="P0-P3", help="new priority (P0 highest .. P3)")
    p.add_argument("--type", metavar="TYPE", help="new type: task|feature|bugfix|improvement|chore")
    p.add_argument("--tag", action="append", metavar="TAG",
                   help="tags/workstreams (replaces list; repeatable; '' clears)")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("claim", parents=[common], help="atomically take ownership of a task")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.add_argument("--owner", required=True, metavar="NAME", help="claiming agent/person")
    p.add_argument("--force", action="store_true", help="take the claim even if held")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", parents=[common], help="release a claim")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.add_argument("--owner", metavar="NAME", help="your name; must match the holder")
    p.add_argument("--force", action="store_true", help="release regardless of holder")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("handoff", parents=[common], help="transfer a claim to another owner")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.add_argument("--to", required=True, metavar="NAME", help="new owner")
    p.add_argument("--from", dest="from_owner", metavar="NAME", help="current owner (you)")
    p.add_argument("--force", action="store_true", help="transfer regardless of holder")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("claims", parents=[common], help="list active claims (owner + age)")
    p.add_argument("--stale", nargs="?", const=24, metavar="HOURS", type=float,
                   help="only claims older than HOURS (default 24)")
    p.set_defaults(func=cmd_claims)

    p = sub.add_parser("export", parents=[common], help="export tasks to a portable JSON snapshot")
    p.add_argument("path", nargs="?", default="-", help="output file ('-' = stdout)")
    p.add_argument("-a", "--all-projects", action="store_true", help="export every project")
    p.add_argument("--no-attachments", action="store_true",
                   help="exclude base64-encoded attachments from the export")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", parents=[common], help="import tasks from an export snapshot")
    p.add_argument("path", help="export file to import ('-' = stdin)")
    p.add_argument("--mode", choices=("merge", "replace"), default="merge",
                   help="merge: keep existing tasks; replace: clear imported projects first")
    p.add_argument("--dry-run", action="store_true", help="preview decisions without writing")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("attach", parents=[common], help="attach a file to a task")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.add_argument("file", help="path of the file to attach")
    p.add_argument("--name", metavar="FILENAME", help="stored filename (default: source name)")
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("attachments", parents=[common], help="list a task's attachments")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_attachments)

    p = sub.add_parser("detach", parents=[common], help="remove an attachment")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.add_argument("attachment", type=int, help="attachment id (see `attachments`)")
    p.set_defaults(func=cmd_detach)

    p = sub.add_parser("move", parents=[common], help="change task status")
    p.add_argument("ref", help="task id or PROJECT-id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--to", metavar="STATUS", help=f"target status ({_STATUS_HELP})")
    g.add_argument("--next", action="store_true", help="advance one step toward done")
    g.add_argument("--prev", action="store_true", help="step one back toward deferred")
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("done", parents=[common], help="mark a task done")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("rm", parents=[common], help="delete a task (scrubs refs from other tasks)")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.add_argument("--force", action="store_true",
                   help="allow deleting a story (sub-tasks are detached, not deleted)")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("projects", parents=[common], help="list projects with per-status counts")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("board", parents=[common], help="open the interactive TUI kanban board")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("mcp", parents=[common], help="run the MCP server (stdio; requires the 'mcp' extra)")
    p.set_defaults(func=cmd_mcp)

    return parser


def _project_map(store: Store, project: str) -> dict[int, Task]:
    return {t.id: t for t in store.list_tasks(project)}


def _task_json(store: Store, task: Task) -> dict:
    data = task.to_dict()
    dep_map = store.dep_statuses(task)
    data["unfinished_deps"] = unfinished_dep_ids(task, dep_map)
    data["blocked"] = is_blocked(task, dep_map)
    data["ready"] = is_ready(task, dep_map)
    data["evidence"] = store.evidence(task)
    data["last_activity"] = store.last_activity(task)
    return data


def cmd_add(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.add_task(
        project,
        args.name,
        description=args.description,
        status=args.status,
        evidence=args.evidence,
        blockers=[b for b in (args.blocker or []) if b.strip()],
        depends_on=_comma_list(args.dep),
        affects=_comma_list(args.affects),
        priority=args.priority,
        type=args.type,
        tags=parse_tags(args.tag or []),
        parent=getattr(args, "story", None),
    )
    print(f"Added {display_ref(project, task.id)} [{task.status}] {task.name}")
    return 0


def cmd_ls(args, store: Store) -> int:
    project = None if args.all_projects else resolve_project(args.project)
    statuses = _comma_list(args.status) or None
    priorities = _comma_list(args.priority) or None
    tags = _comma_list(args.tag) or None
    tasks = store.list_tasks(
        project,
        statuses=statuses,
        search=args.search,
        priorities=priorities,
        tags=tags,
        stale_hours=args.stale,
        story=getattr(args, "story", None),
        stories_only=getattr(args, "stories", False),
    )
    if args.ready:
        tasks = [t for t in tasks if store.is_ready(t)]
    if args.blocked:
        tasks = [t for t in tasks if store.is_blocked(t)]

    if args.json:
        print(jsonlib.dumps([_task_json(store, t) for t in tasks], indent=2))
        return 0

    scope = "all projects" if project is None else f"project {project!r}"
    suffix = ""
    if args.ready:
        suffix += " [ready]"
    if args.blocked:
        suffix += " [blocked]"
    if args.search:
        suffix += f" [search: {args.search}]"
    if args.stale is not None:
        suffix += f" [stale > {args.stale:g}h]"
    print(f"{scope} — {len(tasks)} task(s){suffix}")
    # dependency flags need the FULL project task set, not the filtered listing
    maps: dict[str, dict[int, Task]] = {
        proj: {x.id: x for x in store.list_tasks(proj)}
        for proj in {t.project for t in tasks}
    }
    for t in tasks:
        print(render.task_line(t, maps[t.project], show_project=project is None))
    counts = Counter(t.status for t in tasks)
    summary = "  ".join(f"{s}:{counts[s]}" for s in STATUSES if counts[s])
    if summary:
        print(summary)
    return 0


def cmd_show(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    if args.json:
        print(jsonlib.dumps(_task_json(store, task), indent=2))
        return 0
    print(render.detail(task, _project_map(store, project),
                        attachments=store.list_attachments(task), store=store))
    return 0


def cmd_list(args, store: Store) -> int:
    """Flat table of every task: todo, backlog, deferred, in-flight, done last;
    priority within each status. Columns: id, title, status, priority, type, blocked-by, created."""
    project = None if args.all_projects else resolve_project(args.project)
    status_order = {s: i for i, s in enumerate(LIST_STATUS_ORDER)}
    tasks = sorted(store.list_tasks(project), key=lambda t: (status_order[t.status], t.priority, t.id))
    if args.json:
        print(jsonlib.dumps([_task_json(store, t) for t in tasks], indent=2))
        return 0

    def blocked_by(t: Task) -> str:
        dep_map = store.dep_statuses(t)
        pending = unfinished_dep_ids(t, dep_map)
        bits = [f"#{i}" for i in pending]
        if t.blockers:
            bits.append(f"ext:{len(t.blockers)}")
        return ",".join(bits) if bits else "—"

    scope = "all projects" if project is None else f"project {project!r}"
    print(f"{scope} — {len(tasks)} task(s) · todo first, done last, priority within status")
    print(f"{'ID':<10} {'TITLE':<38} {'STATUS':<11} {'PRI':<3} {'TYPE':<11} {'BLOCKED-BY':<16} CREATED")
    for t in tasks:
        ident = f"{t.project}/{t.id}" if project is None else f"{t.id}"
        print(
            f"{ident:<10} {t.name[:36]:<38} {t.status:<11} "
            f"{priority_label(t.priority):<3} {t.type:<11} {blocked_by(t):<16} "
            f"{t.created_at[:10]}"
        )
    return 0



def cmd_update(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    changes: dict = {}

    if args.name is not None:
        changes["name"] = args.name
    if args.description is not None:
        changes["description"] = args.description
    if args.evidence is not None:
        changes["evidence"] = args.evidence
    if args.blocker is not None:
        changes["blockers"] = [b for b in args.blocker if b.strip()]
    if args.dep is not None:
        changes["depends_on"] = _comma_list(args.dep)
    if args.affects is not None:
        changes["affects"] = _comma_list(args.affects)
    if args.append_evidence:
        task = store.append_evidence(task, args.append_evidence)  # atomic SQL append
    if args.priority is not None:
        changes["priority"] = args.priority
    if args.type is not None:
        changes["type"] = args.type
    if args.tag is not None:
        changes["tags"] = _comma_list(args.tag)

    updated = store.update(task, **changes) if changes else task
    print(f"Updated {display_ref(project, updated.id)}")
    print()
    print(render.detail(updated, _project_map(store, project), store=store))
    return 0


def cmd_move(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    if args.to is not None:
        target = normalize_status(args.to)
    elif args.next:
        target = next_status(task.status)
    else:
        target = prev_status(task.status)
    updated = store.set_status(task, target)
    print(f"{display_ref(project, updated.id)}  {task.status} -> {updated.status}")
    return 0


def cmd_done(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    updated = store.set_status(task, DONE)
    print(f"{display_ref(project, updated.id)}  {task.status} -> {updated.status}")
    return 0


def cmd_rm(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    notes = store.delete(task, force=getattr(args, "force", False))
    print(f"Deleted {display_ref(project, task.id)} ({task.name})")
    for note in notes:
        print(f"  - {note}")
    return 0


def cmd_story_add(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.add_task(
        project, args.name,
        description=args.description, status=args.status, priority=args.priority,
    )
    print(f"Added story {display_ref(project, task.id)} [{task.status}] {task.name}")
    print(f"add sub-tasks with: agenttasker add \"...\" --story {task.id}")
    return 0


def cmd_story_show(args, store: Store) -> int:
    project = resolve_project(args.project)
    story = store.get(project, args.ref)
    children = store.children(story)
    if not children:
        print(f"{display_ref(project, story.id)} {story.name} — no sub-tasks yet")
        print(f"add one with: agenttasker add \"...\" --story {story.id}")
        return 0
    done = sum(1 for c in children if c.status == DONE)
    print(f"{display_ref(project, story.id)}  {story.name}  —  {done}/{len(children)} done")
    dep_map = {c.id: c.status for c in children}
    for c in children:
        flags = []
        if c.status != DONE and is_blocked(c, dep_map):
            flags.append("blocked")
        elif is_ready(c, dep_map):
            flags.append("ready")
        flag = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  #{c.id:<4} {c.status:<11} {c.name}{flag}")
    return 0


def cmd_projects(args, store: Store) -> int:
    counts = store.projects()
    if not counts:
        print("no projects yet — create tasks with `agenttasker add`")
        return 0
    name_w = max([len("PROJECT")] + [len(name) for name in counts])
    header = f"{'PROJECT':<{name_w}}  " + "  ".join(f"{s:<11}" for s in STATUSES) + "  TOTAL"
    print(header)
    for name, bucket in counts.items():
        row = f"{name:<{name_w}}  " + "  ".join(
            f"{bucket[s]:<11}" for s in STATUSES
        )
        print(f"{row}  {bucket['total']}")
    return 0


def cmd_claim(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    updated = store.claim(task, args.owner, force=args.force)
    print(f"{display_ref(project, updated.id)} claimed by {updated.owner} (status -> {updated.status})")
    return 0


def cmd_release(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    updated = store.release(task, owner=args.owner, force=args.force)
    print(f"{display_ref(project, updated.id)} released (was {task.owner or 'unclaimed'})")
    return 0


def cmd_handoff(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    updated = store.handoff(task, args.to, from_owner=args.from_owner or "", force=args.force)
    print(f"{display_ref(project, updated.id)} {task.owner or '(unclaimed)'} -> {updated.owner}")
    return 0
def cmd_claims(args, store: Store) -> int:
    tasks = (
        store.list_tasks(None, stale_hours=args.stale)
        if args.stale is not None
        else [t for t in store.list_tasks(None) if t.owner]
    )
    if not tasks:
        print("no active claims" + (" older than the threshold" if args.stale is not None else ""))
        return 0
    maps = {p: {t.id: t for t in store.list_tasks(p)} for p in {t.project for t in tasks}}
    for t in tasks:
        print(render.task_line(t, maps[t.project], show_project=True))
    return 0


def cmd_export(args, store: Store) -> int:
    project = resolve_project(args.project)
    payload = store.export_tasks(
        None if args.all_projects else project, include_attachments=not args.no_attachments
    )
    text = jsonlib.dumps(payload, indent=2)
    if args.path == "-":
        print(text)
    else:
        Path(args.path).expanduser().write_text(text + "\n", encoding="utf-8")
        n = len(payload["tasks"])
        print(f"exported {n} task(s) -> {args.path}")
    return 0


def cmd_import(args, store: Store) -> int:
    if args.path == "-":
        payload = jsonlib.loads(sys.stdin.read())
    else:
        payload = jsonlib.loads(Path(args.path).expanduser().read_text(encoding="utf-8"))
    report = store.import_tasks(payload, mode=args.mode, dry_run=args.dry_run)
    verb = "would import" if args.dry_run else "imported"
    print(f"{verb}: {len(report['created'])} created, {len(report['existing'])} already present")
    for line in report["created"]:
        print(f"  + {line}")
    for line in report["existing"]:
        print(f"  = {line}")
    for line in report["warnings"]:
        print(f"  ! {line}")
    return 0


def cmd_attach(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    att = store.add_attachment(task, args.file, filename=args.name)
    print(f"attached #{att['id']} {att['filename']} ({att['size']} bytes, sha256 {att['sha256'][:12]}…)")
    return 0


def cmd_attachments(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    atts = store.list_attachments(task)
    if args.json:
        print(jsonlib.dumps(atts, indent=2))
        return 0
    if not atts:
        print("(no attachments)")
        return 0
    for att in atts:
        print(f"  #{att['id']}  {att['filename']}  {att['size']} bytes  {att['sha256'][:12]}…")
        print(f"       {att['path']}")
    return 0


def cmd_detach(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    name = store.remove_attachment(task, args.attachment)
    print(f"detached #{args.attachment} {name}")
    return 0


def cmd_board(args, store: Store) -> int:
    from .tui import run_board, select_project

    explicit = args.project is not None or os.environ.get("AGENTTASKER_PROJECT")
    project = resolve_project(args.project)
    if not explicit:
        # no project given: let the user pick from existing projects first
        projects = list(store.projects())
        if not projects:
            print("no projects yet — create tasks with `agenttasker add \"...\"` first")
            return 0
        if project not in projects:
            chosen = select_project(store, projects, preferred=project)
            if chosen is None:
                return 0
            project = chosen
    run_board(store, project)
    return 0


def cmd_serve(args, store: Store) -> int:
    from .web import serve

    project = resolve_project(args.project)
    serve(project, host=args.host, port=args.port, db=args.db, open_browser=not args.no_browser)
    return 0


def cmd_mcp(args=None, store: Store | None = None) -> int:
    try:
        from .mcp_server import serve
    except ImportError:
        print(
            "error: MCP extras not installed. Install with: pip install 'agenttasker[mcp]'",
            file=sys.stderr,
        )
        return 2
    serve()
    return 0


# --- entry point -------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    if args.cmd == "mcp":
        return cmd_mcp()
    try:
        with Store(args.db) as store:
            return args.func(args, store)
    except TaskError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())

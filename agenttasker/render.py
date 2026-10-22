"""Text rendering shared by the CLI and the MCP server."""

from __future__ import annotations

from .core import (
    DONE,
    DEFAULT_PRIORITY,
    DEFAULT_TYPE,
    Task,
    claim_age_hours,
    display_ref,
    is_blocked,
    is_ready,
    priority_label,
    status_label,
    unfinished_dep_ids,
)


def _indent(text: str, pad: str = "  ") -> str:
    return "\n".join(pad + line for line in text.rstrip().splitlines() or [pad])


def _dep_label(dep_id: int, by_id: dict[int, Task]) -> str:
    dep = by_id.get(dep_id)
    if dep is None:
        return f"#{dep_id} (missing)"
    return f"#{dep.id} ({dep.status}) {dep.name}"


def _owner_flag(task: Task) -> str:
    if not task.owner:
        return ""
    age = claim_age_hours(task)
    if age is None:
        return f"@{task.owner}"
    if age < 1:
        span = f"{int(age * 60)}m"
    elif age < 48:
        span = f"{age:.0f}h"
    else:
        span = f"{age / 24:.0f}d"
    return f"@{task.owner} {span}"


def task_line(task: Task, by_id: dict[int, Task], show_project: bool = False) -> str:
    """One-line board summary: '#12 in_progress Fix login [P1 bugfix @agent 2h]'."""
    prefix = f"{task.project}/" if show_project else ""
    flags: list[str] = []
    if task.priority != DEFAULT_PRIORITY:
        flags.append(priority_label(task.priority))
    if task.type != DEFAULT_TYPE:
        flags.append(task.type)
    owner = _owner_flag(task)
    if owner:
        flags.append(owner)
    if task.status != DONE:
        dep_map = {t.id: t.status for t in by_id.values()}
        pending = unfinished_dep_ids(task, dep_map)
        if pending:
            flags.append("deps:" + ",".join(f"#{i}" for i in pending))
        if task.blockers:
            flags.append(f"ext:{len(task.blockers)}")
        if not pending and not task.blockers and is_ready(task, dep_map):
            flags.append("ready")
    flag_text = f"  [{' '.join(flags)}]" if flags else ""
    return f"{prefix}#{task.id:<4} {task.status:<11} {task.name}{flag_text}"


def detail(task: Task, by_id: dict[int, Task], attachments: list | None = None,
           store=None) -> str:
    """Full task detail with computed dependency state."""
    lines: list[str] = [
        f"{display_ref(task.project, task.id)}  {task.name}",
        f"Status:   {status_label(task.status)}",
        f"Priority: {priority_label(task.priority)}",
        f"Type:     {task.type}",
        f"Tags:     {', '.join(task.tags) if task.tags else '(none)'}",
    ]
    owner = _owner_flag(task)
    lines.append(f"Owner:    {owner if owner else 'unclaimed'}")
    lines.append(f"Project:  {task.project}")
    dep_map = {t.id: t.status for t in by_id.values()}

    if task.description:
        lines.append("Description:")
        lines.append(_indent(task.description))

    entries = store.evidence(task) if store else []
    if entries:
        lines.append("Evidence / pre-task analysis:")
        lines.extend(f"  [{e['ts'][:19]}] {e['text']}" for e in entries)

    lines.append("Blockers (external):")
    if task.blockers:
        lines.extend(f"  - {b}" for b in task.blockers)
    else:
        lines.append("  (none)")

    lines.append("Depends on:")
    if task.depends_on:
        lines.extend(f"  - {_dep_label(i, by_id)}" for i in task.depends_on)
    else:
        lines.append("  (none)")

    lines.append("Affects:")
    if task.affects:
        lines.extend(f"  - {_dep_label(i, by_id)}" for i in task.affects)
    else:
        lines.append("  (none)")

    dependents = [t for t in by_id.values() if task.id in t.depends_on and t.id != task.id]

    if store:
        children = store.children(task)
        if children:
            done = sum(1 for c in children if c.status == DONE)
            lines.append(f"Story progress: {done}/{len(children)} done")
            lines.extend(f"  - #{c.id} ({c.status}) {c.name}" for c in children)
        if task.parent_id:
            parent = store.get_by_id(task.project, task.parent_id)
            if parent:
                lines.append(f"Part of story:  #{parent.id} {parent.name}")

    if attachments:
        lines.append("Attachments:")
        lines.extend(f"  - #{a['id']} {a['filename']} ({a['size']} bytes)" for a in attachments)

    state = "ready" if is_ready(task, dep_map) else ("BLOCKED" if is_blocked(task, dep_map) else "ok")
    lines.append(f"State:    {state}")
    lines.append(f"Created:  {task.created_at}   Updated: {task.updated_at}")
    return "\n".join(lines)

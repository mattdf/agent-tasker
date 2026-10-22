---
name: agenttasker
description: >-
  Local, project-namespaced task tracker for AI agents and humans (CLI + TUI + MCP).
  Use when planning, tracking, or executing multi-step work; when the user asks to
  create/track/manage tasks, todos, or a board; or when coordinating with other agents
  on the same project. Surfaces ready/blocked work so nothing slips through.
---

# AgentTasker

A shared task board agents and humans both read and write. One SQLite file per machine,
tasks namespaced by project (git repo name by default). Agents use the CLI or MCP tools;
humans can use the same CLI or the TUI (`agenttasker board`).

**Why you use it:** conversations forget; the board doesn't. Anything that spans more
than one step, or that you might hand off later, belongs on the board — not in your head.
Agents and humans share one board via the CLI/MCP tools and the TUI (`agenttasker board`).

## Model

- **Statuses** (board order): `deferred → backlog → todo → in_progress → in_review → done`
- **Task fields**: name, description, `evidence` (analysis recorded *before* starting),
  `priority` (`P0` urgent … `P3` low, default `P2`), `type`
  (`task`|`feature`|`bugfix`|`improvement`|`chore`), `tags` (workstream labels),
- **Refs**: `12` or `ProjectName-12` within the current project.
- **Derived state**:
  - *ready* = status `backlog`/`todo` AND unclaimed-or-yours AND no blockers AND all `depends_on` are `done`.
  - *blocked* = not `done` AND (unfinished dependency OR open external blocker).
  - Listings sort by status, then priority (P0 first) — `ls --ready` answers "what next?".
- **Storage**: `$AGENTTASKER_DB` or `~/.local/share/agenttasker/tasks.db` (SQLite, WAL — safe for concurrent agents).
  Attachments live beside it under `attachments/`, content-addressed by sha256.

## Non-negotiable rules

1. **Capture before you build.** When you accept multi-step work, `add_task` each meaningful
   step before starting. If a step will take real thought, it is a task.
2. **Evidence before execution.** Record pre-task analysis (files read, approach chosen,
   constraints found) in `evidence` BEFORE moving anything to `in_progress`.
3. **Claim before you work.** `claim_task <ref> --owner <you>` atomically takes ownership
   (and moves the task to `in_progress`); it refuses if another agent holds the claim —
   NEVER work a task someone else owns. Release when parking; handoff when transferring.
4. **Declare relationships immediately.** Set `depends_on`/`affects` at creation time;
   add discovered ones the moment you discover them.
5. **Blockers are public.** The second work stalls (missing access, failing dep, unanswered
   question), record it with `blockers`, and clear it the moment it resolves.
   A blocked task you're silent about is a slipped deadline.
6. **`in_review` means done-but-unverified; `done` means verified.** Only mark `done` when
   you've run the code/tests/commands that prove it. Log what you ran via `append_evidence`.
7. **Never delete someone else's task.** Park it: `set_status <ref> deferred`.
8. **Leave no orphans.** Before ending a session: no `in_progress` task you're not actively
   finishing (move it back + add a blocker note if parked), and every `ready` task has been
   acknowledged.

## Session ritual

Start of session:

```
agenttasker ls                    # whole board
agenttasker ls --ready            # pick work from here (priority order, P0 first)
agenttasker ls --blocked          # anything stalled? chase it
agenttasker claims                # who holds what (and for how long)
agenttasker claims --stale 12     # claims rotting for over 12h — investigate
```

During work: append findings as you go (`update <ref> --append-evidence "..."`).

End of session: `agenttasker ls` and confirm rule 8. Check `deferred` tasks occasionally —
decide deliberately whether they stay parked.

## CLI reference

Both `agenttasker` and the short alias `atx` invoke the same tool; examples use
`agenttasker` for clarity.
agenttasker add "Name" [-d DESC] [-s STATUS] [-e EVIDENCE] [--priority P0-P3]
                     [--type task|feature|bugfix|improvement|chore] [--tag TAG]...
                     [--blocker TEXT]... [--dep REF]... [--affects REF]...
agenttasker ls [-s STATUS]... [--priority P0-P3]... [--tag TAG] [--search TEXT]
               [--ready] [--blocked] [--stale [HOURS]] [-a] [--json]
agenttasker list [-a] [--json]      # flat table by priority: id title status pri type blocked-by created
agenttasker show REF [--json]
agenttasker update REF [--name N] [-d D] [-e E] [--append-evidence TEXT]
                       [--priority P] [--type T] [--tag T]... [--blocker T]... [--dep R]... [--affects R]...
agenttasker claim REF --owner NAME [--force]     # atomic; also sets in_progress
agenttasker release REF [--owner NAME] [--force]
agenttasker handoff REF --to NAME [--from NAME] [--force]
agenttasker claims [--stale [HOURS]]
agenttasker story add "Feature" [-d DESC]  # create a story (holds sub-tasks)
agenttasker story show REF                 # checklist: sub-tasks, done/total progress
agenttasker add "Name" --story REF         # sub-task of a story
agenttasker ls [--story REF] [--stories]   # filter by story / only stories
agenttasker rm REF [--force]               # stories need --force (children detach)
agenttasker move REF (--to STATUS | --next | --prev)
agenttasker done REF
agenttasker attach REF FILE [--name NAME]        # any binary; sha256-addressed on disk
agenttasker attachments REF [--json]
agenttasker detach REF ATTACHMENT_ID
agenttasker export [PATH] [-a] [--no-attachments]   # portable JSON; attachments base64-embedded
agenttasker import PATH [--mode merge|replace] [--dry-run]
agenttasker rm REF                # permanent; refs in other tasks are scrubbed
agenttasker projects
agenttasker board                 # TUI; picks a project first when none is given; v: board/list
agenttasker serve                 # web UI (board+list, editing, attachments) on 127.0.0.1:8988
agenttasker mcp                   # stdio MCP server
```
Global flags (before or after the subcommand): `-p/--project NAME`, `--db PATH`.
List-valued flags (`--dep`, `--affects`, `--blocker`) REPLACE the list when given;
pass `--dep ''` / `--blocker ''` to clear.


### Stories (feature tracking)

A story is a task that holds sub-tasks (`--story REF`). Progress is derived
(`done/total`). Single level — sub-tasks can't nest further. Plan multi-step
features as a story; work the sub-tasks; the story's `show` reflects progress.

## MCP tools

`add_task` (accepts `parent` for sub-task creation), `get_task`,
`list_tasks` (filters: `ready`/`blocked`/`priority`/`tag`/`stale_hours`,
`all_projects`), `update_task`, `set_status`, `delete_task`, `list_projects`,
`claim_task`, `release_task`, `handoff_task`,
`add_attachment` (base64 in), `list_attachments`, `read_attachment` (base64 out).

Tool errors come back as `ERROR: ...` text — read it, fix the input (usually a bad ref
or unknown status), retry. List fields passed to `update_task` replace; `[]` clears.

Client config:

```json
{
  "mcpServers": {
    "agenttasker": {
      "command": "/abs/path/to/agenttasker",
      "args": ["mcp"],
      "env": {"AGENTTASKER_DB": "/home/USER/.local/share/agenttasker/tasks.db"}
    }
  }
}
```

The server resolves its project from its working directory (git repo name); every tool
also takes an explicit `project` argument to operate cross-project.

## Recipes

Pick up work (the core loop):
```
agenttasker ls --ready                    # P0s first — take the top one
agenttasker claim 14 --owner agent-7      # atomically yours (refuses if taken)
agenttasker update 14 --append-evidence "repro confirmed in tests/e2e"
agenttasker done 14
```

Discovered a dependency mid-task:
```
agenttasker update 14 --dep 7          # 14 now waits on 7
agenttasker ls --blocked               # confirm visibility
```

Blocked externally, parking honestly:
```
agenttasker update 14 --blocker "need write access to staging bucket"
agenttasker release 14 --owner agent-7  # free it for whoever unblocks
agenttasker move 14 --prev
```

Task finished, awaiting human review:
```
agenttasker move 14 --next             # -> in_review
agenttasker update 14 --append-evidence "ran full test suite; 34 passed"
```

Handing an in-flight task to another agent:
```
agenttasker handoff 14 --to agent-9 --from agent-7
```

Attach supporting artifacts (logs, screenshots, datasets):
```
agenttasker attach 14 failure.png
agenttasker attachments 14
```

Machine-to-machine handoff (no hosted service needed):
```
agenttasker export snapshot.json -a     # every project, attachments base64-embedded
# move snapshot.json to the other machine, then:
agenttasker import snapshot.json --dry-run   # preview collisions first
agenttasker import snapshot.json             # merges; name-matched tasks kept and re-linked
```

## Failure modes to avoid

- Working a task you don't own — the claim is the coordination primitive; respect refusals.
- Marking `done` without evidence of verification.
- Free-text task ids in `depends_on` — use numeric refs; external blockers are for prose.
- Editing the export JSON by hand — it is versioned; a mismatched version is rejected.

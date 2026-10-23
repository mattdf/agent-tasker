# AgentTasker

A local-first, project-namespaced task tracker built for AI agents and humans working
on the same projects. GitHub-Projects-style board (CLI + curses TUI), one SQLite file,
no server, no accounts.

- **CLI** — `agenttasker add|ls|show|update|move|done|rm|projects|board|mcp`
- **TUI** — `agenttasker board`: interactive 6-column kanban for humans
- **MCP** — `agenttasker mcp`: stdio MCP server exposing the same board to agents

## Statuses

Board order:

| Status | Meaning |
|---|---|
| `deferred` | parked; revisit later, not in the active set |
| `backlog` | accepted, not yet ready/prioritized |
| `todo` | ready to work once deps/blockers clear |
| `in_progress` | actively being worked (claim before starting) |
| `in_review` | done, awaiting verification |
| `done` | complete and verified |

Tasks carry a **name**, **description**, **evidence** (analysis gathered pre-task),
free-form external **blockers**, **depends_on** (hard task deps), **affects**
(informational links), **priority** (`P0`…`P3`, default `P2`), **type**
(`task`/`feature`/`bugfix`/`improvement`/`chore`), **tags** (workstream labels),
an atomic **owner** claim, and **attachments** (any binary file,
sha256-addressed on disk). Listings sort by status, then priority. A task is *ready*
when it sits in `backlog`/`todo`, has no blockers, and all dependencies are `done`.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[mcp]'   # [mcp] only needed for the MCP server
```

Installs two equivalent commands: `agenttasker` and the short alias `atx`.
Or standalone: `pipx install 'agenttasker[mcp]'` (once published / from a checkout:
`pipx install '/path/to/AgentTasker[mcp]'`).

## Use as a Codex skill

```bash
scripts/install-codex-skill.sh     # copies SKILL.md into ~/.codex/skills/agenttasker/
```

Codex loads skills at session start (restart to pick up changes) and needs a real
file, not a symlink. Invoke explicitly with `$agenttasker`, or let Codex select it
from the description.

## Quick start

```bash
agenttasker add "Fix login redirect" -d "Session cookie lost on 302" -e "traced to SameSite=None" --status todo --priority P1 --tag web
agenttasker add "Write regression test" --dep 1
agenttasker ls --ready              # work that can start now, P0s first
agenttasker claim 1 --owner agent-7 # atomically take it (refuses if held)
agenttasker ls --blocked            # stuck work (unfinished deps or external blockers)
agenttasker update 1 --append-evidence "repro confirmed"
agenttasker done 1
agenttasker board                   # TUI for humans (picks a project if none given)
agenttasker list                    # flat table: id title status pri type blocked-by created
```

## Stories

Track a high-level feature as a story — a task that holds sub-tasks:

```bash
agenttasker story add "OAuth login" -d "Full OAuth2 flow"
agenttasker add "Auth endpoints" --story 1
agenttasker add "Token refresh" --story 1 --priority P1
agenttasker story show 1            # checklist: 1/3 done, per-child status
agenttasker ls --story 1            # just that story's sub-tasks
agenttasker ls --stories            # only stories
agenttasker rm 1 --force            # delete a story (children are detached, not deleted)
```

Stories are plain tasks (full fields, claimable, evidence, events) with derived
progress (`done/total`). Single level — a sub-task can't be a story's parent.

## Git branches

Tag tasks with the branch they're worked on; filter by exact name or `*` wildcards:

```bash
agenttasker add "Fix login" --branch feature/auth-fix
agenttasker ls --branch feature/auth-fix    # exact match
agenttasker ls --branch 'feature/*'         # wildcard
agenttasker update 3 --branch hotfix/crash  # re-tag
```

Branches show in `ls` output (⎇ prefix), `show`, and the web/TUI detail views.


Tasks are namespaced per **project**: the git repo name (or cwd name outside a repo),
overridable with `--project`/`-p` or `$AGENTTASKER_PROJECT`. References accept `12`
or `ProjectName-12`.


## Agent instructions

Per-project instructions that agents read at session start — evidence format,
conventions, working discipline:

```bash
agenttasker instructions                  # view (prints default template if unset)
agenttasker instructions -e               # open in $VISUAL/$EDITOR
agenttasker instructions --set "text"     # set directly
agenttasker instructions --set-file FILE  # set from a file
```

Also readable/writable via the web UI (Instructions button) and MCP
(`get_instructions` / `set_instructions`). Stored in the db, travels with
export/import.
## Storage

One SQLite file, WAL mode:

- default: `~/.local/share/agenttasker/tasks.db`
- override: `AGENTTASKER_DB=/path/to/tasks.db`
- attachments: `<db dir>/attachments/`, content-addressed by sha256

## Coordination, snapshots, attachments

```bash
agenttasker claim REF --owner NAME [--force]   # atomic ownership; also sets in_progress
agenttasker release REF [--owner NAME]         # give a task back
agenttasker handoff REF --to NAME [--from ME]  # explicit transfer
agenttasker claims [--stale HOURS]             # who holds what, and what's rotting

agenttasker attach REF FILE                    # any binary (image, zip, json…)
agenttasker attachments REF
agenttasker detach REF ID

agenttasker export [PATH] [-a]                 # portable JSON; attachments base64-embedded
agenttasker import PATH [--mode merge|replace] [--dry-run]
```

## Web UI

```bash
agenttasker serve            # http://127.0.0.1:8988 (opens your browser)
agenttasker serve -p PROJ --port 9000 --no-browser
```

Same features as the TUI plus adding/deleting tasks: board and list views,
full field editing, status moves, claim/release, evidence appends, and
attachments — click one to open it in a new tab, or upload new files.
Polls for changes every 2s. Stdlib only; binds 127.0.0.1 by default.

## TUI keys

`agenttasker board [-p PROJECT]`

| Key | Action |
|---|---|
| `←`/`→` or `h`/`l` | select column |
| `↑`/`↓` or `k`/`j` | select task |
| `Enter` | full task detail — `↑↓` selects a field, `e` edits it, `q` closes |
| `m` | move task (status menu) |
| `<`/`,` and `>`/`.` | shift task one column left/right |
| `r` | manual reload (the board also auto-reloads when the db changes) |
| `v` | switch board ↔ list view (priority table, same columns as `list`) |

Cards are bordered mini-cards: a 2-line inverted title head in the status color,
a rule, then a 4-line description/evidence body and a deps/blockers/ready meta
line. `!` = blocked (red), `*` = ready, dimmed = done. The board watches the
database file and its WAL side files, so changes from the CLI, MCP agents, or
other TUI sessions appear within ~0.5s.

## MCP

Run the stdio MCP server:

```bash
agenttasker mcp
```

Client config (Claude Desktop / any MCP client):

```json
{
  "mcpServers": {
    "agenttasker": {
      "command": "/path/to/.venv/bin/agenttasker",
      "args": ["mcp"],
      "env": { "AGENTTASKER_DB": "/home/you/.local/share/agenttasker/tasks.db" }
    }
  }
}
```

The server resolves its project from its working directory (git repo name), or
`$AGENTTASKER_PROJECT`; every tool also takes an explicit `project` argument.
Tools: `add_task`, `get_task`, `list_tasks` (filters: `ready`/`blocked`/`priority`/`tag`/`stale_hours`),
`update_task`, `set_status`, `delete_task`, `list_projects`,
`claim_task`, `release_task`, `handoff_task`,
`add_attachment` (base64 in), `list_attachments`, `read_attachment` (base64 out).

Agents: see [SKILL.md](SKILL.md) for the full working discipline.

## Development

```bash
python3 -m unittest discover -s tests
```

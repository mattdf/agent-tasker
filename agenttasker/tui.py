"""Interactive curses kanban board (TUI) for humans.

Keys:
    h/Left  l/Right   select column        j/Down  k/Up  select task
    Enter             task details         m            move task (status menu)
    < ,    >  .       move card left/right r            manual reload
    ?                 help                 q            quit

The board auto-reloads when the task database changes (other agents/humans
writing via the CLI/MCP appear within ~0.5s). Cards span 4 lines: name,
two-line description/evidence excerpt, and a deps/blockers/ready meta line.
"""

from __future__ import annotations

import curses
import os
import textwrap

from .core import (
    DEFAULT_PRIORITY,
    DEFAULT_TYPE,
    DONE,
    STARTABLE_STATUSES,
    STATUSES,
    LIST_STATUS_ORDER,
    Store,
    Task,
    TaskError,
    claim_age_hours,
    is_blocked,
    is_ready,
    next_status,
    prev_status,
    priority_label,
    status_label,
    unfinished_dep_ids,
    utcnow,
)

CARD_HEIGHT = 11  # bordered card: border, 2-line title head, rule, 4-line body, meta, border (+gap)
CARD_INNER_MIN = 8
MIN_COL_WIDTH = 16
WATCH_MS = 400  # db poll interval while idle

_HELP = """\
Board keys
  h / Left, l / Right    select column
  j / Down, k / Up       select task
  Enter                  task details (↑↓ select field · e: edit · q: close)
  m                      move task to another status (menu)
  < or ,  /  > or .      move task one column left / right
  r                      manual reload
  v                      switch board <-> list view (list: priority table)
  ?                      this help

The board watches the database and auto-reloads when other agents or
humans change tasks (CLI, MCP, or another TUI session).

Legend
  !    blocked: unfinished dependencies or external blockers (red)
  *    ready to start now
  card line 1  task name (status color)
  card lines 2-3  description / evidence excerpt
  card line 4  deps / external blockers / ready state
"""


def _safe(scr, y: int, x: int, text: str, attr: int = 0) -> None:
    """addstr that ignores out-of-bounds (curses raises at the last cell)."""
    try:
        scr.addstr(y, x, text, attr)
    except curses.error:
        pass


class Board:
    def __init__(self, store: Store, project: str):
        self.store = store
        self.project = project
        self.tasks: list[Task] = []
        self.by_id: dict[int, Task] = {}
        self.cols: dict[str, list[Task]] = {s: [] for s in STATUSES}
        self.col = 0
        self.row = 0
        self.offsets: dict[str, int] = {}
        self.done = False
        self.message = ""
        self.view = "board"  # "board" | "list"
        self.list_row = 0
        self.list_off = 0

    # --- data
    def reload(self) -> None:
        """Reload from db, keeping the selection on the same task when possible."""
        selected = self.selected()
        keep_id = selected.id if selected is not None else None
        self.tasks = self.store.list_tasks(self.project)
        self.by_id = {t.id: t for t in self.tasks}
        self.evidence_tail = {
            t.id: entries[-1]["text"]
            for t in self.tasks
            if (entries := self.store.evidence(t))
        }
        self.cols = {s: [t for t in self.tasks if t.status == s] for s in STATUSES}
        if keep_id is not None:
            for ci, status in enumerate(STATUSES):
                for ri, task in enumerate(self.cols[status]):
                    if task.id == keep_id:
                        self.col, self.row = ci, ri
                        break
        self.clamp_selection()
        self.list_row = max(0, min(self.list_row, max(0, len(self.tasks) - 1)))

    def clamp_selection(self) -> None:
        self.col = max(0, min(self.col, len(STATUSES) - 1))
        col_tasks = self.cols[STATUSES[self.col]]
        self.row = max(0, min(self.row, max(0, len(col_tasks) - 1)))

    def selected(self) -> Task | None:
        col_tasks = self.cols[STATUSES[self.col]]
        if 0 <= self.row < len(col_tasks):
            return col_tasks[self.row]
        return None

    def dep_status_map(self) -> dict[int, str]:
        return {t.id: t.status for t in self.tasks}

    # --- db watching
    def db_stamp(self):
        """Change detector across the db and its WAL side files (WAL writes
        update -wal/-shm mtimes, not the main file's)."""
        stamp = []
        for suffix in ("", "-wal", "-shm"):
            path = str(self.store.path) + suffix
            try:
                info = os.stat(path)
                stamp.append((info.st_mtime_ns, info.st_size))
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    # --- colors
    def init_colors(self) -> None:
        self.pairs: dict[str, int] = {}
        try:
            curses.start_color()
            curses.use_default_colors()
            palette = {
                "deferred": 8,     # gray
                "backlog": 6,      # cyan
                "todo": 7,         # white
                "in_progress": 3,  # yellow
                "in_review": 5,    # magenta
                "done": 2,         # green
                "alert": 1,        # red
            }
            for i, (name, color) in enumerate(palette.items(), start=1):
                curses.init_pair(i, color, -1)
                self.pairs[name] = curses.color_pair(i)
        except curses.error:
            self.pairs = {name: 0 for name in
                          ("deferred", "backlog", "todo", "in_progress",
                           "in_review", "done", "alert")}

    # --- main loop
    def run(self, stdscr) -> int:
        curses.curs_set(0)
        stdscr.keypad(True)
        self.init_colors()
        self.reload()
        stdscr.timeout(WATCH_MS)
        stamp = self.db_stamp()
        while not self.done:
            self.draw(stdscr)
            ch = stdscr.getch()
            if ch == curses.ERR:  # idle tick: poll the db for outside changes
                current = self.db_stamp()
                if current != stamp:
                    stamp = current
                    self.reload()
                    self.message = "· db changed — reloaded"
                continue
            self.handle(stdscr, ch)
            stamp = self.db_stamp()  # our own writes must not read as foreign
        return 0

    # --- drawing
    VIEW_CYCLE = ("board", "list", "agents")

    def draw(self, stdscr) -> None:
        if self.view == "list":
            self.draw_list(stdscr)
            return
        if self.view == "agents":
            self.draw_agents(stdscr)
            return
        self.draw_board(stdscr)

    def view_tasks(self) -> list[Task]:
        """Tasks shown by the current non-board view (drives selection)."""
        if self.view == "agents":
            return sorted(
                (t for t in self.tasks if t.owner),
                key=lambda t: (t.owner, t.priority, t.id),
            )
        return self.list_tasks_sorted()

    def draw_list(self, stdscr) -> None:
        """Flat priority-ordered table of every task in the project."""
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        _safe(stdscr, 0, 0, f" {self.project} — list view · todo first, done last", curses.A_BOLD)
        _safe(stdscr, 1, 0,
              " ↑↓ select · Enter: details · v: next view (board/list/agents) · q: quit",
              curses.A_DIM)

        tasks = self.view_tasks()
        self.list_row = max(0, min(self.list_row, max(0, len(tasks) - 1)))

        id_w, st_w, pri_w, ty_w, bl_w, cr_w = 6, 11, 3, 11, 14, 10
        title_w = max(10, width - (id_w + st_w + pri_w + ty_w + bl_w + cr_w + 6))
        header = (f"{'ID':<{id_w}} {'TITLE':<{title_w}} {'STATUS':<{st_w}} "
                  f"{'PRI':<{pri_w}} {'TYPE':<{ty_w}} {'BLOCKED-BY':<{bl_w}} CREATED")
        _safe(stdscr, 2, 0, header[: width - 1], curses.A_BOLD | curses.A_UNDERLINE)

        dep_map = self.dep_status_map()
        visible = height - 4
        if self.list_row < self.list_off:
            self.list_off = self.list_row
        elif self.list_row >= self.list_off + visible:
            self.list_off = self.list_row - visible + 1
        self.list_off = max(0, min(self.list_off, max(0, len(tasks) - visible)))

        for i in range(self.list_off, min(len(tasks), self.list_off + visible)):
            t = tasks[i]
            pending = unfinished_dep_ids(t, dep_map)
            blocked = ",".join(f"#{p}" for p in pending[:2])
            if t.blockers:
                blocked = (blocked + "+" if blocked else "") + f"ext:{len(t.blockers)}"
            row = (f"{t.id:<{id_w}} {t.name[:title_w - 1]:<{title_w}} {t.status:<{st_w}} "
                   f"{priority_label(t.priority):<{pri_w}} {t.type:<{ty_w}} "
                   f"{(blocked or '—'):<{bl_w}} {t.created_at[:10]}")
            attr = 0
            if i == self.list_row:
                attr |= curses.A_REVERSE
            elif t.status == DONE:
                attr |= curses.A_DIM
            _safe(stdscr, 3 + i - self.list_off, 0, row[: width - 1], attr)

        if not tasks:
            _safe(stdscr, 3, 0, " (no tasks)", curses.A_DIM)
        more = " ↓ more" if self.list_off + visible < len(tasks) else ""
        _safe(stdscr, height - 1, 0, f" {len(tasks)} task(s){more}", curses.A_DIM)
        stdscr.noutrefresh()
        curses.doupdate()

    def draw_agents(self, stdscr) -> None:
        """Who is working on what: claimed tasks grouped by owner."""
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        _safe(stdscr, 0, 0, f" {self.project} — agents · active claims", curses.A_BOLD)
        _safe(stdscr, 1, 0,
              " ↑↓ select · Enter: details · v: next view (board/list/agents) · q: quit",
              curses.A_DIM)

        tasks = self.view_tasks()
        self.list_row = max(0, min(self.list_row, max(0, len(tasks) - 1)))

        id_w, ow_w, st_w, pri_w, ty_w = 6, 18, 11, 3, 11
        cl_w, cr_w = 17, 10
        title_w = max(10, width - (id_w + ow_w + st_w + pri_w + ty_w + cl_w + cr_w + 7))
        header = (f"{'ID':<{id_w}} {'OWNER':<{ow_w}} {'TITLE':<{title_w}} {'STATUS':<{st_w}} "
                  f"{'PRI':<{pri_w}} {'TYPE':<{ty_w}} {'CLAIMED':<{cl_w}} AGE")
        _safe(stdscr, 2, 0, header[: width - 1], curses.A_BOLD | curses.A_UNDERLINE)

        visible = height - 4
        if self.list_row < self.list_off:
            self.list_off = self.list_row
        elif self.list_row >= self.list_off + visible:
            self.list_off = self.list_row - visible + 1
        self.list_off = max(0, min(self.list_off, max(0, len(tasks) - visible)))

        for i in range(self.list_off, min(len(tasks), self.list_off + visible)):
            t = tasks[i]
            age = claim_age_hours(t)
            age_txt = f"{age:.1f}h" if age is not None else "—"
            row = (f"{t.id:<{id_w}} {t.owner[:ow_w - 1]:<{ow_w}} {t.name[:title_w - 1]:<{title_w}} "
                   f"{t.status:<{st_w}} {priority_label(t.priority):<{pri_w}} {t.type:<{ty_w}} "
                   f"{t.claimed_at[:16]:<{cl_w}} {age_txt}")
            attr = curses.A_REVERSE if i == self.list_row else 0
            _safe(stdscr, 3 + i - self.list_off, 0, row[: width - 1], attr)

        if not tasks:
            _safe(stdscr, 3, 0, " (no active claims)", curses.A_DIM)
        more = " ↓ more" if self.list_off + visible < len(tasks) else ""
        _safe(stdscr, height - 1, 0, f" {len(tasks)} claim(s){more}", curses.A_DIM)
        stdscr.noutrefresh()
        curses.doupdate()

    def list_tasks_sorted(self) -> list[Task]:
        order = {s: i for i, s in enumerate(LIST_STATUS_ORDER)}
        return sorted(self.tasks, key=lambda t: (order[t.status], t.priority, t.id))
    def draw_board(self, stdscr) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        _safe(stdscr, 0, 0, f" {self.project} — agenttasker board", curses.A_BOLD)
        _safe(
            stdscr, 1, 0,
            " ←→ column  ↑↓ task  Enter: details  m: move  </>: shift  v: list  ?: help  q: quit"
            "  · auto-reload on db change",
            curses.A_DIM,
        )

        n = len(STATUSES)
        gap = 1
        col_w = (width - gap * (n - 1)) // n
        if col_w < MIN_COL_WIDTH:
            _safe(
                stdscr, 3, 0,
                f" terminal too narrow ({width} cols); need >= {MIN_COL_WIDTH * n + gap * (n - 1)}",
                curses.A_BOLD,
            )
            stdscr.noutrefresh()
            curses.doupdate()
            return

        top = 2
        body_h = height - top - 1
        max_cards = body_h // CARD_HEIGHT

        for ci, status in enumerate(STATUSES):
            x = ci * (col_w + gap)
            w = col_w if ci < n - 1 else max(col_w, width - x)
            col_tasks = self.cols[status]

            # keep the selected card visible
            if ci == self.col:
                off = self.offsets.get(status, 0)
                if self.row < off:
                    off = self.row
                elif self.row >= off + max_cards:
                    off = self.row - max_cards + 1
                self.offsets[status] = max(0, off)
            off = self.offsets.get(status, 0)

            header = f" {status_label(status)} ({len(col_tasks)})"
            attr = self.pairs.get(status, 0) | curses.A_BOLD
            if ci == self.col:
                attr |= curses.A_UNDERLINE
            _safe(stdscr, top, x, header[:w], attr)

            y = top + 1
            for i in range(off, min(len(col_tasks), off + max_cards)):
                task = col_tasks[i]
                self.draw_card(
                    stdscr, task, y, x, w,
                    selected=(ci == self.col and i == self.row),
                )
                y += CARD_HEIGHT

        if max_cards == 0:
            _safe(stdscr, top + 1, 0, " terminal too short to show cards", curses.A_DIM)

        footer = self.message if self.message else self.footer_text()
        _safe(stdscr, height - 1, 0, footer[: width - 1], curses.A_DIM)
        stdscr.noutrefresh()
        curses.doupdate()

    def draw_card(self, stdscr, task: Task, y: int, x: int, w: int, selected: bool) -> None:
        """Bordered mini-card: inverted 2-line title head, rule, then body."""
        dep_map = self.dep_status_map()
        blocked = is_blocked(task, dep_map)
        ready = is_ready(task, dep_map)
        inner = max(CARD_INNER_MIN, w - 4)

        border_attr = self.pairs.get(task.status, 0)
        if task.status == DONE:
            border_attr |= curses.A_DIM
        if selected:
            border_attr |= curses.A_BOLD

        # head: inverted bar in the status color, title forced to wrap over 2 lines
        mark = "!" if blocked else ("*" if ready else " ")
        title_attr = self.pairs.get(task.status, 0) | curses.A_BOLD | curses.A_REVERSE
        if task.status == DONE:
            title_attr |= curses.A_DIM
        title_lines = textwrap.wrap(
            f"#{task.id} {mark} {task.name}", width=inner, max_lines=2, placeholder=" …"
        ) or [""]
        while len(title_lines) < 2:
            title_lines.append("")

        # body: description/evidence excerpt (4 lines) + deps/blockers/ready meta
        body_attr = curses.A_REVERSE if selected else 0  # full-contrast body text
        body = task.description.strip() or self.evidence_tail.get(task.id, "")
        excerpt = (
            textwrap.wrap(body, width=inner, max_lines=4, placeholder=" …") if body else []
        )
        while len(excerpt) < 4:
            excerpt.append("")
        parts: list[str] = []
        if task.priority != DEFAULT_PRIORITY:
            parts.append(priority_label(task.priority))
        if task.type != DEFAULT_TYPE:
            parts.append(task.type)
        if task.owner:
            parts.append("@" + task.owner)
        if task.depends_on:
            shown = ",".join(f"#{i}" for i in task.depends_on[:3])
            parts.append("deps:" + shown + ("…" if len(task.depends_on) > 3 else ""))
        if task.blockers:
            parts.append(f"ext:{len(task.blockers)}")
        if ready:
            parts.append("ready")
        meta_attr = self.pairs["alert"] if blocked else 0
        if selected:
            meta_attr |= curses.A_REVERSE

        _safe(stdscr, y, x, ("┌" + "─" * max(0, w - 2) + "┐")[:w], border_attr)
        rule = ("├" + "─" * max(0, w - 2) + "┤")[:w]
        rows = [
            (title_lines[0], title_attr),
            (title_lines[1], title_attr),
            (rule, border_attr),
            (excerpt[0], body_attr),
            (excerpt[1], body_attr),
            (excerpt[2], body_attr),
            (excerpt[3], body_attr),
            (" · ".join(parts), meta_attr),
        ]
        for i, (line, attr) in enumerate(rows):
            if line == rule:  # head/body separator spans the full card width
                _safe(stdscr, y + 1 + i, x, rule, attr)
                continue
            padded = line[:inner].ljust(inner)
            _safe(stdscr, y + 1 + i, x, f"│ {padded} │"[:w], attr)
        _safe(stdscr, y + 9, x, ("└" + "─" * max(0, w - 2) + "┘")[:w], border_attr)

    def footer_text(self) -> str:
        task = self.selected()
        if task is None:
            col = status_label(STATUSES[self.col])
            return f" {col}: no tasks. add one with `agenttasker add \"...\" -p {self.project}`"
        dep_map = self.dep_status_map()
        pending = unfinished_dep_ids(task, dep_map)
        bits = [f" #{task.id} {task.name} [{status_label(task.status)}]"]
        if pending:
            bits.append("blocked by: " + ",".join(f"#{i}" for i in pending))
        elif task.blockers:
            bits.append(f"external blockers: {len(task.blockers)}")
        return " ".join(bits)

    # --- input
    def handle(self, stdscr, ch: int) -> None:
        self.message = ""
        if ch in (ord("v"), ord("V")):  # cycle board -> list -> agents
            idx = self.VIEW_CYCLE.index(self.view) if self.view in self.VIEW_CYCLE else 0
            self.view = self.VIEW_CYCLE[(idx + 1) % len(self.VIEW_CYCLE)]
        elif self.view in ("list", "agents"):
            self.handle_list(stdscr, ch)
        elif ch in (ord("q"), ord("Q")):
            self.done = True
        elif ch in (curses.KEY_LEFT, ord("h"), ord("H")):
            self.col = max(0, self.col - 1)
            self.clamp_selection()
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("L")):
            self.col = min(len(STATUSES) - 1, self.col + 1)
            self.clamp_selection()
        elif ch in (curses.KEY_UP, ord("k"), ord("K")):
            self.row = max(0, self.row - 1)
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            col_tasks = self.cols[STATUSES[self.col]]
            self.row = min(max(0, len(col_tasks) - 1), self.row + 1)
        elif ch in (curses.KEY_ENTER, 10, 13):
            task = self.selected()
            if task is not None:
                self.view_popup(stdscr, task)
        elif ch in (ord("m"), ord("M")):
            self.move_menu(stdscr)
        elif ch in (ord("<"), ord(",")):
            self.shift(-1)
        elif ch in (ord(">"), ord(".")):
            self.shift(1)
        elif ch in (ord("r"), ord("R")):
            self.reload()
            self.flash("reloaded")
        elif ch == ord("?"):
            self.help_popup(stdscr)
        elif ch == curses.KEY_RESIZE:
            self.offsets.clear()


    def handle_list(self, stdscr, ch: int) -> None:
        tasks = self.view_tasks()
        if ch in (ord("q"), ord("Q")):
            self.done = True
        elif ch in (curses.KEY_UP, ord("k"), ord("K")):
            self.list_row = max(0, self.list_row - 1)
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            self.list_row = min(max(0, len(tasks) - 1), self.list_row + 1)
        elif ch == curses.KEY_NPAGE:
            self.list_off = min(max(0, len(tasks) - 1), self.list_off + max(1, 10))
            self.list_row = self.list_off
        elif ch == curses.KEY_PPAGE:
            self.list_off = max(0, self.list_off - max(1, 10))
            self.list_row = self.list_off
        elif ch in (curses.KEY_ENTER, 10, 13):
            if 0 <= self.list_row < len(tasks):
                self.view_popup(stdscr, tasks[self.list_row])
        elif ch in (ord("r"), ord("R")):
            self.reload()
        elif ch == curses.KEY_RESIZE:
            self.list_off = 0

    def shift(self, direction: int) -> None:
        task = self.selected()
        if task is None:
            return
        try:
            target = next_status(task.status) if direction > 0 else prev_status(task.status)
        except TaskError as exc:
            self.flash(str(exc))
            return
        self.apply_move(task, target)

    def apply_move(self, task: Task, target: str) -> None:
        old = task.status
        if target == old:
            return
        self.store.set_status(task, target)
        self.reload()
        self.col = STATUSES.index(target)
        col_tasks = self.cols[target]
        for i, t in enumerate(col_tasks):
            if t.id == task.id:
                self.row = i
                break
        self.flash(f"#{task.id} {old} -> {target}")

    def flash(self, message: str) -> None:
        self.message = message  # shown in footer on next draw

    # --- popups (drawn on the shared stdscr; the main loop redraws the board after)
    def _popup_geometry(self, lines, title: str, scr_h: int, scr_w: int):
        longest = max(
            [len(title)] + [len(l[0] if isinstance(l, tuple) else l) for l in lines] + [30]
        )
        w = max(30, min(longest + 4, scr_w - 2))
        h = min(len(lines) + 4, scr_h - 2)
        y = max(0, (scr_h - h) // 2)
        x = max(0, (scr_w - w) // 2)
        return h, w, y, x

    def _draw_box(self, h: int, w: int, y: int, x: int, title: str):
        win = curses.newwin(h, w, y, x)
        win.box()
        _safe(win, 0, 2, f" {title} "[: w - 2], curses.A_BOLD)
        return win

    def view_popup(self, stdscr, task: Task) -> None:
        """Field-based task view: arrows select fields, 'e' edits, 'q' closes."""
        stdscr.timeout(-1)  # blocking input while a popup is open
        try:
            self._view_loop(stdscr, task)
        finally:
            stdscr.timeout(WATCH_MS)

    def _view_loop(self, stdscr, task: Task) -> None:
        sel, offset, message, follow = 0, 0, "", True
        while True:
            blocks = self._field_blocks(task)
            sel = max(0, min(sel, len(blocks) - 1))
            height, width = stdscr.getmaxyx()
            disp: list[tuple[str, int, int]] = []  # (text, attr, block index)
            for bi, b in enumerate(blocks):
                attr = curses.A_BOLD | (curses.A_REVERSE if bi == sel else 0)
                marker = "▸ " if bi == sel else "  "
                disp.append((f"{marker}{b['label']}", attr, bi))
                for text, a in b["lines"]:
                    if text == "─":
                        disp.append(("SEP", a, bi))
                        continue
                    # word-wrap long content (evidence/description) to the popup width
                    for part in textwrap.wrap(text, width=max(10, width - 12)) or [""]:
                        disp.append((f"  {part}", a, bi))
                disp.append(("", 0, bi))

            title = f"{task.project}-{task.id}"
            h, w, y, x = self._popup_geometry([d[0] for d in disp], title, height, width)
            max_lines = max(1, h - 3)
            if follow:  # keep the selected field visible when selection moves
                sel_line = next(i for i, d in enumerate(disp) if d[2] == sel)
                if sel_line < offset:
                    offset = sel_line
                elif sel_line >= offset + max_lines:
                    offset = sel_line - max_lines + 1
            offset = max(0, min(offset, max(0, len(disp) - max_lines)))

            win = self._draw_box(h, w, y, x, title)
            for i in range(max_lines):
                idx = offset + i
                if idx >= len(disp):
                    break
                text, attr, _ = disp[idx]
                if text == "SEP":
                    text, attr = "─" * (w - 6), attr | curses.A_DIM
                _safe(win, 1 + i, 2, text[: w - 3], attr)
            more = " ↓" if offset + max_lines < len(disp) else ""
            hint = f" ↑↓ field · PgUp/PgDn scroll{more} · e: edit · q: close{' · ' + message if message else ''}"
            _safe(win, h - 1, 2, hint[: w - 3], curses.A_DIM)
            win.noutrefresh()
            curses.doupdate()

            ch = stdscr.getch()
            message = ""
            follow = True
            if ch in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = min(len(blocks) - 1, sel + 1)
            elif ch == curses.KEY_NPAGE:  # scroll without moving the selection
                offset = min(max(0, len(disp) - max_lines), offset + max_lines)
                follow = False
            elif ch == curses.KEY_PPAGE:
                offset = max(0, offset - max_lines)
                follow = False
            elif ch in (ord("e"), ord("E"), curses.KEY_ENTER, 10, 13):
                block = blocks[sel]
                if block["key"]:
                    try:
                        updated = self._edit_field(stdscr, task, block["key"])
                        if updated is not None:
                            task = updated
                            self.reload()
                            message = "saved"
                    except TaskError as exc:
                        message = f"error: {exc}"
            elif ch in (ord("q"), ord("Q"), 27, curses.KEY_RESIZE):
                return

    def _field_blocks(self, task: Task) -> list[dict]:
        dim = curses.A_DIM
        bold = curses.A_BOLD
        status_attr = self.pairs.get(task.status, 0) | bold
        alert = self.pairs["alert"]
        good = self.pairs["done"]
        dep_map = self.dep_status_map()

        def dep_lines(ids, emphasis: bool):
            out = []
            for dep_id in ids:
                dep = self.by_id.get(dep_id)
                if dep is None:
                    out.append((f"#{dep_id} (missing)", alert))
                else:
                    attr = dim if dep.status == DONE else (alert if emphasis else bold)
                    out.append((f"#{dep.id} ({dep.status}) {dep.name}", attr))
            return out or [("(none)", dim)]

        def block(key, label, lines):
            return {"key": key, "label": label, "lines": lines}

        if task.status == DONE:
            state = ("done", good)
        elif is_blocked(task, dep_map):
            pending = unfinished_dep_ids(task, dep_map)
            why = []
            if pending:
                why.append("deps " + ",".join(f"#{i}" for i in pending))
            if task.blockers:
                why.append(f"{len(task.blockers)} external blocker(s)")
            state = ("BLOCKED — " + " + ".join(why), alert)
        elif is_ready(task, dep_map):
            state = ("ready to start", good)
        else:
            state = ("in flight", dim)
        dependents = [t for t in self.tasks if task.id in t.depends_on and t.id != task.id]

        age = claim_age_hours(task)
        owner_text = f"{task.owner} (claimed {age:.1f}h ago)" if (task.owner and age is not None) else (task.owner or "unclaimed")
        def human_size(n: float) -> str:
            for unit in ("B", "KB", "MB", "GB"):
                if n < 1024 or unit == "GB":
                    return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
                n /= 1024

        atts = self.store.list_attachments(task)
        att_lines = [
            (f"#{a['id']}  {a['filename']}  ({human_size(a['size'])})", dim) for a in atts
        ] or [("(none)", dim)]

        return [
            block("name", "TITLE", [(task.name, 0)]),
            block("status", "STATUS", [(status_label(task.status), status_attr)]),
            block("priority", "PRIORITY", [(priority_label(task.priority), bold)]),
            block("type", "TYPE", [(task.type, bold)]),
            block(None, "PROJECT", [(task.project, dim)]),
            block(None, "OWNER", [(owner_text, alert if task.owner else dim)]),
            block("description", "DESCRIPTION",
                  [("─", 0)] + [(l, 0) for l in (task.description.splitlines() or ["(none)"])]),
            block("evidence", "EVIDENCE / PRE-TASK ANALYSIS",
                  [(f"[{e['ts'][:19]}] {e['text']}", dim)
                   for e in self.store.evidence(task)] or [("(none)", dim)]),
            block("blockers", "BLOCKERS (EXTERNAL)",
                  [(f"• {b}", alert) for b in task.blockers] or [("(none)", dim)]),
            block("depends_on", "DEPENDS ON", dep_lines(task.depends_on, True)),
            block("affects", "AFFECTS", dep_lines(task.affects, False)),
            block(None, "BLOCKS (TASKS DEPENDING ON THIS)", dep_lines([t.id for t in dependents], True)),
            block(None, "SUB-TASKS (STORY)", [
                (f"#{c.id} ({c.status}) {c.name}",
                 dim if c.status == DONE else bold)
                for c in self.store.children(task)
            ]) if self.store.children(task) else None,
            block(None, "EVENT LOG", [
                (f"{e['ts'][:19]}  {e['kind']:<8} {e['detail']}", dim)
                for e in self.store.events(task, 20)
            ] or [("(no events yet)", dim)]),
            block(None, "STATE", [("─", 0), state]),
            block(None, "DATES", [(f"created {task.created_at}  ·  updated {task.updated_at}", dim)]),
        ]

    # --- field editors
    def _edit_field(self, stdscr, task: Task, key: str) -> Task | None:
        """Edit one field. Returns the updated task, or None if cancelled."""
        if key == "status":
            options = [status_label(s) for s in STATUSES]
            idx = self._menu(stdscr, "Set status", options, STATUSES.index(task.status))
            if idx is None:
                return None
            return self.store.set_status(task, STATUSES[idx])
        if key == "name":
            value = self._input_box(stdscr, "Title", task.name)
            if value is None or not value.strip():
                return None
            return self.store.update(task, name=value.strip())
        if key in ("depends_on", "affects"):
            labels = {
                "depends_on": "Depends on (comma-separated refs, empty clears)",
                "affects": "Affects (comma-separated refs, empty clears)",
            }
            current = ", ".join(f"#{i}" for i in getattr(task, key))
            value = self._input_box(stdscr, labels[key], current)
            if value is None:
                return None
            refs = [r.strip() for r in value.split(",") if r.strip()]
            return self.store.update(task, **{key: refs})
        if key == "priority":
            options = [f"{p} — {label}" for p, label in
                       zip(("P0", "P1", "P2", "P3"), ("urgent", "high", "normal", "low"))]
            idx = self._menu(stdscr, "Priority", options, task.priority)
            if idx is None:
                return None
            return self.store.update(task, priority=idx)
        if key == "type":
            idx = self._menu(stdscr, "Type", list(TASK_TYPES), TASK_TYPES.index(task.type))
            if idx is None:
                return None
            return self.store.update(task, type=TASK_TYPES[idx])
        if key == "tags":
            value = self._input_box(stdscr, "Tags (comma-separated)", ", ".join(task.tags))
            if value is None:
                return None
            return self.store.update(task, tags=[t for t in value.split(",") if t.strip()])
        if key == "blockers":
            text = self._edit_in_editor("\n".join(task.blockers))
            if text is None:
                return None
            return self.store.update(
                task, blockers=[l.strip() for l in text.splitlines() if l.strip()]
            )
        if key == "evidence":
            # edit renders rows as "[ts] text" lines; timestamps round-trip
            entries = self.store.evidence(task)
            rendered = "\n".join(f"[{e['ts']}] {e['text']}" for e in entries)
            text = self._edit_in_editor(rendered)
            if text is None:
                return None
            from .db import parse_evidence_blob
            parsed = parse_evidence_blob(text, utcnow())
            self.store.set_evidence_rows(task, parsed)
            return self.store.get(task.project, task.id)
        if key == "description":
            text = self._edit_in_editor(task.description)
            if text is None:
                return None
            return self.store.update(task, description=text.rstrip("\n"))
        return None

    def _input_box(self, stdscr, title: str, initial: str = "") -> str | None:
        """Single-line input popup. Enter accepts, Esc cancels."""
        height, width = stdscr.getmaxyx()
        w = min(max(40, len(initial) + 12), width - 2)
        h = 5
        y, x = max(0, (height - h) // 2), max(0, (width - w) // 2)
        value = initial
        while True:
            win = self._draw_box(h, w, y, x, title)
            shown = value if len(value) <= w - 6 else "…" + value[-(w - 7):]
            _safe(win, 1, 2, shown[: w - 3])
            _safe(win, h - 1, 2, " Enter save · Esc cancel ", curses.A_DIM)
            win.noutrefresh()
            curses.doupdate()
            ch = stdscr.getch()
            if ch in (10, 13, curses.KEY_ENTER):
                return value
            if ch == 27:
                return None
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                value = value[:-1]
            elif 32 <= ch < 127:
                value += chr(ch)

    def _edit_in_editor(self, text: str) -> str | None:
        """Edit text in $VISUAL/$EDITOR: suspend curses, run, resume."""
        import shlex
        import subprocess
        import tempfile

        fd, path = tempfile.mkstemp(prefix="agenttasker-", suffix=".md")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
            try:
                curses.def_prog_mode()
                curses.endwin()
                subprocess.run(shlex.split(editor) + [path], check=False)
            finally:
                curses.reset_prog_mode()
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def help_popup(self, stdscr) -> None:
        self._scroll_popup(stdscr, "Help", _HELP.splitlines())

    def _scroll_popup(self, stdscr, title: str, lines: list) -> None:
        """lines: plain strings or (text, attr) tuples; '─' rows become separators."""
        stdscr.timeout(-1)
        try:
            self._scroll_loop(stdscr, title, lines)
        finally:
            stdscr.timeout(WATCH_MS)

    def _scroll_loop(self, stdscr, title: str, lines: list) -> None:
        height, width = stdscr.getmaxyx()
        wrapped: list[tuple[str, int]] = []
        for line in lines:
            if isinstance(line, tuple):
                text, attr = line
            else:
                text, attr = line, 0
            if text == "─":
                wrapped.append(("SEPARATOR", attr))
                continue
            for part in textwrap.wrap(text, width=max(10, width - 8)) or [""]:
                wrapped.append((part, attr))
        h, w, y, x = self._popup_geometry(wrapped, title, height, width)
        max_lines = max(1, h - 3)
        off = 0
        while True:
            win = self._draw_box(h, w, y, x, title)
            for i in range(max_lines):
                idx = off + i
                if idx >= len(wrapped):
                    break
                text, attr = wrapped[idx]
                if text == "SEPARATOR":
                    text, attr = "─" * (w - 4), attr | curses.A_DIM
                _safe(win, 1 + i, 2, text[: w - 3], attr)
            if off + max_lines < len(wrapped):
                _safe(win, h - 1, 2, " ↓ more ", curses.A_DIM)
            _safe(win, h - 1, max(2, w - 22), " ↑↓ scroll  q close ", curses.A_DIM)
            win.noutrefresh()
            curses.doupdate()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                off = max(0, off - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                off = min(max(0, len(wrapped) - max_lines), off + 1)
            elif ch in (curses.KEY_ENTER, 10, 13, ord("q"), ord("Q"), 27, curses.KEY_RESIZE):
                return

    def _menu(self, stdscr, title: str, options: list[str], current: int) -> int | None:
        """Arrow-select popup. Returns chosen index, or None on cancel."""
        stdscr.timeout(-1)
        try:
            return self._menu_loop(stdscr, title, options, current)
        finally:
            stdscr.timeout(WATCH_MS)

    def _menu_loop(self, stdscr, title: str, options: list[str], current: int) -> int | None:
        sel = current
        height, width = stdscr.getmaxyx()
        h, w, y, x = self._popup_geometry(options, title, height, width)
        while True:
            win = self._draw_box(h, w, y, x, title)
            for i, opt in enumerate(options):
                attr = curses.A_REVERSE if i == sel else 0
                _safe(win, 1 + i, 2, f" {opt} "[: w - 3], attr)
            _safe(win, h - 1, 2, " ↑↓ select  Enter confirm  Esc cancel ", curses.A_DIM)
            win.noutrefresh()
            curses.doupdate()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = min(len(options) - 1, sel + 1)
            elif ch in (curses.KEY_ENTER, 10, 13):
                return sel
            elif ch in (27, ord("q"), curses.KEY_RESIZE):
                return None

    def move_menu(self, stdscr) -> None:
        task = self.selected()
        if task is None:
            return
        options = [status_label(s) for s in STATUSES]
        idx = self._menu(stdscr, f"Move #{task.id}", options, STATUSES.index(task.status))
        if idx is not None:
            self.apply_move(task, STATUSES[idx])


def select_project(store: Store, projects: list[str], preferred: str | None = None) -> str | None:
    """Full-screen project picker; returns the chosen name or None if cancelled."""

    def _run(stdscr) -> str | None:
        curses.curs_set(0)
        stdscr.keypad(True)
        counts = store.projects()
        sel = projects.index(preferred) if preferred in projects else 0
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            _safe(stdscr, 0, 0, " Select a project", curses.A_BOLD)
            _safe(stdscr, 1, 0, " ↑↓ select · Enter open · q quit", curses.A_DIM)
            for i, name in enumerate(projects[: max(0, height - 5)]):
                total = counts.get(name, {}).get("total", 0)
                attr = curses.A_REVERSE if i == sel else 0
                _safe(stdscr, 3 + i, 2, f" {name} ({total} tasks) "[: width - 3], attr)
            stdscr.noutrefresh()
            curses.doupdate()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = min(len(projects) - 1, sel + 1)
            elif ch in (curses.KEY_ENTER, 10, 13):
                return projects[sel]
            elif ch in (ord("q"), ord("Q"), 27):
                return None

    return curses.wrapper(_run)


def run_board(store: Store, project: str) -> int:
    return curses.wrapper(Board(store, project).run)

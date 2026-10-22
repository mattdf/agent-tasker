"""Local web UI: a sibling to the TUI served by `agenttasker serve`.

Stdlib only (ThreadingHTTPServer + a single-page app). Binds 127.0.0.1 by
default; JSON API and attachment downloads live on the same server.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .core import (
    DONE,
    LIST_STATUS_ORDER,
    PRIORITY_LABELS,
    STATUSES,
    TASK_TYPES,
    DEFAULT_PRIORITY,
    DEFAULT_TYPE,
    Store,
    TaskError,
    claim_age_hours,
    display_ref,
    is_blocked,
    is_ready,
    next_status,
    prev_status,
    resolve_project,
    unfinished_dep_ids,
    utcnow,
)
from . import render

_INDEX = Path(__file__).parent / "web_static" / "index.html"
DEFAULT_PORT = 8988


def _task_payload(store: Store, task) -> dict:
    data = task.to_dict()
    dep_map = store.dep_statuses(task)
    data["blocked"] = is_blocked(task, dep_map)
    data["ready"] = is_ready(task, dep_map)
    data["blocked_by"] = unfinished_dep_ids(task, dep_map)
    age = claim_age_hours(task)
    data["claim_age_hours"] = round(age, 4) if age is not None else None
    data["attachments"] = [
        {k: a[k] for k in ("id", "filename", "size", "sha256", "created_at")}
        for a in store.list_attachments(task)
    ]
    data["events"] = store.events(task, 20)
    data["evidence_rows"] = store.evidence(task)
    data["last_activity"] = store.last_activity(task)
    data["active_hours"] = round(h, 2) if (h := store.active_hours(task)) is not None else None
    children = store.children(task)
    if children:
        data["children"] = [
            {k: c.to_dict()[k] for k in ("id", "name", "status", "priority", "type")}
            for c in children
        ]
        data["story_progress"] = f"{sum(1 for c in children if c.status == DONE)}/{len(children)}"
    return data


def build_handler(store_path: Path | None, default_project: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agenttasker"

        def log_message(self, fmt, *args):  # quiet; errors still surface
            pass

        # --- plumbing
        def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def _json(self, code: int, payload):
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, exc: Exception):
            self._json(400, {"error": str(exc)})

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length) or b"{}")

        def _store(self) -> Store:
            return Store(store_path)

        def _route(self):
            return urlparse(self.path)

        # --- verbs
        def do_GET(self):
            url = self._route()
            try:
                if url.path in ("/", "/index.html"):
                    self._send(200, _INDEX.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/config":
                    self._api_config()
                elif url.path == "/api/tasks":
                    self._api_tasks(parse_qs(url.query))
                elif url.path.startswith("/file/"):
                    self._api_file(url.path)
                else:
                    self._json(404, {"error": f"no route {url.path}"})
            except TaskError as exc:
                self._error(exc)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_POST(self):
            url = self._route()
            try:
                if url.path == "/api/tasks":
                    self._api_add(self._body())
                elif "/attachments" in url.path:
                    parts = url.path.strip("/").split("/")
                    # tasks/{project}/{id}/attachments
                    self._api_attach(parts[2], parts[3], self._body())
                else:
                    self._json(404, {"error": f"no route {url.path}"})
            except TaskError as exc:
                self._error(exc)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_PATCH(self):
            url = self._route()
            try:
                parts = url.path.strip("/").split("/")  # api/tasks/{project}/{id}
                if len(parts) == 4 and parts[:2] == ["api", "tasks"]:
                    self._api_update(parts[2], parts[3], self._body())
                else:
                    self._json(404, {"error": f"no route {url.path}"})
            except TaskError as exc:
                self._error(exc)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_DELETE(self):
            url = self._route()
            try:
                parts = url.path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "tasks"]:
                    self._api_delete(parts[2], parts[3])
                elif len(parts) == 6 and parts[:2] == ["api", "tasks"] and parts[4] == "attachments":
                    self._api_detach(parts[2], parts[3], parts[5])
                else:
                    self._json(404, {"error": f"no route {url.path}"})
            except TaskError as exc:
                self._error(exc)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        # --- api
        def _api_config(self):
            with self._store() as store:
                projects = [
                    {"name": name, "total": bucket["total"]}
                    for name, bucket in store.projects().items()
                ]
            self._json(200, {
                "default_project": default_project,
                "projects": projects,
                "statuses": list(STATUSES),
                "list_order": list(LIST_STATUS_ORDER),
                "types": list(TASK_TYPES),
                "priorities": list(PRIORITY_LABELS),
                "default_priority": DEFAULT_PRIORITY,
                "default_type": DEFAULT_TYPE,
                "now": utcnow(),
            })

        def _project(self, explicit: str | None) -> str:
            project = explicit or default_project
            if not project:
                raise TaskError("no project selected")
            return project

        def _api_tasks(self, qs):
            project = self._project((qs.get("project") or [None])[0])
            with self._store() as store:
                tasks = [_task_payload(store, t) for t in store.list_tasks(project)]
            self._json(200, {"project": project, "tasks": tasks})

        def _api_add(self, body: dict):
            project = self._project(body.get("project"))
            with self._store() as store:
                task = store.add_task(
                    project,
                    body.get("name", ""),
                    description=body.get("description", ""),
                    status=body.get("status", "backlog"),
                    evidence=body.get("evidence", ""),
                    priority=body.get("priority", DEFAULT_PRIORITY),
                    type=body.get("type", DEFAULT_TYPE),
                    tags=body.get("tags", []),
                    blockers=body.get("blockers", []),
                    depends_on=body.get("depends_on", []),
                    affects=body.get("affects", []),
                    parent=body.get("parent"),
                )
                self._json(200, _task_payload(store, task))

        def _api_update(self, project_name: str, ref: str, body: dict):
            project = unquote(project_name)
            with self._store() as store:
                task = store.get(project, ref)
                if "claim" in body:
                    claim = body["claim"] or {}
                    task = store.claim(task, claim.get("owner", ""), force=claim.get("force", False))
                elif "release" in body:
                    release = body["release"] or {}
                    task = store.release(task, owner=release.get("owner"), force=release.get("force", False))
                else:
                    if body.get("append_evidence"):
                        task = store.append_evidence(task, body["append_evidence"])
                    if body.get("edit_evidence") is not None:
                        edit = body["edit_evidence"] or {}
                        task = store.edit_evidence(task, int(edit.get("id", 0)),
                                                   edit.get("text", ""))
                    if body.get("delete_evidence") is not None:
                        task = store.delete_evidence(task, int(body["delete_evidence"]))
                    changes = {}
                    for key in ("name", "description", "evidence", "status", "priority", "type"):
                        if key in body and body[key] is not None:
                            changes[key] = body[key]
                    if "parent" in body and body["parent"] is not None:
                        changes["parent"] = body["parent"]
                    for key in ("blockers", "tags"):
                        if key in body and body[key] is not None:
                            changes[key] = body[key]
                    for key in ("depends_on", "affects"):
                        if key in body and body[key] is not None:
                            changes[key] = [
                                str(r) for r in body[key]
                            ]
                    if changes:
                        task = store.update(task, **changes)
                self._json(200, _task_payload(store, task))

        def _api_delete(self, project_name: str, ref: str):
            project = unquote(project_name)
            with self._store() as store:
                task = store.get(project, ref)
                store.delete(task, force=True)  # web confirm dialog already gated
            self._json(200, {"deleted": display_ref(project, task.id)})

        def _api_attach(self, project_name: str, ref: str, body: dict):
            project = unquote(project_name)
            data = base64.b64decode(body.get("content_b64", ""), validate=True)
            with self._store() as store:
                task = store.get(project, ref)
                att = store.add_attachment_bytes(task, body.get("filename", "attachment"), data)
                self._json(200, att)

        def _api_detach(self, project_name: str, ref: str, att_id: str):
            project = unquote(project_name)
            with self._store() as store:
                task = store.get(project, ref)
                name = store.remove_attachment(task, int(att_id))
            self._json(200, {"detached": name})

        def _api_file(self, path: str):
            # /file/{project}/{task_id}/{att_id}/{filename} — looked up by ids only
            parts = path.strip("/").split("/")
            if len(parts) < 5:
                raise TaskError("bad file route")
            project = unquote(parts[1])
            with self._store() as store:
                task = store.get(project, parts[2])
                data = store.read_attachment_bytes(task, int(parts[3]))
                atts = {a["id"]: a for a in store.list_attachments(task)}
                filename = atts.get(int(parts[3]), {}).get("filename", "attachment")
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            # render in-browser when possible (browsers fall back to a download
            # for types they can't display); sandbox script-capable types
            headers = {"Content-Disposition": f'inline; filename="{filename}"'}
            if content_type in ("text/html", "image/svg+xml", "application/xhtml+xml"):
                headers["Content-Security-Policy"] = "sandbox"
            self._send(200, data, content_type, headers)

    return Handler


def serve(project: str | None, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          db=None, open_browser: bool = True) -> None:
    handler = build_handler(db, project)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"agenttasker web ui -> {url}  (project: {project or 'pick in browser'})"
          f"  db: {(Path(db) if db else 'default')}")
    if open_browser:
        import threading
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()

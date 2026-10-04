"""cygnus.server.projects_routes - the Project Connector's endpoints
(docs/CONNECTOR.md): the projects Cygnus's coding agents work in, which one
is active, starting one, and what changed in it.

  GET  /projects                 every project and the active one
  POST /projects/connect         {path, name?, dev_cmd?, url?, agent?, active?}
  POST /projects/disconnect      {id}
  POST /projects/active          {id}            ("cygnus" = Cygnus's own folder)
  POST /projects/start           {id}            dev server + browser
  GET  /projects/{id}/changes    git state, recent commits, the jobs that ran there

Connecting and disconnecting are done from this computer only: they decide
which folders agents may work in. Writes are never allowed from here; that
yes comes from the owner when an agent first would change a file
(safety.assess_project_write)."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cygnus.agents import project
from cygnus.server import app as _app
from cygnus.server.app import _no_cache  # defined before this module is imported

router = APIRouter()


def _http_is_remote(request: Request) -> bool:
    # Defined further down server/app.py than where this router is included.
    return _app._http_is_remote(request)

DIFF_SHOWN = 40_000           # characters of diff the panel gets


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _fail(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status)


def _id(body: dict) -> str:
    return str(body.get("id") or body.get("name") or "").strip()


@router.get("/projects")
async def get_projects():
    items = await asyncio.to_thread(project.list_projects)
    active = next((p["id"] for p in items if p["active"]), project.CYGNUS_ID)
    return _no_cache(JSONResponse({"ok": True, "projects": items, "active": active}))


@router.post("/projects/connect")
async def post_projects_connect(request: Request):
    if _http_is_remote(request):
        return _fail("Connect a folder from this computer.", 403)
    body = await _body(request)
    dev_cmd = body.get("dev_cmd")
    try:
        info = await asyncio.to_thread(
            project.connect, str(body.get("path") or ""), name=str(body.get("name") or ""),
            dev_cmd=str(dev_cmd).strip() if dev_cmd is not None else None,
            url=str(body.get("url") or "").strip(), agent=str(body.get("agent") or "").strip(),
            active=bool(body.get("active", False)))
    except ValueError as e:
        return _fail(str(e))
    return JSONResponse({"ok": True, "project": info})


@router.post("/projects/disconnect")
async def post_projects_disconnect(request: Request):
    if _http_is_remote(request):
        return _fail("Disconnect a project from this computer.", 403)
    pid = _id(await _body(request))
    if not pid:
        return _fail("Say which project.")
    if pid == project.CYGNUS_ID:
        return _fail("Cygnus's own folder can't be disconnected.")
    try:
        gone = await asyncio.to_thread(project.disconnect, pid)
    except KeyError:
        return _fail(f"No project called {pid}.", 404)
    return JSONResponse({"ok": True, "id": gone})


@router.post("/projects/active")
async def post_projects_active(request: Request):
    pid = _id(await _body(request))
    if not pid:
        return _fail("Say which project.")
    try:
        info = await asyncio.to_thread(project.set_active, pid)
    except KeyError:
        return _fail(f"No project called {pid}.", 404)
    if not info["exists"]:
        return JSONResponse({"ok": True, "project": info,
                             "warning": f"{info['path']} is missing, so agents will use Cygnus's folder."})
    return JSONResponse({"ok": True, "project": info})


@router.post("/projects/start")
async def post_projects_start(request: Request):
    pid = _id(await _body(request))
    if not pid:
        return _fail("Say which project.")
    if project.find(pid) == project.CYGNUS_ID:
        return _fail("Cygnus is already running.")
    # Waits for the dev server to answer (up to a minute or so).
    result = await asyncio.to_thread(project.launch_project, pid)
    ok = not result.startswith("[Error]")
    return JSONResponse({"ok": ok, "result": result.removeprefix("[Error]").strip()},
                        status_code=200 if ok else 400)


@router.get("/projects/{pid}/changes")
async def get_project_changes(pid: str):
    """What changed in the project: branch, uncommitted files, recent
    commits, the Cygnus jobs that ran there, and a diff from where the
    newest job started (or of the uncommitted work)."""
    from cygnus.mcp import gitinfo, project_connector

    def _work():
        key, folder = project_connector.resolve(pid)
        out = project_connector.changes(key, folder, commits=8, jobs=8)
        out["diff"], out["diff_truncated"], out["diff_base"] = "", False, ""
        if out["git"]:
            since = next((j["project_head"] for j in out["jobs"] if j.get("project_head")), "")
            try:
                d = gitinfo.diff(folder, since=since, max_chars=DIFF_SHOWN)
            except ValueError:                     # the job's commit was rewritten away
                d = gitinfo.diff(folder, max_chars=DIFF_SHOWN)
            out["diff"], out["diff_truncated"], out["diff_base"] = d["diff"], d["truncated"], d["base"]
        return out

    try:
        out = await asyncio.to_thread(_work)
    except project_connector.ToolError as e:
        return _fail(str(e), 404)
    return _no_cache(JSONResponse(dict(out, ok=True)))

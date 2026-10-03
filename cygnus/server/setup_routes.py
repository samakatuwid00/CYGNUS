"""setup_routes.py - the first-run setup's endpoints.

This computer only, and only from Cygnus's own pages: app.py's middleware
refuses /setup to the phone and to anything proxied, and refuses a write that
is not same-origin JSON - these handle the student's model key.

  GET  /setup          the page (web/setup.html)
  GET  /setup/state    the provider chosen, what stops it working, the machine
  POST /setup/test     one real call with a key, before it is kept
  POST /setup/save     keep the provider and key; takes effect on restart
  POST /setup/restart  start again with it: the desktop app restarts the
                       backend; run by hand, the student is told to
"""

import asyncio
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from cygnus import config, paths
from cygnus.setup import machine, provider

router = APIRouter()

# The exit code that tells the desktop app "start me again, on purpose":
# unlike a crash, it does not count against the restart budget
# (desktop/main.js, RESTART_REQUESTED).
RESTART_REQUESTED = 75


@router.get("/setup")
async def setup_page():
    from cygnus.server.app import _no_cache
    html = (paths.WEB_DIR / "setup.html").read_text(encoding="utf-8")
    return _no_cache(HTMLResponse(content=html))


def _choice(body: dict) -> tuple[str, str, str, str]:
    return (str(body.get("provider") or "").strip().lower(), str(body.get("key") or ""),
            str(body.get("model") or "").strip(), str(body.get("base_url") or "").strip())


@router.get("/setup/state")
async def setup_state():
    specs = await asyncio.to_thread(machine.read)
    rec = machine.recommend(specs)
    return {"provider": config.CYGNUS_PROVIDER or None, "problem": config.PROVIDER_PROBLEM,
            "providers": list(config.PROVIDERS), "machine": specs, "recommend": rec,
            "downloads": await asyncio.to_thread(machine.downloads, rec["whisper"])}


@router.post("/setup/test")
async def setup_test(request: Request):
    name, key, model, base_url = _choice(await request.json())
    return await asyncio.to_thread(provider.test, name, key, model, base_url)


@router.post("/setup/save")
async def setup_save(request: Request):
    body = await request.json()
    name, key, model, base_url = _choice(body)
    if name not in config.PROVIDERS:
        return JSONResponse({"ok": False, "message": f"Unknown provider: {name}."}, status_code=400)
    whisper = str(body.get("whisper") or "").strip()
    return await asyncio.to_thread(provider.save, name, key, model, base_url, whisper)


@router.post("/setup/restart")
async def setup_restart():
    """Only the desktop app can bring the backend back after it exits;
    anyone else would be left with no Cygnus at all."""
    if os.getenv("CYGNUS_DESKTOP") != "1":
        return {"restart": "manual", "message": "Close Cygnus and start it again to use the new settings."}
    asyncio.get_running_loop().call_later(0.5, os._exit, RESTART_REQUESTED)
    return {"restart": "auto", "message": "Restarting Cygnus with the new settings..."}

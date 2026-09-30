"""setup_routes.py - the first-run setup's endpoints.

This computer only, and only from Cygnus's own pages: app.py's middleware
refuses /setup to the phone and to anything proxied, and refuses a write that
is not same-origin JSON - these handle the student's model key.

  GET  /setup/state    the provider chosen, what stops it working, the machine
  POST /setup/test     one real call with a key, before it is kept
  POST /setup/save     keep the provider and key; takes effect on restart
"""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cygnus import config
from cygnus.setup import machine, provider

router = APIRouter()


def _choice(body: dict) -> tuple[str, str, str, str]:
    return (str(body.get("provider") or "").strip().lower(), str(body.get("key") or ""),
            str(body.get("model") or "").strip(), str(body.get("base_url") or "").strip())


@router.get("/setup/state")
async def setup_state():
    specs = await asyncio.to_thread(machine.read)
    return {"provider": config.CYGNUS_PROVIDER or None, "problem": config.PROVIDER_PROBLEM,
            "providers": list(config.PROVIDERS), "machine": specs, "recommend": machine.recommend(specs)}


@router.post("/setup/test")
async def setup_test(request: Request):
    name, key, model, base_url = _choice(await request.json())
    return await asyncio.to_thread(provider.test, name, key, model, base_url)


@router.post("/setup/save")
async def setup_save(request: Request):
    name, key, model, base_url = _choice(await request.json())
    if name not in config.PROVIDERS:
        return JSONResponse({"ok": False, "message": f"Unknown provider: {name}."}, status_code=400)
    return await asyncio.to_thread(provider.save, name, key, model, base_url)

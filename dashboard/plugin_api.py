"""agy-orchestrator dashboard/desktop API — GET /state, POST /reauth, and the
model-picker endpoints GET/POST /routes + POST /models/refresh (ADR-002).

Mounted by the gateway at /api/plugins/agy-orchestrator/ . /state reads the
snapshot file the meter thread keeps fresh; /reauth triggers a zero-quota agy
command (its startup performs silent auth when the cached refresh token is
valid), re-reads the keychain, and returns fresh auth state. The picker
endpoints are module-level and stateless (settings.json / models.json on disk)
so they work regardless of which process instance serves them; the running
meter picks external writes up via its settings-mtime check.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_STATE = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) \
    / "cache" / "agy-orchestrator" / "state.json"


def _meter_mod():
    spec = importlib.util.spec_from_file_location(
        "agy_meter_routes", _PLUGIN_DIR / "meter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@router.get("/state")
def state():
    try:
        return JSONResponse(json.loads(_STATE.read_text()))
    except Exception as e:  # no state yet, or unreadable
        return JSONResponse({"error": f"no state yet: {e}"}, status_code=503)


@router.get("/routes")
def get_routes():
    """Effective route->models config + discovered model catalog, for the picker."""
    mod = _meter_mod()
    return JSONResponse({
        "routes": mod.get_routes_from(mod._load_settings()),
        "defaults": mod.ROUTE_DEFAULTS,
        "models": mod.read_models(),
    })


@router.post("/routes")
async def save_routes(request: Request):
    """Persist the owner's selection: {"routes": {"plan": [slug, ...],
    "parallel": [slug, ...]}}. Only known catalog slugs are accepted."""
    mod = _meter_mod()
    try:
        body = await request.json()
        routes = (body or {}).get("routes")
        saved = mod.save_routes(routes)
        return JSONResponse({"ok": True, "routes": saved})
    except ValueError as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@router.post("/models/refresh")
def refresh_models():
    """Re-run `agy models` discovery now (new releases appear in the picker)."""
    mod = _meter_mod()
    cat = mod.discover_models()
    return JSONResponse({"ok": not cat.get("error"), "models": cat},
                        status_code=200 if not cat.get("error") else 503)


@router.post("/reauth")
def reauth():
    try:
        spec = importlib.util.spec_from_file_location(
            "agy_meter_reauth", _PLUGIN_DIR / "meter.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        auth = mod.get_meter().attempt_silent_reauth()
        ok = bool(auth.get("ok")) and not auth.get("needs_reauth")
        return JSONResponse({"ok": ok, "auth": auth,
                             "hint": None if ok
                             else "silent auth failed — run `agy` interactively "
                                  "to complete the browser login"})
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)

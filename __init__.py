"""agy-meter — Antigravity lane meter + router plugin.

Registers:
  * observer hooks -> GLM-side t/s + error telemetry into the meter
  * llm_execution middleware -> 429 fallback (glm-5.3-flash pinned calls retry
    once on glm-5.3; the parent loop is never touched)
  * tools: agy_route (system-one lane classifier), agy_status (full state)
"""
from __future__ import annotations

import json
import time

from . import classifier
from .meter import get_meter


def _unwrap(args, kwargs) -> dict:
    if isinstance(kwargs, dict) and kwargs:
        return kwargs
    if args and isinstance(args[0], dict):
        return args[0]
    return {}


def register(ctx) -> None:  # noqa: ANN001 — PluginContext from host
    meter = get_meter()
    meter.start()

    # ---- observer hooks: GLM-side telemetry ----
    def on_post_api(**kw):
        try:
            meter.record_llm(kw.get("provider"), kw.get("model"),
                             kw.get("usage") or {}, kw.get("api_duration") or 0)
        except Exception:
            pass

    def on_api_error(**kw):
        try:
            status = kw.get("status_code")
            if status is None:
                err = kw.get("error") or {}
                status = err.get("type") or err.get("status") or err.get("code")
                reason = kw.get("reason")
                if status is None and reason:
                    status = str(reason)[:60]
            meter.record_error(kw.get("provider"), kw.get("model"), status)
        except Exception:
            pass

    ctx.register_hook("post_api_request", on_post_api)
    ctx.register_hook("api_request_error", on_api_error)

    # ---- middleware: one-retry fallback for the pinned flash model ----
    # Delegate children and aux tasks are pinned to glm-5.3-flash and have no
    # fallback chain of their own (known gap). NARROWED 2026-11-15 to an exact
    # model match: the old endswith("flash") suffix match would have caught any
    # future model named *-flash and silently rewritten it. Full removal is
    # pending: delegation.fallback_providers now covers delegate children, but
    # aux tasks (vision/compression/titling) still have no core fallback chain.
    _PINNED_FLASH = "glm-5.3-flash"
    _flash_cooldown_until = 0.0
    _rerouted_requests: set[str] = set()

    def _should_reroute_flash(model: str, req_id: str) -> bool:
        if model != _PINNED_FLASH:
            return False
        if time.monotonic() < _flash_cooldown_until:
            return True
        return bool(req_id and req_id in _rerouted_requests)

    def llm_request_guard(**kw):
        request = kw.get("request") or {}
        model = str(request.get("model") or "")
        req_id = str(kw.get("api_request_id") or "")
        if _should_reroute_flash(model, req_id):
            alt = dict(request)
            alt["model"] = "glm-5.3"
            return alt
        return request

    def llm_guard(**kw):
        nonlocal _flash_cooldown_until
        request = kw.get("request") or {}
        next_call = kw.get("next_call")
        model = str(request.get("model") or "")
        req_id = str(kw.get("api_request_id") or "")
        if model != _PINNED_FLASH:
            return next_call(request)
        if _should_reroute_flash(model, req_id):
            alt = dict(request)
            alt["model"] = "glm-5.3"
            return next_call(alt)
        try:
            return next_call(request)
        except Exception as exc:
            msg = str(exc).lower()
            if "429" in msg or "rate limit" in msg or "rate_limit" in msg or "quota" in msg:
                _flash_cooldown_until = time.monotonic() + 60.0
                if req_id:
                    if len(_rerouted_requests) >= 256:
                        _rerouted_requests.clear()
                    _rerouted_requests.add(req_id)
                meter.record_error("agy-meter-mw", model, "429->glm-5.3")
            raise

    ctx.register_middleware("llm_request", llm_request_guard)
    ctx.register_middleware("llm_execution", llm_guard)

    # ---- tools ----
    route_schema = {
        "type": "object",
        "properties": {
            "chunks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Self-contained task chunk descriptions to route.",
            },
            "route": {
                "type": "string",
                "enum": ["plan", "parallel", "delegate"],
                "description": "Optional batch intent: pin ALL chunks to this "
                               "route, skipping per-chunk classification "
                               "(plan -> thinking brain, parallel -> fan-out "
                               "work, delegate -> context-bound Hermes children).",
            },
        },
        "required": ["chunks"],
    }

    def agy_route_handler(*args, **kwargs):
        params = _unwrap(args, kwargs)
        chunks = params.get("chunks") or []
        pin = params.get("route")
        if not chunks:
            return {"error": "chunks[] required"}
        pin = {"delegate": "hermes_delegate"}.get(pin, pin)
        routes_cfg = meter.get_routes()  # owner's selection (settings.json, ADR-002)
        out = []
        for chunk in chunks[:20]:
            if pin:
                res = {"route": pin, "via": "pinned",
                       "probabilities": None, "confidence": None}
            else:
                # Heuristic by default; the Kev/LM-Studio chain only when
                # settings.json sets {"classifier": "kev"} (2026-11-15).
                res = classifier.classify(chunk, meter.classifier_settings())
            route = res.get("route")
            if route not in classifier.ROUTES:  # fail-safe: unknown -> parallel
                route = "parallel"
                res["route"] = route
            conf = res.get("confidence")
            if isinstance(conf, (int, float)) and conf < 0.35:
                res["low_confidence"] = True  # ambiguous chunk: apply parent judgment
            # Model resolution: ONLY the owner's selected models, in order;
            # pressure walks the selection, never beyond it (ADR-002).
            res.update(meter.resolve_model(route, routes_cfg))
            if route == "hermes_delegate" and meter.zai_pressure().get("hot"):
                res["zai_pressure"] = True
                res["note"] = ("zai hot: stagger/defer this child or rewrite "
                               "self-contained -> agy")
            meter.note_classifier(res.get("via"))
            out.append({"chunk": chunk[:160], **res})
        reply = {"routes": out,
                 "route_models": routes_cfg,
                 "zai_pressure": meter.zai_pressure()}
        if len(chunks) > 20:
            reply["note"] = (f"{len(chunks)} chunks given; only the first 20 "
                             "were routed")
        return json.dumps(reply)

    status_schema = {
        "type": "object",
        "properties": {},
    }

    def agy_status_handler(*args, **kwargs):
        return json.dumps(meter.snapshot())

    ctx.register_tool(name="agy_route", toolset="agy", schema=route_schema,
                      handler=agy_route_handler,
                      description="Route task chunks into agy routes (plan / "
                                  "parallel / hermes_delegate). Heuristic "
                                  "classifier by default (Kev-4B chain opt-in "
                                  "via settings). Models resolve from the "
                                  "owner's picker selection — only selected "
                                  "models are ever used; a hot pool falls to "
                                  "the next selection. Optional 'route' pins "
                                  "the whole batch (plan|parallel|delegate).")
    ctx.register_tool(name="agy_status", toolset="agy", schema=status_schema,
                      handler=agy_status_handler,
                      description="Current agy state: auth expiry, quota "
                                  "snapshot, pool burn (5h window), per-slug "
                                  "model usage, dispatch log, GLM t/s "
                                  "telemetry, recent errors.")

    # ---- agy_dispatch: meter-owned dispatch (ADR-003, 2026-11-15) ----
    dispatch_schema = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "Fully self-contained task prompt (agy "
                                      "has no session context)."},
            "model": {"type": "string",
                      "description": "Optional explicit model slug (from the "
                                     "pane picker catalog)."},
            "route": {"type": "string", "enum": ["plan", "parallel"],
                      "description": "Resolve the model from this route's "
                                     "owner selection (default: parallel). "
                                     "Ignored when 'model' is set."},
            "name": {"type": "string",
                     "description": "Short task name for the run file "
                                    "(default 'run')."},
            "timeout_min": {"type": "integer", "minimum": 1, "maximum": 120,
                            "description": "agy --print-timeout in minutes "
                                           "(default 15)."},
            "workdir": {"type": "string",
                        "description": "Optional absolute working directory "
                                       "for the run."},
            "readonly": {"type": "boolean",
                         "description": "Fingerprint workdir's git tree before "
                                        "dispatch; call agy_guard(phase=after) "
                                        "at harvest to verify nothing changed."},
        },
        "required": ["prompt"],
    }

    def agy_dispatch_handler(*args, **kwargs):
        params = _unwrap(args, kwargs)
        model = params.get("model")
        resolution = None
        try:
            if not model:
                resolution = meter.resolve_model(params.get("route") or "parallel")
                model = resolution.get("model")
                if not model:
                    return json.dumps({"error": resolution.get("pool_warning")
                                       or "no model resolved"})
            out = meter.dispatch(
                prompt=params.get("prompt"), model=model,
                name=params.get("name") or "run",
                timeout_min=int(params.get("timeout_min") or 15),
                workdir=params.get("workdir"),
                readonly=bool(params.get("readonly")))
            if resolution:
                out["model_resolution"] = resolution
            return json.dumps(out)
        except (ValueError, TypeError) as e:
            return json.dumps({"error": str(e)[:200]})

    guard_schema = {
        "type": "object",
        "properties": {
            "workdir": {"type": "string",
                        "description": "Absolute path of the git working tree "
                                       "to fingerprint."},
            "phase": {"type": "string", "enum": ["before", "after"],
                      "description": "before = record baseline; after = "
                                     "compare (CLEAN / DRIFT / NOT_A_REPO)."},
            "key": {"type": "string",
                    "description": "Baseline key (defaults to workdir; "
                                   "readonly dispatches use 'dispatch:<run_id>')."},
        },
        "required": ["workdir", "phase"],
    }

    def agy_guard_handler(*args, **kwargs):
        params = _unwrap(args, kwargs)
        workdir = params.get("workdir")
        if not workdir:
            return json.dumps({"error": "workdir required"})
        fn = (meter.guard_after if params.get("phase") == "after"
              else meter.guard_before)
        return json.dumps(fn(str(workdir), key=params.get("key") or None))

    ctx.register_tool(name="agy_dispatch", toolset="agy", schema=dispatch_schema,
                      handler=agy_dispatch_handler,
                      description="Spawn a detached, env-scrubbed, structurally-"
                                  "metered agy run: model from the owner's "
                                  "picker (or explicit slug), output written to "
                                  "runs/<slug>__<name>.json by the meter itself "
                                  "(no hand-naming), credentials stripped from "
                                  "the child env, list argv (no shell-quoting "
                                  "pitfalls). Returns run_id + paths "
                                  "immediately; harvest via read_file. "
                                  "readonly=true wraps a git-fingerprint guard.")
    ctx.register_tool(name="agy_guard", toolset="agy", schema=guard_schema,
                      handler=agy_guard_handler,
                      description="Git-fingerprint read-only guard: phase=before "
                                  "records a tree baseline (HEAD + porcelain + "
                                  "untracked size/mtime); phase=after compares "
                                  "against it — CLEAN, DRIFT (with changed "
                                  "files), NOT_A_REPO, or NO_BASELINE.")

"""Route classifier for agy dispatches — keyword heuristic by default.

Owner ruling 2026-11-15: the heuristic IS the classifier for the 3-way
plan/parallel/hermes_delegate decision (instant, zero network, zero deps).
The probabilistic chain (Ollama system-one Kev-4B -> LM Studio structured
completion) is OPT-IN via settings.json {"classifier": "kev"}; it exists for
the day routes multiply again and calibrated probabilities earn their keep.

Routes follow the owner ruling of 2026-11-14 (model-selectable routing,
ADR-002). The classifier picks ONLY the route (task archetype); the model
resolves from the owner's ordered selection in settings.json ("routes" key),
edited from the desktop pane picker:
  hermes_delegate  delegate_task / glm-5.3-flash (zai)  — fixed, never rerouted
  plan             owner-selected models               — thinking brain
  parallel         owner-selected models               — parallel fan-out work
Only selected models are ever used; a hot primary falls to the next
SELECTION (never to an unselected substitute).
"""
from __future__ import annotations

import json
import urllib.request

ROUTES = {
    "hermes_delegate": {
        "slug": "delegate_task/glm-5.3-flash",
        "pool": "zai",
        "desc": "The task references files, repos, diffs, sessions, or prior conversation it does NOT fully include in the text; it must run where those are reachable (Hermes delegate child with tools).",
    },
    "plan": {
        "desc": "Thinking brain: deep planning and analysis — produce a detailed, step-by-step implementation plan or dispatch-ready instruction set (chunks, specs, acceptance criteria) from a problem statement, including planning and reasoning over complex code changes. No code execution.",
    },
    "parallel": {
        "desc": "Execution work for parallel fan-out: precise coding, edits, refactors, tests, plus mechanical bulk — boilerplate, scaffolding, batch text transforms, translations, doc drafts, summaries.",
    },
}
_ROUTE_KEYS = list(ROUTES)
_CRITERIA = [ROUTES[k]["desc"] for k in _ROUTE_KEYS]

OLLAMA_URL = "http://localhost:11434/v1/systemone"
LMSTUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
# Kev-4B (ggml-org, Qwen3.5-4B base, Apache-2.0) carries the llama.cpp decision
# metadata Ollama's /v1/systemone requires. Plumb-4B's Ollama registry build
# lacks it ("not a llama.cpp decision model" on 0.40.x) — available via settings.
DECISION_MODEL = "hf.co/ggml-org/Kev-4B-GGUF:Q8_0"
PLUMB_MODEL = "crh225/plumb-4b"
LMSTUDIO_MODEL = "google/gemma-3-e4b"


def _post(url: str, payload: dict, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(100_000))


def classify(chunk: str, settings: dict | None = None) -> dict:
    settings = settings or {}
    # Heuristic-first (owner ruling 2026-11-15): the 3-way plan/parallel/
    # delegate decision is keyword-decidable and batch pinning often bypasses
    # classification anyway. The Kev-4B/LM-Studio chain is OPT-IN via
    # settings {"classifier": "kev"} when calibrated probabilities are wanted.
    if settings.get("classifier") != "kev":
        return {"route": _heuristic(chunk), "probabilities": None,
                "confidence": None, "via": "heuristic", "errors": []}
    errors: list[str] = []
    try:
        return _via_ollama(chunk, settings)
    except Exception as e:
        errors.append(f"ollama: {str(e)[:90]}")
    try:
        return _via_lmstudio(chunk, settings)
    except Exception as e:
        errors.append(f"lmstudio: {str(e)[:90]}")
    route = _heuristic(chunk)
    return {"route": route, "probabilities": None, "confidence": None,
            "via": "heuristic", "errors": errors}


def _route_from_choice(choice) -> str:
    """Accept a route key, an index into _ROUTE_KEYS, or a criterion description."""
    s = str(choice)
    if s in ROUTES:
        return s
    if s.isdigit() and int(s) < len(_ROUTE_KEYS):
        return _ROUTE_KEYS[int(s)]
    for key, desc in zip(_ROUTE_KEYS, _CRITERIA):
        if s.startswith(desc[:40]):
            return key
    raise ValueError(f"unknown route choice: {s[:80]}")


def _via_ollama(chunk: str, settings: dict) -> dict:
    model = settings.get("decision_model", DECISION_MODEL)
    criteria = {k: ROUTES[k]["desc"] for k in _ROUTE_KEYS}  # dict keys -> clean route names
    res = _post(OLLAMA_URL, {
        "model": model,
        "state": chunk[:4000],
        "questions": {"route": {"type": "choice",
                                "instructions": "Pick the execution route for this task chunk.",
                                "criteria": criteria}},
    })
    ans_wrapper = res.get("answers", res) if isinstance(res, dict) else {}
    ans = ans_wrapper.get("route") if isinstance(ans_wrapper, dict) else None
    ans = ans if isinstance(ans, dict) else {}
    probs = ans.get("probabilities")
    choice = ans.get("choice")
    if choice is None and isinstance(probs, dict) and probs:
        choice = max(probs, key=lambda k: probs[k])
    if choice is None:
        raise ValueError(f"unparseable systemone reply: {str(res)[:120]}")
    route = _route_from_choice(choice)
    return {"route": route,
            "probabilities": probs, "confidence": ans.get("confidence"),
            "via": f"ollama:{model}"}


def _lmstudio_pick_model(settings: dict, configured: str) -> str:
    """If the configured id isn't loaded, fall back to the first gemma model."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:1234/v1/models", timeout=5) as r:
            data = json.loads(r.read(50_000))
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if configured in ids:
            return configured
        for i in ids:
            if "gemma" in i.lower():
                return i
        if ids:
            return ids[0]
    except Exception:
        pass
    return configured


def _via_lmstudio(chunk: str, settings: dict) -> dict:
    model = settings.get("lmstudio_model", LMSTUDIO_MODEL)
    schema = {"type": "object",
              "properties": {"route": {"type": "string", "enum": _ROUTE_KEYS}},
              "required": ["route"], "additionalProperties": False}
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "You are a task router. Pick exactly one route.\n"
                        + "\n".join(f"{k}: {ROUTES[k]['desc']}" for k in _ROUTE_KEYS)
                        + '\nAnswer with JSON {"route": "..."} and nothing else.'},
            {"role": "user", "content": chunk[:4000]},
        ],
        "temperature": 0,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "route", "schema": schema}},
    }
    try:
        res = _post(LMSTUDIO_URL, payload, timeout=15)
    except urllib.error.HTTPError:
        # model id not loaded? auto-pick and retry once
        payload["model"] = _lmstudio_pick_model(settings, model)
        if payload["model"] == model:
            raise
        res = _post(LMSTUDIO_URL, payload, timeout=15)
    content = res["choices"][0]["message"]["content"]
    route = json.loads(content).get("route")
    if route not in ROUTES:
        raise ValueError(f"model returned bad route: {str(route)[:60]}")
    return {"route": route, "probabilities": None, "confidence": None,
            "via": f"lmstudio:{payload['model']}"}


def _heuristic(chunk: str) -> str:
    c = chunk.lower()
    context_bound = any(w in c for w in ("we discussed", "the bug", "review the diff",
                                         "the error", "our repo", "from earlier",
                                         "the conversation", "uncommitted", "this session"))
    if context_bound:
        return "hermes_delegate"
    complex_ = any(w in c for w in ("analyz", "compare", "evaluate", "decide",
                                    "strategy", "trade-off", "architect",
                                    "plan", "design"))
    if complex_:
        return "plan"
    return "parallel"

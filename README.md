# agy-meter

Hermes desktop plugin for monitoring Antigravity (AGY) token allocations, quotas, auth lifecycle, and handling model routing with fallback.

## What it does

`agy-meter` monitors Gemini and third-party token usage in real time. It renders a statusbar chip with an expandable telemetry pane, provides agent tools for task routing and status inspection, and injects middleware to automatically recover from rate limits.

- **Statusbar Chip & Pane**: Glanceable token metrics and re-auth timer; pane shows pools, auth status, quotas, and telemetry.
- **Agent Tools**: Inspect system state and classify tasks into execution lanes.
- **Middleware**: Transparent failover retry for rate-limited model calls.

## Chip readout

The statusbar displays live metrics using the format:

```
AGY garg(%) gem(%) 3rd(%) h(HH:mm) w(HH:mm) auth(mm)
```

- `garg(%)`: Argon pool usage, as a percentage.
- `gem(%)`: Gemini pool usage, as a percentage.
- `3rd(%)`: Third-party pool usage, as a percentage.
- `h(HH:mm)`: 5-hour quota window.
- `w(HH:mm)`: Weekly quota window.
- `auth(mm)`: Minutes remaining until re-authentication is required.

The chip is clickable. Clicking it triggers a silent re-auth.

## Tools

- `agy_route`: Classifies task chunks into routes (`plan` / `parallel` / `hermes_delegate`) — heuristic by default, Kev-4B chain opt-in via settings. Optional `route` param pins a whole batch.
- `agy_status`: Full plugin state (pools, auth, quota, per-slug usage, dispatch log, telemetry).
- `agy_dispatch`: **Meter-owned dispatch** (ADR-003) — detached, env-scrubbed, structurally-metered agy run; model from the owner's picker; returns run_id + paths immediately.
- `agy_guard`: Git-fingerprint read-only guard (`before` records a baseline, `after` compares — CLEAN / DRIFT / NOT_A_REPO).

## Dispatch & hygiene (ADR-003)

- **Structural attribution**: `agy_dispatch` writes `runs/<slug>__<run_id>.json` itself — no hand-naming convention to remember.
- **Env scrub**: agy is a closed binary; credential-looking env vars never reach it (keep-list + `env_passthrough` in settings.json).
- **Read-only guard**: `agy_dispatch(readonly=true, workdir=…)` fingerprints the git tree before dispatch; `agy_guard(phase=after, key=…)` at harvest proves the tree never changed (DRIFT lists what moved).
- **Classifier**: heuristic by default (instant); set `{"classifier": "kev"}` in settings.json to re-enable the calibrated chain.

## Routes & model picker

The classifier picks the **route** (task archetype); the **model** comes from
the owner's ordered selection, edited in the pane's model picker. Only
selected models are ever used.

| Route | Purpose | Owner selection (primary → fallback) |
|---|---|---|
| `plan` | thinking brain: plans, deep analysis | `claude-opus-5-5-high` → `gemini-4-hydrogen-high` |
| `parallel` | fan-out work: code + bulk | `claude-sonnet-5-5-high` → `gemini-3.8-flash-high` |
| `hermes_delegate` | context-bound chunks | fixed: `delegate_task` / `glm-5.3-flash` (zai) |

The picker lists every model `agy models` exposes (grouped by base model with
effort variants and pool tags) and refreshes automatically every 6h — new
releases appear without code changes. Selections are validated against the
discovered catalog on save.

## Pressure-aware routing

- **Trigger**: A selected model's pool is ≥80% used (estimate), or returned an HTTP 429 in the last 30 minutes.
- **Resolution**: walk the route's ordered selection to the first model on a cool pool (`routed_from`/`reason` reported). Never beyond the selection — if everything is hot, the primary is returned with `pool_warning`.
- **`hermes_delegate` exception**: this route never reroutes. It gets flagged instead (`zai_pressure=true`: stagger `delegate_task` fan-outs, move self-contained chunks to agy).

## Middleware

- `llm_execution`: Intercepts model calls and retries `glm-5.3-flash` HTTP 429 errors once on `glm-5.3`.

## State file path

Plugin state persists locally at:

```
~/.hermes/cache/agy-meter/state.json

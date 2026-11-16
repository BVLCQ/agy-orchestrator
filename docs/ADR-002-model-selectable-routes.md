# ADR 002: Model-Selectable Routes with a Discovery-Driven Picker

## Status

**Accepted**: 2026-11-14

---

## Context

ADR-001 fused task type and model: five lanes, each hardcoded to one slug
(`opus_complex` → `claude-opus-5-5-high`, `argon_high` → `gemini-4-argon-high`,
…). Two problems emerged:

1. **The catalog moves.** Within ten days of ADR-001, `gemini-4-hydrogen-*`
   (including an `xhigh` effort tier Argon lacks) and
   `gemini-pro-orchestrator-g1-pro` appeared. Every release required a code
   edit to re-map lanes. The owner ruled Argon → Hydrogen for planning — a
   model swap that should be a selection, not a patch.
2. **The owner wants the choice.** The routing model each route uses is a
   product decision (which subscription bucket to spend, which model to trust
   with planning), and it must change without asking the agent to edit code.

Constraints carried over from ADR-001: no quota API (pressure is estimated or
429-learned), `agy` runs cold with no session context, zero manual
reconciliation steps.

---

## Decision

### 1. Routes are task archetypes; models are the owner's selection

Three routes replace the five lanes:

| Route | Purpose | Model source |
|---|---|---|
| `plan` | thinking brain: plans, deep analysis | owner selection |
| `parallel` | fan-out work: precise code + mechanical bulk | owner selection |
| `hermes_delegate` | context-bound chunks | fixed: `delegate_task` / glm-5.3-flash (zai) |

The classifier (Kev-4B → LM Studio → heuristic) picks ONLY the route. The
model resolves from an ordered per-route list saved in
`settings.json` (`routes` key), seeded with the owner ruling of 2026-11-14:

- `plan`: `claude-opus-5-5-high` → `gemini-4-hydrogen-high`
- `parallel`: `claude-sonnet-5-5-high` → `gemini-3.8-flash-high`

### 2. Only selected models are ever routable

The old cross-lane `FALLBACKS` graph is removed — it could substitute an
unselected model, which is exactly what the owner forbade. Pressure
resolution walks the route's ordered selection: a hot primary falls to the
next SELECTION (`routed_from`/`reason` reported). If every selection's pool
is hot, the primary is returned with `pool_warning`; the owner widens the
selection or waits. `hermes_delegate` remains never-rerouted (ADR-001).

### 3. The picker lists what `agy models` actually offers

A catalog job parses `agy models` output (slug ⇉ label TSV) into base models
with ordered effort variants and pool tags, persisted to `models.json`:
auto-refresh every 6h in the meter tick, on-demand via `POST
/api/plugins/agy-meter/models/refresh`. New releases (Opus 5.6, a new Gemini)
appear in the picker without code changes.

The desktop pane renders the picker: per route, ordered model slots (primary
→ fallback) as grouped dropdowns, save + catalog-refresh actions, a fixed
read-only `hermes_delegate` row. `POST /routes` validates slugs against the
catalog before persisting (merged with existing settings; budget calibration
keys untouched). The running meter picks external writes up via a
settings-file mtime check, so saves apply without a gateway restart.

### 4. Batch intent

`agy_route` accepts an optional `route` (`plan` | `parallel` | `delegate`)
that pins every chunk in the call to that route, skipping per-chunk
classification — matching how batches are actually dispatched
(plan-then-dispatch, parallel fan-out). Unpinned batches classify per chunk
as before. The delegate-vs-agy axis stays advisory below 0.35 confidence
(ADR-001).

---

## Alternatives Considered

### A. Keep model-locked lanes; add per-lane model overrides in code
Rejected: every catalog change or preference change is a code edit and a
gateway restart — precisely the loop the owner wants out of.

### B. Per-dispatch model parameter on `agy_route` (agent picks each call)
Rejected for v0.2: standing config matches the owner's ruling ("set once,
every dispatch follows"); the classifier also needs a stable default. The
ordered-list schema leaves room to add a per-call override later without a
migration.

### C. Auto-select the best model per chunk (classifier chooses model too)
Rejected: the classifier cannot see quota state or owner preference, and the
owner explicitly wants the selection to be theirs. Text heuristics already
failed on the context axis (ADR-001, decision 3).

---

## Consequences

### Positive
- Model swaps (Argon → Hydrogen) and new releases are picker operations.
- Routing can never spend a bucket the owner didn't opt into.
- Pool spread is visible at selection time (each option shows its pool tag);
  both seeded routes span two buckets, preserving the split-burst property.

### Negative / Accepted costs
- The plan route lost its dedicated-bucket anchor (Argon) by owner choice;
  Hydrogen draws from the shared gemini pool. If hydrogen turns out to have
  a dedicated bucket, `_pool_for` needs one prefix rule.
- Unknown-to-catalog slugs are rejected on save, so a selection referencing
  a not-yet-discovered model needs a catalog refresh first (deliberate: typos
  must not silently strand a route). With no catalog on disk, saves are
  allowed (discovery must never block the owner).
- Kev-4B was calibrated on the old five-lane criteria; the three-route
  criteria are re-seeded and may need description tuning if routing quality
  regresses.

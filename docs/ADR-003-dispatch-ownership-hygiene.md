# ADR 003: Meter-Owned Dispatch, Hygiene, and the Heuristic-Default Classifier

## Status

**Accepted**: 2026-11-15

---

## Context

The adversarial critique of 2026-11-15 (informed by the community ecosystem
review: soyelmismo/hermes-antigravity-subscription, neerazz/hermes-antigravity-oauth,
josefacero/hermes-gogogoogle) found four structural weaknesses in v0.2.0:

1. **Metering was a filename convention.** Pool attribution depended on run
   files being hand-named `<slug>__<task>.json`; a misnamed file mis-attributes
   burn, hand-run commands are invisible, and the skill had to mandate the
   convention in prose.
2. **Dispatch hygiene was prose.** Credential-looking env vars reached the
   closed-source `agy` binary (pattern fix: soyelmismo's env scrub), and the
   read-only-worker contract was enforced by the parent remembering to run
   `git status` twice (pattern fix: gogogoogle's git fingerprint).
3. **A real state-clobber bug**: dashboard `/reauth` built a throwaway `Meter`
   whose `attempt_silent_reauth()` called `snapshot()`, overwriting the live
   meter's state.json with near-empty pools.
4. **Over-engineered classifier**: the four-tier chain (Ollama Kev-4B → LM
   Studio → ctx.llm → heuristic) decided a 3-way question the heuristic mostly
   nails, batch pinning often bypasses, and whose one calibrated skill (the
   context-axis abstain) fired exactly where the parent overrides it anyway.

---

## Decision

### 1. The meter owns dispatch (`agy_dispatch`)

A new tool spawns agy runs: list argv (no shell-quoting pitfalls), detached
(`start_new_session`), output written BY THE METER to
`runs/<slug>__<run_id>.json` — attribution becomes structural, not lexical.
Model resolution rides the owner's picker selection (explicit slug or
`route:` plan/parallel), preserving ADR-002's selection-only invariant.
Returns run_id + paths immediately; the 45s ingest tick meters the result; a
reaper thread reaps finished children. Hand-shell dispatch from the skill
remains possible but is no longer the canonical path.

### 2. Env scrub for every agy child

`scrubbed_env()` = parent env minus credential-looking names (TOKEN, SECRET,
PASSWORD, API_KEY, …, DATABASE_URL, REDIS_URL, …), with an explicit keep list
(PATH, HOME, proxies, SSH_AUTH_SOCK, locale) and a settings `env_passthrough`
override. Applied to the meter's own subprocesses (`agy models` discovery,
silent reauth) and to `agy_dispatch` children.

### 3. Git-fingerprint read-only guard (`agy_guard`)

`phase=before` records HEAD + porcelain + untracked (size, mtime) under a key;
`phase=after` compares — CLEAN / DRIFT (with changed files) / NOT_A_REPO /
NO_BASELINE. A dirty baseline is pinned exactly, so drift remains detectable.
`agy_dispatch(readonly=true, workdir=…)` records the baseline automatically
(key `dispatch:<run_id>`); the parent calls `agy_guard(after)` at harvest.
Untracked file contents are not hashed (same honest limitation gogogoogle
documents); size+mtime catches most in-place edits.

### 4. Heuristic-default classifier; Kev is opt-in

`classify()` returns the keyword heuristic immediately. The Kev-4B/LM-Studio
chain activates only with settings `{"classifier": "kev"}`. The ctx.llm
escalation tier is removed. Trade-off accepted: the `low_confidence` abstain
flag disappears in default mode — it fired on context-bound chunks where the
parent applies judgment by design (ADR-001 decision 3).

### 5. Middleware narrowed, not removed

`delegation.fallback_providers: [{provider: zai, model: glm-5.3}]` now gives
delegate children the flash→glm-5.3 rescue at the correct layer. The
middleware stays — narrowed from `endswith("flash")` to the exact pinned
`glm-5.3-flash` — because aux tasks (vision/compression/titling) still have
no core fallback chain. Full removal is pending aux-fallback support.

### 6. Housekeeping

Per-slug usage stats (calls/errors/tokens) surfaced in `agy_status` + the
pane; `/reauth` no longer snapshots (clobber fix); the plugin is a git repo
with a weekly no-agent cron CI (`agy-meter-ci`, Mondays 09:00, silent when
green).

---

## Consequences

- **Positive**: attribution structural; credentials never reach the closed
  binary; read-only contract mechanically verified; one less runtime
  dependency (Ollama) in the default path; instant constant-latency routing;
  a bug fixed; drift from core now caught weekly.
- **Accepted costs**: Kev's calibrated signal off by default (reversible via
  setting); guard hashes untracked files' metadata, not contents; the
  middleware remains as a narrow aux-task patch until core grows aux
  fallbacks; `agy_dispatch` results still arrive via file harvest (no push
  notification when a run finishes).

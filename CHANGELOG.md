# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-11-15

### Added
- **`agy_dispatch` tool (ADR-003)**: meter-owned dispatch — detached, list-argv (no shell-quoting pitfalls), output written by the meter to `runs/<slug>__<run_id>.json` (attribution is structural, not a hand-naming convention), model from the owner's picker (explicit slug or `route:`), env-scrubbed child, returns run_id + paths immediately. `readonly=true` records a git-fingerprint baseline automatically.
- **`agy_guard` tool (ADR-003)**: git-fingerprint read-only guard — `phase=before` records HEAD + porcelain + untracked size/mtime; `phase=after` compares (CLEAN / DRIFT with changed files / NOT_A_REPO / NO_BASELINE). Pattern credit: josefacero/hermes-gogogoogle.
- **Env scrub** (pattern credit: soyelmismo/hermes-antigravity-subscription): every agy subprocess (discovery, silent reauth, dispatch children) runs with credential-looking env vars stripped; keep-list + settings `env_passthrough` override.
- Per-slug usage stats (calls / errors / tokens) in `agy_status` and the pane's "model usage" data.
- Plugin is now a git repo with a weekly no-agent cron CI (`agy-meter-ci`, Mondays 09:00, silent when green).

### Changed
- **Classifier is heuristic-first** (owner ruling 2026-11-15): instant, zero-network keyword routing by default; the Kev-4B/LM-Studio chain is opt-in via settings `{"classifier": "kev"}`; the ctx.llm escalation tier is removed.
- `delegation.fallback_providers` (zai glm-5.3) now covers delegate children; the flash-fallback middleware is NARROWED to the exact pinned model `glm-5.3-flash` (old suffix match would have caught any future `*-flash` model) and stays only for aux tasks until core grows aux fallbacks.
- `/reauth` no longer snapshots — a throwaway Meter's `attempt_silent_reauth()` was clobbering the live meter's state.json with empty pools (owner-found bug).

## [0.2.0] - 2026-11-14

### Added
- Model-selectable routing (ADR-002): routes `plan` / `parallel` replace the four model-locked lanes. The model per route is the owner's ordered selection (settings.json `routes`), edited from a model picker in the desktop pane.
- Model catalog discovery from `agy models` (persisted to `models.json`, auto-refresh every 6h, manual `POST /models/refresh`): new releases (e.g. Gemini 4 Hydrogen) appear in the picker automatically, grouped by base model with effort variants and pool tags.
- Dashboard API: `GET /routes` (selection + catalog), `POST /routes` (catalog-validated save), `POST /models/refresh`.
- `agy_route` optional `route` param (plan|parallel|delegate) pinning a whole batch to one route, skipping per-chunk classification.

### Changed
- **Only owner-selected models are ever routable.** Pressure resolution walks the route's ordered selection; when every selection's pool is hot, the primary is returned with `pool_warning` — never an unselected substitute. The old cross-lane `FALLBACKS` graph is removed.
- `hermes_delegate` unchanged and fixed (zai, context-bound); classifier criteria and heuristic rebuilt for the three-route partition.
- `agy_route` response items carry `route` + `model` (+ `routed_from`/`reason` on fallback) instead of `lane`/`slug`; the reply echoes `route_models`.

## [0.1.0] - 2026-11-04

### Added
- Desktop statusbar chip displaying exact weekly token burn per pool (`g`/`a`/`3rd`) and auth-minute countdown, with clickable silent re-auth.
- Telemetry pane for tracking usage metrics and pool health.
- `agy_route` 5-lane classifier tool (Kev-4B via Ollama `/v1/systemone`, fallback to LM Studio Gemma, `ctx.llm`, and heuristic classification) with pressure-aware lane substitution.
- `agy_status` tool for inspecting pool usage and authentication states.
- `llm_execution` middleware retrying `glm-5.3-flash` 429 rate-limit responses once on `glm-5.3`.
- Keychain auth meter with automatic silent re-auth every 15 minutes.
- `POST /reauth` HTTP endpoint for triggering manual or programmatic re-authentication.
- Three distinct agy token pools (Gemini shared, Argon dedicated, third-party) with rolling 5-hour and 7-day windows.
- Budget self-calibration triggered automatically on the first 429 response.
- Evidence-based ZAI pressure cooling mechanism (3 successful GLM completions clear hot state, backed by a 5-minute idle backstop).
- Dry-bucket detection with recovery handling for unparseable run corpses.

### Changed
- Redesigned statusbar chip to be exact-only, removing percentage estimates and locally-anchored window clocks after Google quota APIs proved unverifiable.

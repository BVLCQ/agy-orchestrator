# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

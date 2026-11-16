# ADR 001: Model Routing for a Multi-Provider Agent Setup

## Status

**Accepted**: 2026-11-05

---

## Context

The setup has two execution surfaces and one local decision model:

| Component | Role | Quota characteristics |
|---|---|---|
| **Hermes** (`glm-5.3` on the z.ai plan) | Personal agent. Holds the session context and orchestrates the work. | Unlimited plan. In practice it still gets transient provider-level throttling. |
| **Antigravity CLI** (`agy`) | Outside execution lane for heavier or specialized chunks. | Subscription with three separate quota buckets: **Gemini (shared)**, **Argon (dedicated)** and **third-party (Claude/GPT)**. |
| **Kev-4B** (local, via Ollama `/v1/systemone`) | Calibrated decision model. | Local, no quota. Returns class probabilities in one pass. |

Constraints that shape the design:

1. **Google exposes no quota API.** Only the IDE panel shows real usage. Remaining budget can't be read in code. It can only be estimated, or learned when a request is denied.
2. **The Antigravity CLI is a separate process with no session context.** Each `agy` call starts cold. It can't see Hermes's conversation, open files or working state unless they are put into the prompt.
3. **A 429 middleware already exists** for provider-level retries inside a single provider call path.
4. **Zero manual steps.** The user won't reconcile quotas by hand or confirm routing decisions.

The open question is where routing decisions are made, what they choose between, and how the system handles quota limits it can't measure directly.

---

## Decision

### 1. Route at dispatch time, not in middleware

The route is chosen **once, when a chunk is dispatched**. It is not chosen in the 429 middleware.

- The `agy` lane runs as a separate process. Once it starts, it can't be redirected mid-flight to another provider. There is no shared request object to rewrite.
- The 429 middleware keeps its narrow job: retries within a single provider. It is not a routing layer.
- Pressure signals from the middleware (429 timestamps, success counts) **feed into** later dispatch decisions. They don't change requests already in flight.

### 2. Five lanes

| Lane | Backend | Use | Auto-reroute? |
|---|---|---|---|
| `hermes_delegate` | Hermes / z.ai (`glm-5.3`) | Chunks tied to the session context: work that needs conversation state, local working memory or ongoing intent. | **Never.** Context can't move to a cold process. |
| `opus_complex` | `agy`, third-party bucket (Claude Opus) | Complex reasoning, architecture, hard multi-step analysis. | Yes |
| `sonnet_coding` | `agy`, third-party bucket (Claude Sonnet) | Standard coding and patch drafting. | Yes |
| `argon_high` | `agy`, Argon dedicated bucket | High-capability work that can go to the dedicated pool. | Yes |
| `flash_high` | `agy`, Gemini shared bucket (Flash) | Fast, high-volume or lightweight chunks. | Yes |

`hermes_delegate` stands apart because it is the only lane that has the session context. The other four are interchangeable to varying degrees, so they can substitute for each other under pressure.

### 3. A calibrated classifier, with a confidence floor on the context axis

Kev-4B classifies each chunk in one pass and returns calibrated probabilities over the lanes.

- **If top-class confidence is ≥ 0.35**, the classifier's choice is the starting route, before any pressure substitution (Decision 4).
- **If confidence is < 0.35**, the **delegate-vs-agy decision goes to the parent** (Hermes). The parent knows what context the chunk depends on. The classifier only sees the chunk text.
- Why: the classifier is **reliably uncertain** on chunks tied to the session context. Its low confidence there is a well-calibrated sign that the answer depends on information it doesn't have, so it is treated as a handoff signal rather than noise.
- The classifier keeps full authority over **which `agy` lane** to use once the work is known to be context-free.

### 4. Pressure substitution instead of hard blacklists

Pools aren't disabled. They are marked **hot**, and dispatch picks the next suitable lane.

**An `agy` pool is hot when:**
- its estimated burn is **≥ 80%** of its internal budget, **or**
- it returned a **429 in the last 30 minutes**.

**z.ai (`hermes_delegate`) cools on evidence, not on a timer:**
- after throttling, z.ai stays hot until **3 successful GLM calls** are seen.
- This fits an unlimited plan, where throttling is transient and not tied to a quota window, so a clock-based cooldown would be arbitrary.

**Substitution rules:**
- A hot pool sends the chunk to the next-best lane for its class (for example, `opus_complex` → `argon_high`, or `sonnet_coding` → `argon_high` / `flash_high`).
- `hermes_delegate` is never auto-substituted (Decision 2). Pressure on z.ai delays or queues context-bound work. It doesn't move that work to `agy`.
- Every lane stays reachable. Hot only lowers a lane's priority, and lanes come back as the evidence changes.

### 5. Exact-only display

The status chip shows **only values that can be verified**: observed 429s, success counts and lane in use. It doesn't show percentage estimates.

- Estimates were shown at first. They were **removed after the quota denominators proved unverifiable**: without a quota API, the "out of N" had no ground truth, and the chip suggested a precision the system didn't have.
- **Budgets are still internal machinery.** They drive the ≥ 80% hot threshold and are **calibrated by the first 429** in each window. The budget is set to the burn observed when that 429 arrived.

### 6. Repo work: `agy` drafts, Hermes applies and gates

For changes to the repository:

1. Hermes **embeds the relevant file contents** in the dispatch prompt, because `agy` has no session or filesystem context.
2. The `agy` lane returns a **draft patch**.
3. Hermes **applies the patch and runs the gates** (tests, lint, type checks and other checks).
4. Every write to the working tree goes through Hermes, which holds the context and is responsible for the result.

---

## Alternatives Considered

### A. Delegation-provider plugin
Put routing in a provider plugin inside `delegate_task`.

**Rejected: wrong layer.** The plugin runs *inside* Hermes's delegation path and can only choose among Hermes-side providers. It can't reach `agy`, a separate process with its own quota buckets. Routing has to happen above both execution surfaces.

### B. Middleware rerouting
Extend the 429 middleware to send failed or throttled requests to another lane.

**Rejected: impossible mid-flight.** The `agy` lane runs as its own process. Once dispatched, there is no request to intercept and resend elsewhere. The middleware can retry within a provider. It can't move work across process boundaries.

### C. Vision-scraping the IDE quota panel
Capture the IDE's quota panel and parse real usage from screenshots.

**Rejected: fragile machinery for cosmetic parity.** It would depend on UI layout, rendering and window state, all of which change without notice. The gain would be a nicer chip, while routing already works from 429 calibration. That is a poor trade of reliability for appearance.

### D. Manual sync
Have the user periodically copy real usage from the IDE panel into the system.

**Rejected: the user wants zero manual steps.** It would also go stale between syncs, so it wouldn't remove the need for 429 calibration anyway.

---

## Consequences

### Positive
- **Routing matches how the processes actually work.** Decisions are made where they can be acted on: at dispatch, before a process boundary is crossed.
- **Context-bound work stays in place.** `hermes_delegate` is never auto-rerouted, and uncertain chunks go to the parent, so context-dependent work isn't silently sent to a cold process.
- **Degrades gracefully.** Pressure substitution keeps all lanes available and changes their priority, so no single hot pool stops work.
- **Honest display.** The chip shows only verifiable numbers, so it can't mislead.
- **Safe repo changes.** Hermes applies and gates every patch, so the tree is never changed by a process that lacks context.

### Negative / Accepted costs
- **One probe dispatch per window misalignment.** Without a quota API, the local estimate of when a window resets can differ from the account's. The first dispatch after the estimated reset acts as a probe: it either succeeds, confirming the reset, or gets a 429 and recalibrates the budget.
- **Local burn can drift from the account ledger.** The local count and Google's real usage can diverge, for example through IDE usage outside this system. The drift is **bounded by 429 calibration**: each denial resets the budget to observed reality.
- **The classifier is only advisory on the context axis.** Below 0.35 confidence, the delegate-vs-agy decision belongs to the parent. Kev-4B is authoritative only for choosing among `agy` lanes. Some decisions will cost parent reasoning instead of being made cheaply by the classifier.
- **Prompt size for repo work.** Because `agy` has no file access, file contents have to be embedded in each dispatch, which uses tokens from the receiving bucket.

### Follow-ups
- If Google ever publishes a quota API, revisit Decision 5 and the probe-dispatch cost.
- Track how often chunks fall below 0.35 confidence and how the parent decides them. If a pattern appears, it can be fed back into classifier calibration.

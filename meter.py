"""agy-orchestrator — quota meter, router and owned dispatch for the
Antigravity (agy) offload lane.

Owns: keychain auth state (token held in memory only, never written to state),
server-side quota snapshot (loadCodeAssist, display-only, owner-ruled 2026-11-04),
per-pool metering of agy dispatches (~/.hermes/cache/agy-orchestrator/runs/),
model-selectable routing with owner picker (ADR-002), env-scrubbed git-guarded
dispatch (ADR-003), and GLM-side provider telemetry fed by observer hooks.
State is mirrored to ~/.hermes/cache/agy-orchestrator/state.json for the chip.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
CACHE_DIR = HERMES_HOME / "cache" / "agy-orchestrator"
RUNS_DIR = CACHE_DIR / "runs"
STATE_FILE = CACHE_DIR / "state.json"


def _migrate_legacy_cache() -> None:
    """One-time rename from the pre-public name (agy-meter). Runs at import,
    before any reader; never overwrites an existing new-style directory."""
    legacy = HERMES_HOME / "cache" / "agy-meter"
    try:
        if legacy.is_dir() and not CACHE_DIR.exists():
            legacy.rename(CACHE_DIR)
    except OSError:
        pass


_migrate_legacy_cache()

KEYCHAIN_CMD = ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"]
LCA_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"

WINDOW_H = 5.0        # agy baseline quota window (hours)
AUTH_EVERY_S = 300
QUOTA_EVERY_S = 900
INGEST_EVERY_S = 45
REAUTH_RETRY_S = 900  # min gap between silent re-auth attempts
ZAI_429_BACKSTOP_S = 300   # idle backstop only; evidence (3 GLM successes) cools sooner
ZAI_COOL_SUCCESSES = 3

SETTINGS_FILE = CACHE_DIR / "settings.json"
# Google publishes no absolute quota; % is against an ESTIMATE until a real 429
# calibrates it (first 429 in a window sets budget = tokens consumed then).
DEFAULT_BUDGETS = {"gemini": 20_000_000, "argon": 5_000_000, "third_party": 5_000_000}

# ---- model-selectable routing (owner ruling 2026-11-14, ADR-002) ----
# Discovered model catalog (from `agy models`), separate file so the dashboard
# API can refresh it without racing state.json snapshot writes.
MODELS_FILE = CACHE_DIR / "models.json"
MODELS_EVERY_S = 6 * 3600
# The owner's per-route ordered selection (primary -> fallback). Seeded with
# the ruling of 2026-11-14; from then on ONLY what the picker saves is used.
ROUTE_DEFAULTS = {
    "plan": ["claude-opus-5-5-high", "gemini-4-hydrogen-high"],
    "parallel": ["claude-sonnet-5-5-high", "gemini-3.8-flash-high"],
}
# hermes_delegate is fixed (context-bound, never rerouted — ADR-001); mirrored
# here so meter.py stays loadable standalone (no classifier import).
DELEGATE_MODEL = "delegate_task/glm-5.3-flash"

# ---- dispatch hygiene (owner-approved 2026-11-15; pattern credit:
# soyelmismo/hermes-antigravity-subscription env scrub,
# josefacero/hermes-gogogoogle git-fingerprint guard) ----
GUARDS_FILE = CACHE_DIR / "guards.json"
# Google emits real reset horizons inside 429 error text ("Individual quota
# reached. Resets in 4h 12m") — the only Google-sourced clock we ever get.
_RESET_RE = re.compile(r"Resets in (\d+)\s*h\s*(?:(\d+)\s*m)?")
# agy is a closed binary: never hand it credential-looking env vars. Policy =
# parent env minus matches; the keep-list wins first, settings
# "env_passthrough" names survive on top (settings.json).
ENV_CRED_SUBSTRINGS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "ACCESS_KEY",
                       "PRIVATE_KEY", "CREDENTIAL", "AUTH", "WEBHOOK", "DSN", "COOKIE")
ENV_CRED_NAMES = ("DATABASE_URL", "REDIS_URL", "MONGO_URI", "MONGO_URL",
                  "ELASTICSEARCH_URL", "AMQP_URL")
ENV_ALWAYS_KEEP = ("PATH", "HOME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
                   "LC_MESSAGES", "TZ", "TERM", "TMPDIR", "TEMP", "TMP",
                   "SSH_AUTH_SOCK", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                   "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
                   "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "HERMES_HOME")
_KEEP_UPPER = {k.upper() for k in ENV_ALWAYS_KEEP}


def scrubbed_env(passthrough=None) -> dict:
    """Parent environment minus credential-looking entries, for agy children
    (closed binary — it needs none of them). Matching is name-based, upper-
    case-insensitive (passthrough included); the keep list always survives."""
    passthrough = passthrough or ()
    extra = {str(p) for p in passthrough}
    extra_upper = {str(p).upper() for p in passthrough}
    out = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if k in extra or ku in extra_upper or ku in _KEEP_UPPER:
            out[k] = v
            continue
        if ku in ENV_CRED_NAMES or any(s in ku for s in ENV_CRED_SUBSTRINGS):
            continue
        out[k] = v
    return out


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def _save_settings(s: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=1))
    except Exception:
        pass


# ---- model catalog discovery (`agy models`) ----
_EFFORT_ORDER = {"low": 0, "med": 1, "medium": 1, "high": 2, "xhigh": 3, "max": 4}


def _split_effort(slug: str) -> tuple[str, str | None]:
    """Split an agy slug into (base, effort): claude-opus-5-5-high ->
    (claude-opus-5-5, high). Slugs without an effort suffix (e.g.
    gemini-pro-orchestrator-g1-pro) return (slug, None)."""
    for suf in ("xhigh", "medium", "high", "low", "med", "max"):
        if slug.endswith("-" + suf) and len(slug) > len(suf) + 1:
            return slug[: -(len(suf) + 1)], suf
    return slug, None


def parse_models_output(text: str) -> dict:
    """Parse `agy models` TSV output (slug<TAB>Label per line) into a grouped
    catalog: bases with ordered effort variants, pool tags, flat slug list."""
    bases: dict[str, dict] = {}
    for line in (text or "").splitlines():
        if "\t" not in line:
            continue  # "Fetching available models..." banner / noise
        slug, label = (p.strip() for p in line.split("\t", 1))
        if not slug:
            continue
        base, effort = _split_effort(slug)
        b = bases.setdefault(base, {"base": base, "label": label or slug,
                                    "variants": []})
        b["variants"].append({"effort": effort, "slug": slug})
    for b in bases.values():
        b["variants"].sort(key=lambda v: _EFFORT_ORDER.get(v["effort"], 9))
        b["pool"] = _pool_for(b["variants"][0]["slug"])
    return {"bases": sorted((b for b in bases.values()), key=lambda b: b["base"]),
            "slugs": sorted({v["slug"] for b in bases.values() for v in b["variants"]})}


def discover_models() -> dict:
    """Run `agy models` and persist the catalog to models.json. Never raises;
    returns an error-carrying dict when discovery fails (stale file wins)."""
    try:
        out = subprocess.run([_agy_bin(), "models"], capture_output=True,
                             text=True, timeout=90, env=scrubbed_env())
        cat = parse_models_output(out.stdout or "")
        if not cat["slugs"]:
            raise ValueError("no models parsed from `agy models` output")
    except Exception as e:
        return {"error": str(e)[:120], "bases": [], "slugs": [],
                "fetched_at": None}
    cat["fetched_at"] = _iso(_now())
    try:
        tmp = MODELS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cat, indent=1))
        tmp.replace(MODELS_FILE)
    except Exception:
        pass
    return cat


def read_models() -> dict:
    try:
        data = json.loads(MODELS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {"bases": [], "slugs": [], "fetched_at": None}


# ---- route config (owner selection; ADR-002) ----
def get_routes_from(settings: dict) -> dict:
    """Effective route->models map: the owner's saved selection per route,
    else the seeded defaults. Invalid shapes fall back per-route, never
    silently to an unselected model."""
    saved = settings.get("routes") if isinstance(settings, dict) else None
    saved = saved if isinstance(saved, dict) else {}
    out = {}
    for route, default in ROUTE_DEFAULTS.items():
        sel = saved.get(route)
        sel = ([s for s in sel if isinstance(s, str) and s.strip()]
               if isinstance(sel, list) else [])
        out[route] = sel or list(default)
    return out


def save_routes(routes: dict) -> dict:
    """Validate and persist the owner's route->models selection into
    settings.json (merged, budgets untouched). Raises ValueError on bad input.
    Slugs are validated against the discovered catalog when one exists."""
    if not isinstance(routes, dict) or set(routes) != set(ROUTE_DEFAULTS):
        raise ValueError(f"routes must be an object with keys {sorted(ROUTE_DEFAULTS)}")
    known = set(read_models().get("slugs") or [])
    clean = {}
    for route, models in routes.items():
        if (not isinstance(models, list) or not models
                or not all(isinstance(s, str) and s for s in models)):
            raise ValueError(f"route {route}: non-empty list of model slugs required")
        if known:
            unknown = [s for s in models if s not in known]
            if unknown:
                raise ValueError(f"route {route}: not in catalog "
                                 f"(refresh models?): {', '.join(unknown)}")
        dedup: list[str] = []
        for s in models:
            if s not in dedup:
                dedup.append(s)
        clean[route] = dedup
    s = _load_settings()
    s["routes"] = clean
    _save_settings(s)
    return clean


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _pool_for(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("gemini-4-argon"):
        return "argon"          # dedicated quota bucket (IDE Usage panel, 2026-11-04)
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith(("claude", "gpt-oss")):
        return "third_party"
    return "other"


def _fresh_pool() -> dict:
    return {"window_start": _iso(_now()), "tokens_in": 0, "tokens_out": 0,
            "thinking_tokens": 0, "calls": 0, "errors": 0, "lifetime_tokens": 0,
            "week_start": _iso(_now()), "week_tokens": 0}


def _load_run_record(text: str) -> dict | None:
    """Parse one run record out of a run file's text. agy writes a single JSON
    object; hand-wrapped dispatches (the pre-0.2 wrapper convention, still
    honored by the README) may append a trailing status line ("NAME EXIT=0")
    after it. raw_decode parses the leading object and ignores trailing data,
    so those runs count as the successes they were instead of fabricating
    pool errors with zero tokens. Returns None when no JSON object leads —
    that file is a genuine corpse."""
    try:
        rec = json.loads(text)
        return rec if isinstance(rec, dict) else None
    except Exception:
        pass
    try:
        rec, _ = json.JSONDecoder().raw_decode(text.lstrip())
        if isinstance(rec, dict):
            return rec
    except Exception:
        pass
    return None


def _in_window(p: dict, mt) -> bool:
    """True when a run file's mtime falls inside the pool's current 5h window.
    A fresh process replays every retained run file (state is rebuilt from
    files, cursor 0): week/lifetime counters are retention-bounded (7d) so
    replay is honest there, but a fresh 5h window must not absorb week-old
    burn, errors, or 429 calibration (restart re-attribution quirk)."""
    try:
        return float(mt) >= datetime.fromisoformat(
            p["window_start"]).timestamp()
    except Exception:
        return True


def _agy_bin() -> str:
    return shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")


def _git(workdir: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", workdir, *args], capture_output=True,
                         text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "git failed").strip()[:120])
    return out.stdout


def _tree_fingerprint(workdir: str) -> dict:
    """HEAD + sorted porcelain + untracked (size, mtime) -> sha256[:16].
    Untracked file CONTENTS are not hashed (same limitation gogogoogle
    documents honestly); size+mtime catches most in-place edits."""
    head = _git(workdir, "rev-parse", "HEAD").strip()
    status = _git(workdir, "status", "--porcelain=v1")
    untracked = []
    for line in status.splitlines():
        if line.startswith("??"):
            p = line[3:].strip().strip('"')
            try:
                st = (Path(workdir) / p).stat()
                untracked.append(f"{p}:{st.st_size}:{int(st.st_mtime)}")
            except OSError:
                untracked.append(f"{p}:?:?")
    blob = "\n".join([head, *sorted(status.splitlines()), *sorted(untracked)])
    return {"head": head, "dirty": bool(status.strip()),
            "fp": hashlib.sha256(blob.encode()).hexdigest()[:16]}


class Meter:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.started = False
        self._cursor = 0.0
        self._bearer: str | None = None
        self._next_reauth_attempt = 0.0
        self._glm_success_streak = 0
        self.settings = _load_settings()
        self._settings_mtime = (SETTINGS_FILE.stat().st_mtime
                                if SETTINGS_FILE.exists() else None)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.state = {
            "updated_at": None,
            "auth": {"ok": None, "email": None, "expires_at": None,
                     "minutes_left": None, "needs_reauth": None, "error": None},
            "quota": {"http": None, "fetched_at": None, "data": None, "error": None},
            "pools": {"gemini": _fresh_pool(), "argon": _fresh_pool(),
                      "third_party": _fresh_pool(), "other": _fresh_pool()},
            "glm": {},          # "provider/model" -> {tps_ewma, calls, tokens_out, last_at}
            "errors": [],       # recent provider errors [{ts, provider, model, status}]
            "last_429_at": None,
            "classifier": {"last_via": None, "last_at": None},
            "models": read_models(),   # discovered catalog (ADR-002 picker)
            "slugs": {},        # per-slug run stats {calls, errors, tokens} (2026-11-15)
            "dispatches": [],   # recent agy_dispatch spawns (last 100)
            "last_error": None,
        }
        self._next_auth = 0.0
        self._next_quota = 0.0
        self._next_models = 0.0
        self._dispatched: dict[int, dict] = {}
        self._dispatch_seq = 0

    # ---------- settings / routes (ADR-002) ----------
    def _reload_settings_if_stale(self) -> None:
        """Pick up external writes to settings.json: the pane picker saves
        routes via the dashboard API from outside this process. Budget
        calibration re-reads first too, so the two writers don't clobber
        each other's keys."""
        try:
            mt = SETTINGS_FILE.stat().st_mtime
        except OSError:
            return
        if mt != self._settings_mtime:
            self.settings = _load_settings()
            self._settings_mtime = mt

    def get_routes(self) -> dict:
        self._reload_settings_if_stale()
        return get_routes_from(self.settings)

    def resolve_model(self, route: str, routes_cfg: dict | None = None) -> dict:
        """Resolve the model for a route from the OWNER'S ordered selection.
        Only selected models are ever used: a hot primary falls to the next
        selected model; if every selection sits on a hot pool the primary is
        returned with pool_warning — never an unselected substitute."""
        if route == "hermes_delegate":
            return {"model": DELEGATE_MODEL, "pool": "zai"}
        models = (routes_cfg or self.get_routes()).get(route) or []
        if not models:
            return {"model": None, "pool": None,
                    "pool_warning": f"route {route}: no models selected"}
        hot: dict[str, bool] = {}
        for i, slug in enumerate(models):
            pool = _pool_for(slug)
            if pool not in hot:
                hot[pool] = bool(self.pool_pressure(pool).get("hot"))
            if not hot[pool]:
                res = {"model": slug, "pool": pool}
                if i > 0:
                    res["routed_from"] = models[0]
                    res["reason"] = "primary pool hot — owner's next selection"
                return res
        return {"model": models[0], "pool": _pool_for(models[0]),
                "pool_warning": "all selected models' pools are hot — "
                                "widen the selection or wait"}

    # ---------- classifier settings (Kev opt-in, 2026-11-15) ----------
    def classifier_settings(self) -> dict:
        self._reload_settings_if_stale()
        out = {}
        for k in ("classifier", "decision_model", "lmstudio_model"):
            v = self.settings.get(k)
            if isinstance(v, str) and v:
                out[k] = v
        return out

    # ---------- git-fingerprint read-only guard ----------
    def _load_guards(self) -> dict:
        try:
            g = json.loads(GUARDS_FILE.read_text())
            return g if isinstance(g, dict) else {}
        except Exception:
            return {}

    def guard_before(self, workdir: str, key: str | None = None) -> dict:
        """Fingerprint workdir's git tree (HEAD + porcelain + untracked
        size/mtime) and store it under key. Verdicts: BASELINE (clean tree),
        BASELINE_DIRTY (dirt is pinned exactly — drift still detectable),
        NOT_A_REPO."""
        key = key or workdir
        try:
            fp = _tree_fingerprint(workdir)
        except Exception as e:
            return {"key": key, "workdir": workdir, "verdict": "NOT_A_REPO",
                    "error": str(e)[:120]}
        rec = {"workdir": workdir, **fp, "recorded_at": _iso(_now())}
        g = self._load_guards()
        g[key] = rec
        try:
            GUARDS_FILE.write_text(json.dumps(g, indent=1))
        except Exception:
            pass
        return {"key": key, **rec,
                "verdict": "BASELINE" if not fp["dirty"] else "BASELINE_DIRTY"}

    def guard_after(self, workdir: str, key: str | None = None) -> dict:
        """Compare workdir's tree against the stored baseline. CLEAN =
        identical fingerprint; DRIFT = anything changed (read-only contract
        violated or an external edit) — inspect before trusting the run."""
        key = key or workdir
        base = self._load_guards().get(key)
        if not isinstance(base, dict) or "fp" not in base:
            return {"key": key, "workdir": workdir, "verdict": "NO_BASELINE",
                    "error": "no guard_before for this key"}
        try:
            now = _tree_fingerprint(workdir)
        except Exception as e:
            return {"key": key, "workdir": workdir, "verdict": "NOT_A_REPO",
                    "error": str(e)[:120]}
        if now["fp"] == base["fp"]:
            return {"key": key, "head": now["head"], "verdict": "CLEAN"}
        try:
            changed = [l[3:].strip().strip('"')
                       for l in _git(workdir, "status", "--porcelain=v1").splitlines()]
        except Exception:
            changed = []
        return {"key": key, "verdict": "DRIFT", "head": now["head"],
                "baseline_head": base.get("head"), "changed": changed[:40]}

    # ---------- owned dispatch (structural metering, 2026-11-15) ----------
    def dispatch(self, prompt: str, model: str, name: str = "run",
                 timeout_min: int = 15, workdir: str | None = None,
                 readonly: bool = False) -> dict:
        """Spawn a detached, env-scrubbed agy run whose output lands in runs/
        under the meter's own <slug>__ naming — attribution is STRUCTURAL, not
        a hand-naming convention. Returns immediately (harvest via read_file);
        the meter ingests the run file on its next tick. readonly=True
        fingerprints workdir now (key "dispatch:<run_id>"); call guard_after
        with that key when harvesting to verify the tree never changed."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt required")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model slug required")
        if not re.fullmatch(r"[\w.+-]+", model):
            # the slug becomes a filename under runs/ — no traversal, no spaces
            raise ValueError(f"invalid model slug: {model!r}")
        safe = "".join(c if (c.isalnum() or c in "._-") else "-"
                       for c in str(name or "run"))[:60] or "run"
        with self.lock:
            self._dispatch_seq += 1
            seq = self._dispatch_seq
        run_id = f"{safe}-{int(_now())}-{seq}"
        out_path = RUNS_DIR / f"{model}__{run_id}.json"
        err_path = out_path.with_suffix(".err")
        cmd = [_agy_bin(), "-p", prompt, "--model", model,
               "--output-format", "json", "--print-timeout", f"{int(timeout_min)}m"]
        guard = (self.guard_before(workdir, key=f"dispatch:{run_id}")
                 if readonly and workdir else None)
        with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
            proc = subprocess.Popen(cmd, stdout=fo, stderr=fe, cwd=workdir or None,
                                    env=scrubbed_env(self.settings.get("env_passthrough")),
                                    start_new_session=True)
        with self.lock:
            self._dispatched[proc.pid] = {"proc": proc, "out": str(out_path),
                                          "model": model}
            d = self.state.setdefault("dispatches", [])
            d.append({"run_id": run_id, "model": model, "pool": _pool_for(model),
                      "pid": proc.pid, "started_at": _iso(_now()),
                      "output": str(out_path),
                      "readonly": bool(guard and guard.get("fp")),
                      "guard_key": (guard or {}).get("key")})
            self.state["dispatches"] = d[-100:]
        return {"run_id": run_id, "model": model, "pool": _pool_for(model),
                "pid": proc.pid, "output_file": str(out_path),
                "stderr_file": str(err_path), "guard": guard,
                "note": "detached run; harvest output_file via read_file, then "
                        "guard_after on guard_key if readonly"}

    def _reap_one(self, pid: int, info: dict) -> None:
        """Retire a finished dispatch. If it died WITHOUT writing output (rc!=0
        and the run file is still empty/missing), account exactly one error
        against its pool and slug — the empty-file skip in ingest_runs would
        otherwise never see it (review finding #1, 2026-11-15)."""
        proc = info["proc"]
        with self.lock:
            self._dispatched.pop(pid, None)
        if proc.returncode == 0:
            return
        try:
            if Path(info["out"]).stat().st_size > 0:
                return  # real output landed; ingest accounting applies
        except OSError:
            pass
        pool = _pool_for(info["model"])
        p = self.state["pools"].setdefault(pool, _fresh_pool())
        p["errors"] = p.get("errors", 0) + 1
        sl = self.state.setdefault("slugs", {}).setdefault(
            info["model"], {"calls": 0, "errors": 0, "tokens": 0})
        sl["errors"] += 1
        self.record_error("agy", info["model"], "run:nowrite")

    def _reap_dispatched(self) -> None:
        while True:
            time.sleep(5)
            with self.lock:
                items = list(self._dispatched.items())
            for pid, info in items:
                if info["proc"].poll() is not None:
                    self._reap_one(pid, info)

    # ---------- auth ----------
    def _read_envelope(self) -> dict | None:
        try:
            out = subprocess.run(KEYCHAIN_CMD, capture_output=True, text=True, timeout=10)
            raw = (out.stdout or "").strip()
            if not raw.startswith("go-keyring-base6"):
                return None
            payload = None
            for sep in (":", ","):
                if sep in raw:
                    payload = raw.split(sep, 1)[1]
                    break
            if not payload:
                return None
            return json.loads(base64.b64decode(payload))
        except Exception:
            return None

    @staticmethod
    def _parse_ts(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
            except Exception:
                return None
        s = str(value).strip()
        for chop in (" -03", " MST"):
            if s.endswith(chop):
                s = s[: -len(chop)]
        try:
            return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:
            return None

    def refresh_auth(self) -> None:
        auth = self.state["auth"]
        env = self._read_envelope()
        if env is None:
            auth.update({"ok": False, "needs_reauth": True,
                         "error": "keychain read failed (not logged in?)"})
            self._bearer = None
            return
        token = env.get("token")
        bearer = None
        expires_at = None
        if isinstance(token, dict):
            for k in ("access_token", "accessToken"):
                if isinstance(token.get(k), str) and token[k]:
                    bearer = token[k]
            expires_at = self._parse_ts(
                token.get("expiry") or token.get("expires_at") or token.get("expiry_time"))
        elif isinstance(token, str) and token:
            bearer = token
            if token.count(".") >= 2:  # JWT: read exp claim
                try:
                    p = token.split(".")[1]
                    p += "=" * (-len(p) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(p))
                    expires_at = self._parse_ts(claims.get("exp"))
                except Exception:
                    pass
        minutes = None
        if expires_at:
            try:
                minutes = round((datetime.fromisoformat(expires_at)
                                 - datetime.now(timezone.utc)).total_seconds() / 60)
            except Exception:
                minutes = None
        auth.update({
            "ok": True,
            "email": env.get("id_token_email"),
            "expires_at": expires_at,
            "minutes_left": minutes,
            "needs_reauth": minutes is not None and minutes <= 0,
            "error": None,
        })
        self._bearer = bearer  # memory only; never persisted, never logged

    # ---------- quota (display-only; owner ruling 2026-11-04) ----------
    def refresh_quota(self) -> None:
        if not self._bearer:
            self.state["quota"].update({"http": None, "data": None,
                                        "error": "no bearer token (auth first)"})
            return
        req = urllib.request.Request(
            LCA_URL, data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {self._bearer}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read(200_000)
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
            try:
                body = e.read(50_000)
            except Exception:
                body = b""
            if code in (401, 403):
                self.state["auth"]["needs_reauth"] = True
        except Exception as e:
            self.state["quota"].update({"http": None, "error": str(e)[:120]})
            return
        try:
            data = json.loads(body)
        except Exception:
            data = {"raw": body[:400].decode("utf-8", "replace")}
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if "token" not in k.lower()}
        self.state["quota"].update({"http": code, "fetched_at": _iso(_now()),
                                    "data": data, "error": None})

    # ---------- wrapped dispatch runs ----------
    def ingest_runs(self) -> None:
        now = _now()
        newest = self._cursor
        for f in RUNS_DIR.glob("*.json"):
            mt = None
            try:
                st = f.stat()
                mt = st.st_mtime
                if mt <= self._cursor:
                    continue
                if st.st_size == 0:
                    # In-flight dispatch: dispatch() creates the output file
                    # empty at spawn and agy writes JSON only at completion, so
                    # a tick landing mid-run must NOT count a phantom error.
                    # Never advance the cursor past it — the completed file
                    # (fresh mtime) gets ingested on a later tick. Runs that
                    # die without output are accounted by _reap_one.
                    continue
                rec = _load_run_record(f.read_text())
                if rec is None:
                    raise ValueError("no leading JSON object in run file")
            except Exception:
                # unparseable run file (hard-failed dispatch can die mid-write):
                # still count it against the pool named in the slug prefix
                if "__" in f.name:
                    slug = f.name.split("__", 1)[0]
                    bp = self.state["pools"].setdefault(
                        _pool_for(slug), _fresh_pool())
                    if _in_window(bp, mt):
                        bp["errors"] = bp.get("errors", 0) + 1
                    sl = self.state.setdefault("slugs", {}).setdefault(
                        slug, {"calls": 0, "errors": 0, "tokens": 0})
                    sl["errors"] += 1
                newest = max(newest, mt) if isinstance(mt, float) else newest
                continue
            newest = max(newest, mt)
            # agy's json output carries no model slug: wrapper convention names
            # run files "<slug>__<task>.json"; prefer an explicit record field.
            model = rec.get("model") or f.name.split("__", 1)[0]
            pool = _pool_for(str(model))
            p = self.state["pools"].setdefault(pool, _fresh_pool())
            try:
                ws = datetime.fromisoformat(p["window_start"]).timestamp()
                if now - ws > WINDOW_H * 3600:
                    p.update({"window_start": _iso(now), "tokens_in": 0, "tokens_out": 0,
                              "thinking_tokens": 0, "calls": 0, "errors": 0})
                wk = datetime.fromisoformat(p.get("week_start") or p["window_start"]).timestamp()
                if now - wk > 7 * 86400:
                    p.update({"week_start": _iso(now), "week_tokens": 0})
            except Exception:
                p.update(_fresh_pool())
            usage = rec.get("usage") or {}
            tin = usage.get("input_tokens") or 0
            tout = usage.get("output_tokens") or 0
            th = usage.get("thinking_tokens") or 0
            in_window = _in_window(p, mt)
            if in_window:
                p["tokens_in"] += tin
                p["tokens_out"] += tout
                p["thinking_tokens"] += th
                p["calls"] += 1
            p["lifetime_tokens"] += tin + tout + th
            p["week_tokens"] = p.get("week_tokens", 0) + tin + tout + th
            status = str(rec.get("status") or "SUCCESS").upper()
            if status not in ("SUCCESS", "OK") and in_window:
                p["errors"] += 1
                errtxt = str(rec.get("error", ""))
                is_429 = "429" in errtxt or "quota" in errtxt.lower()
                # register against the ROUTER's pressure view (pool_pressure scans
                # state.errors); provider "agy" never heats zai_pressure
                self.record_error("agy", str(model),
                                  "429" if is_429 else f"run:{status}")
                if is_429:
                    self.state["last_429_at"] = rec.get("ts") or _iso(now)
                    self._calibrate_budget(pool, p)
                    m = _RESET_RE.search(errtxt)
                    if m:
                        # Real Google-sourced reset horizon, captured at the
                        # 429 — shown in the pane as "resets in ~Xh (429 @ …)".
                        p["quota_reset_min"] = int(m.group(1)) * 60 + int(m.group(2) or 0)
                        p["quota_reset_observed_at"] = rec.get("ts") or _iso(now)
            # per-slug stats (agy_status / pane "model usage", 2026-11-15)
            sl = self.state.setdefault("slugs", {}).setdefault(
                str(model), {"calls": 0, "errors": 0, "tokens": 0})
            sl["calls"] += 1
            sl["tokens"] += tin + tout + th
            if status not in ("SUCCESS", "OK"):
                sl["errors"] += 1
        cutoff = now - 7 * 86400
        for pat in ("*.json", "*.err"):
            for f in RUNS_DIR.glob(pat):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except Exception:
                    pass
        self._cursor = newest

    # ---------- budgets / window ----------
    def _calibrate_budget(self, pool: str, p: dict) -> None:
        """First observed 429 means the window budget ~= what the window had
        consumed when it dried. The observed capacity IS the truth — it replaces
        any estimate (default or explicit) and persists across restarts."""
        self._reload_settings_if_stale()  # picker may have written routes meanwhile
        if (self.settings.get("budgets") or {}).get(pool + "_calibrated"):
            return
        consumed = (p.get("tokens_in", 0) + p.get("tokens_out", 0)
                    + p.get("thinking_tokens", 0)) or p.get("lifetime_tokens", 0) or 1
        budgets = self.settings.setdefault("budgets", {})
        budgets[pool] = int(consumed * 1.05)
        budgets[pool + "_calibrated"] = True
        _save_settings(self.settings)

    def _pool_derived(self, p: dict, pool: str) -> dict:
        budgets = self.settings.get("budgets", {}) if hasattr(self, "settings") else {}
        budget = budgets.get(pool) or DEFAULT_BUDGETS.get(pool, 1)
        calibrated = bool(budgets.get(pool + "_calibrated"))
        consumed = (p.get("tokens_in", 0) + p.get("tokens_out", 0)
                    + p.get("thinking_tokens", 0))
        pct = round(min(100.0, 100.0 * consumed / budget), 1) if budget else None
        remaining_min = None
        week_remaining_min = None
        try:
            elapsed = _now() - datetime.fromisoformat(p["window_start"]).timestamp()
            remaining_min = max(0, round((WINDOW_H * 3600 - elapsed) / 60))
            welapsed = _now() - datetime.fromisoformat(
                p.get("week_start") or p["window_start"]).timestamp()
            week_remaining_min = max(0, round((7 * 86400 - welapsed) / 60))
        except Exception:
            pass
        return {"pct_used": pct, "budget_tokens": budget, "budget_calibrated": calibrated,
                "window_remaining_min": remaining_min,
                "week_remaining_min": week_remaining_min}

    def attempt_silent_reauth(self) -> dict:
        """Run a zero-quota agy command; its startup path performs silent auth
        when the cached refresh token is still valid. Returns fresh auth state.
        Deliberately NO snapshot(): the dashboard /reauth endpoint runs this on
        a throwaway Meter, and its snapshot would clobber the live meter's
        state.json with empty pools (owner-found bug, 2026-11-15)."""
        try:
            subprocess.run([_agy_bin(), "models"], capture_output=True,
                           timeout=45, env=scrubbed_env())
        except Exception as e:
            self.state["auth"]["auto_error"] = str(e)[:120]
        self.refresh_auth()
        self.state["auth"]["last_auto_attempt"] = _iso(_now())
        return self.state["auth"]

    def pool_pressure(self, pool: str) -> dict:
        """Routing pressure for a pool: hot = pct_used >= 80 or a 429 on any of
        its models within the last 30 minutes."""
        p = self.state.get("pools", {}).get(pool) or {}
        pct = p.get("pct_used")
        cutoff = _now() - 1800
        recent_429 = False
        for e in self.state.get("errors", [])[-50:]:
            try:
                if (_pool_for(str(e.get("model", ""))) == pool
                        and "429" in str(e.get("status", ""))
                        and datetime.fromisoformat(e["ts"]).timestamp() >= cutoff):
                    recent_429 = True
                    break
            except Exception:
                continue
        hot = recent_429 or (pct is not None and pct >= 80.0)
        return {"pool": pool, "pct_used": pct, "recent_429": recent_429, "hot": hot}

    def zai_pressure(self) -> dict:
        """zai account pressure with EVIDENCE-based cooling: hot while the last
        GLM 429 is recent (< ZAI_429_BACKSTOP_S) AND fewer than ZAI_COOL_SUCCESSES
        consecutive successful GLM calls have landed since it. A burst 429 whose
        retry immediately succeeds (the common case, observed 2026-11-04 12:42)
        cools within seconds; the timer is only the idle backstop. agy pools keep
        the 30-min leg (a dry quota bucket stays dry until its window resets)."""
        cutoff = _now() - ZAI_429_BACKSTOP_S
        last = None
        last_429_ts = None
        for e in reversed(self.state.get("errors", [])):
            try:
                prov = str(e.get("provider", ""))
                model = str(e.get("model", ""))
                if "429" in str(e.get("status", "")) and (
                        prov == "zai" or prov == "agy-orchestrator-mw"
                        or model.startswith("glm")):
                    last = e["ts"]
                    last_429_ts = datetime.fromisoformat(e["ts"]).timestamp()
                    break
            except Exception:
                continue
        if last_429_ts is None:
            return {"hot": False, "last_429_at": None}
        recent = last_429_ts >= cutoff
        recovered = self._glm_success_streak >= ZAI_COOL_SUCCESSES
        return {"hot": recent and not recovered,
                "last_429_at": last,
                "success_streak": self._glm_success_streak}

    # ---------- GLM-side telemetry (observer hooks) ----------
    def record_llm(self, provider, model, usage, duration_s) -> None:
        try:
            key = f"{provider}/{model}"
            u = usage or {}
            out_tok = u.get("output_tokens") or u.get("completion_tokens") or 0
            dur = float(duration_s or 0)
            row = self.state["glm"].setdefault(
                key, {"tps_ewma": None, "calls": 0, "tokens_out": 0})
            row["calls"] += 1
            row["tokens_out"] += out_tok
            if dur > 0 and out_tok:
                tps = out_tok / dur
                row["tps_ewma"] = (round(tps, 1) if row["tps_ewma"] is None
                                   else round(0.7 * row["tps_ewma"] + 0.3 * tps, 1))
            row["last_at"] = _iso(_now())
            if str(provider) == "zai" or str(model or "").startswith("glm"):
                self._glm_success_streak += 1
        except Exception:
            pass

    def record_error(self, provider, model, status) -> None:
        try:
            self.state["errors"].append({"ts": _iso(_now()), "provider": provider,
                                         "model": model, "status": status})
            self.state["errors"] = self.state["errors"][-200:]
            if "429" in str(status) and (
                    str(provider) in ("zai", "agy-orchestrator-mw")
                    or str(model or "").startswith("glm")):
                self._glm_success_streak = 0  # recovery clock restarts
                self.state["last_429_at"] = _iso(_now())
        except Exception:
            pass

    def note_classifier(self, via: str) -> None:
        self.state["classifier"].update({"last_via": via, "last_at": _iso(_now())})

    # ---------- snapshot ----------
    def snapshot(self) -> dict:
        with self.lock:
            self.state["updated_at"] = _iso(_now())
            self.state["routes"] = self.get_routes()
            for pool, p in self.state.get("pools", {}).items():
                try:
                    p.update(self._pool_derived(p, pool))
                    pr = self.pool_pressure(pool)
                    p["hot"] = pr.get("hot", False)
                    p["dry"] = bool(p.get("hot") and p.get("budget_calibrated"))
                except Exception:
                    pass
            tmp = STATE_FILE.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(self.state, indent=1))
                tmp.replace(STATE_FILE)
            except Exception as e:
                self.state["last_error"] = str(e)[:200]
            return self.state

    def tick(self) -> None:
        while True:
            try:
                now = _now()
                if now >= self._next_auth:
                    self.refresh_auth()
                    self._next_auth = now + AUTH_EVERY_S
                if now >= self._next_quota:
                    self.refresh_quota()
                    self._next_quota = now + QUOTA_EVERY_S
                if (self.state["auth"].get("needs_reauth")
                        and now >= self._next_reauth_attempt):
                    self.attempt_silent_reauth()
                    self._next_reauth_attempt = now + REAUTH_RETRY_S
                if now >= self._next_models:
                    cat = discover_models()
                    if not cat.get("error"):
                        self.state["models"] = cat
                    self._next_models = now + MODELS_EVERY_S
                self.ingest_runs()
                self.snapshot()
            except Exception as e:
                try:
                    self.state["last_error"] = str(e)[:200]
                except Exception:
                    pass
            time.sleep(INGEST_EVERY_S)

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        threading.Thread(target=self.tick, name="agy-orchestrator", daemon=True).start()
        threading.Thread(target=self._reap_dispatched, name="agy-orchestrator-reap",
                         daemon=True).start()


_meter: Meter | None = None
_meter_lock = threading.Lock()


def get_meter() -> Meter:
    global _meter
    with _meter_lock:
        if _meter is None:
            _meter = Meter()
        return _meter

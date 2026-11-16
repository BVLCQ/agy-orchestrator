"""Pytest suite for agy-orchestrator (Meter) and its route classifier.

Stdlib + pytest only. Module locations are resolved in this order:
  1. AGY_METER_DIR / AGY_CLASSIFIER_PATH environment variables (file paths)
  2. importable names (agy_meter, meter / classifier, agy_meter.classifier)
  3. AGY_METER_DIR (default ~/.hermes/plugins/agy-orchestrator): meter.py or
     __init__.py, and classifier.py
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# --------------------------------------------------------------------------
# Module loading
# --------------------------------------------------------------------------
PLUGIN_DIR = Path(os.environ.get("AGY_METER_DIR")
                  or Path.home() / ".hermes" / "plugins" / "agy-orchestrator")


def _load_from_file(name: str, path: Path):
    kwargs = {}
    if path.name == "__init__.py":
        kwargs["submodule_search_locations"] = [str(path.parent)]
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def _find_module(predicate, import_names, files, load_name):
    for name in import_names:
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if predicate(mod):
            return mod
    for f in files:
        if f.is_file():
            try:
                mod = _load_from_file(load_name, f)
            except Exception:
                continue
            if predicate(mod):
                return mod
    raise ImportError(f"could not locate module ({import_names} / {files}); "
                      "set AGY_METER_PATH / AGY_CLASSIFIER_PATH or AGY_METER_DIR")


def _is_meter(mod) -> bool:
    return hasattr(mod, "Meter") and hasattr(mod, "_pool_for")


def _is_classifier(mod) -> bool:
    return hasattr(mod, "_heuristic") and hasattr(mod, "ROUTES")


_meter_override = os.environ.get("AGY_METER_PATH")
_cls_override = os.environ.get("AGY_CLASSIFIER_PATH")

mm = _find_module(
    _is_meter,
    [] if _meter_override else ["agy_meter", "meter"],
    [Path(_meter_override)] if _meter_override
    else [PLUGIN_DIR / "meter.py", PLUGIN_DIR / "__init__.py"],
    "agy_meter_under_test",
)
cls = _find_module(
    _is_classifier,
    [] if _cls_override else ["classifier", "agy_meter.classifier"],
    [Path(_cls_override)] if _cls_override else [PLUGIN_DIR / "classifier.py"],
    "agy_classifier_under_test",
)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
class Clock:
    """Deterministic, integer-second clock (so _iso() round-trips exactly)."""

    def __init__(self) -> None:
        self.t = float(int(time.time()))

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def __call__(self) -> float:
        return self.t


class RunWriter:
    """Writes run files with strictly increasing mtimes (ingest uses an mtime cursor)."""

    def __init__(self, runs_dir: Path, clock: Clock) -> None:
        self.dir = runs_dir
        self.clock = clock
        self.last_mt = 0.0

    def _stamp(self, path: Path, mtime) -> float:
        if mtime is None:
            mtime = max(self.clock.t, self.last_mt + 1)
            self.last_mt = mtime
        os.utime(path, (mtime, mtime))
        return mtime

    def write(self, name: str, rec, mtime=None) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(rec))
        self._stamp(p, mtime)
        return p

    def write_raw(self, name: str, text: str, mtime=None) -> Path:
        p = self.dir / name
        p.write_text(text)
        self._stamp(p, mtime)
        return p


def make_rec(tin=0, tout=0, th=0, **extra) -> dict:
    d = {"usage": {"input_tokens": tin, "output_tokens": tout, "thinking_tokens": th}}
    d.update(extra)
    return d


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(mm, "_now", c)
    return c


@pytest.fixture
def env(monkeypatch, tmp_path, clock):
    cache = tmp_path / "cache"
    runs = cache / "runs"
    runs.mkdir(parents=True)
    settings = cache / "settings.json"
    state = cache / "state.json"
    models = cache / "models.json"
    guards = cache / "guards.json"
    monkeypatch.setattr(mm, "CACHE_DIR", cache)
    monkeypatch.setattr(mm, "RUNS_DIR", runs)
    monkeypatch.setattr(mm, "SETTINGS_FILE", settings)
    monkeypatch.setattr(mm, "STATE_FILE", state)
    monkeypatch.setattr(mm, "MODELS_FILE", models)
    monkeypatch.setattr(mm, "GUARDS_FILE", guards)
    return SimpleNamespace(cache=cache, runs_dir=runs, settings_file=settings,
                           state_file=state, models_file=models,
                           guards_file=guards, writer=RunWriter(runs, clock))


@pytest.fixture
def runs(env):
    return env.writer


@pytest.fixture
def m(env):
    return mm.Meter()


# --------------------------------------------------------------------------
# _pool_for attribution (incl. argon split)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("model,expected", [
    ("gemini-4-argon-high", "argon"),
    ("gemini-4-argon", "argon"),
    ("Gemini-4-Argon-High", "argon"),
    ("gemini-4-hydrogen-high", "gemini"),
    ("gemini-4-hydrogen-xhigh", "gemini"),
    ("gemini-3.8-flash-high", "gemini"),
    ("gemini-4-flash", "gemini"),
    ("gemini-4", "gemini"),
    ("claude-opus-5-5-medium", "third_party"),
    ("claude-sonnet-5-5-high", "third_party"),
    ("gpt-oss-120b", "third_party"),
    ("glm-5.3", "other"),
    ("mystery-model", "other"),
    ("", "other"),
    (None, "other"),
])
def test_pool_for_attribution(model, expected):
    assert mm._pool_for(model) == expected


def test_pool_for_argon_is_not_gemini_pool():
    assert mm._pool_for("gemini-4-argon-high") != mm._pool_for("gemini-3.8-flash-high")


# --------------------------------------------------------------------------
# ingest_runs: filename-slug fallback
# --------------------------------------------------------------------------
@pytest.mark.parametrize("fname,pool", [
    ("gemini-4-argon-high__task1.json", "argon"),
    ("gemini-3.8-flash-high__task1.json", "gemini"),
    ("claude-sonnet-5-5-high__task1.json", "third_party"),
    ("claude-opus-5-5-medium__task1.json", "third_party"),
    ("gpt-oss-120b__task1.json", "third_party"),
    ("mystery__task1.json", "other"),
    ("no-delimiter.json", "other"),
    ("gemini-3.8-flash-high__a__b.json", "gemini"),
])
def test_ingest_filename_slug_fallback(m, runs, fname, pool):
    runs.write(fname, make_rec(100, 50, 10))
    m.ingest_runs()
    p = m.state["pools"][pool]
    assert p["calls"] == 1
    assert (p["tokens_in"], p["tokens_out"], p["thinking_tokens"]) == (100, 50, 10)
    for other, op in m.state["pools"].items():
        if other != pool:
            assert op["calls"] == 0


def test_ingest_explicit_model_field_beats_slug(m, runs):
    runs.write("gemini-3.8-flash-high__x.json",
               make_rec(10, 5, 0, model="gemini-4-argon-high"))
    m.ingest_runs()
    assert m.state["pools"]["argon"]["calls"] == 1
    assert m.state["pools"]["gemini"]["calls"] == 0


def test_ingest_empty_model_field_falls_back_to_slug(m, runs):
    runs.write("claude-opus-5-5-medium__x.json", make_rec(10, 5, 0, model=""))
    m.ingest_runs()
    assert m.state["pools"]["third_party"]["calls"] == 1


def test_ingest_accumulates_tokens_lifetime_and_week(m, runs):
    runs.write("gemini-3.8-flash-high__a.json", make_rec(100, 50, 10))
    runs.write("gemini-3.8-flash-high__b.json", make_rec(1, 2, 3))
    m.ingest_runs()
    g = m.state["pools"]["gemini"]
    assert g["calls"] == 2
    assert g["tokens_in"] == 101 and g["tokens_out"] == 52 and g["thinking_tokens"] == 13
    assert g["lifetime_tokens"] == 166
    assert g["week_tokens"] == 166
    assert g["errors"] == 0


def test_ingest_tolerates_null_or_missing_usage(m, runs):
    runs.write("gemini-3.8-flash-high__a.json", {"usage": None})
    runs.write("gemini-3.8-flash-high__b.json", {"usage": {"input_tokens": None}})
    runs.write("gemini-3.8-flash-high__c.json", {})
    m.ingest_runs()
    g = m.state["pools"]["gemini"]
    assert g["calls"] == 3
    assert g["lifetime_tokens"] == 0


def test_ingest_cursor_prevents_double_counting(m, runs):
    runs.write("gemini-3.8-flash-high__a.json", make_rec(10, 10, 0))
    m.ingest_runs()
    m.ingest_runs()
    assert m.state["pools"]["gemini"]["calls"] == 1
    runs.write("gemini-3.8-flash-high__b.json", make_rec(10, 10, 0))
    m.ingest_runs()
    assert m.state["pools"]["gemini"]["calls"] == 2


@pytest.mark.parametrize("status", ["SUCCESS", "success", "OK", "ok"])
def test_ingest_success_statuses_are_not_errors(m, runs, status):
    runs.write("gemini-3.8-flash-high__a.json", make_rec(1, 1, 0, status=status))
    m.ingest_runs()
    assert m.state["pools"]["gemini"]["errors"] == 0
    assert m.state["errors"] == []


def test_ingest_missing_status_defaults_to_success(m, runs):
    runs.write("gemini-3.8-flash-high__a.json", make_rec(1, 1, 0))
    m.ingest_runs()
    assert m.state["pools"]["gemini"]["errors"] == 0


@pytest.mark.parametrize("error_text,expected_status", [
    ("429 Too Many Requests", "429"),
    ("Quota exceeded for this window", "429"),
    ("RESOURCE_EXHAUSTED: quota", "429"),
    ("model overloaded", "run:FAILED"),
    ("", "run:FAILED"),
])
def test_ingest_error_status_recorded(m, runs, error_text, expected_status):
    runs.write("gemini-3.8-flash-high__a.json",
               make_rec(1, 1, 0, status="failed", error=error_text))
    m.ingest_runs()
    assert m.state["pools"]["gemini"]["errors"] == 1
    last = m.state["errors"][-1]
    assert last["provider"] == "agy"
    assert last["model"] == "gemini-3.8-flash-high"
    assert last["status"] == expected_status
    if expected_status == "429":
        assert m.state["last_429_at"] is not None
    else:
        assert m.state["last_429_at"] is None


def test_ingest_429_uses_record_ts_for_last_429(m, runs):
    runs.write("gemini-3.8-flash-high__a.json",
               make_rec(1, 1, 0, status="ERROR", error="429",
                        ts="2026-11-05T10:00:00+00:00"))
    m.ingest_runs()
    assert m.state["last_429_at"] == "2026-11-05T10:00:00+00:00"


def test_ingest_429_never_heats_zai(m, runs):
    runs.write("gemini-3.8-flash-high__a.json",
               make_rec(1, 1, 0, status="ERROR", error="429 quota"))
    m.ingest_runs()
    assert m.zai_pressure()["hot"] is False


def test_ingest_429_parses_google_reset_horizon(m, runs):
    runs.write("gemini-4-argon-high__a.json",
               make_rec(1, 1, 0, status="ERROR",
                        error="Individual quota reached. Resets in 4h 12m"))
    runs.write("claude-opus-5-5-high__b.json",
               make_rec(1, 1, 0, status="ERROR",
                        error="RESOURCE_EXHAUSTED: quota. Resets in 21h"))
    m.ingest_runs()
    assert m.state["pools"]["argon"]["quota_reset_min"] == 252
    assert m.state["pools"]["third_party"]["quota_reset_min"] == 21 * 60
    # non-429 runs never set it
    runs.write("gemini-3.8-flash-high__c.json", make_rec(1, 1, 0))
    m.ingest_runs()
    assert "quota_reset_min" not in m.state["pools"]["gemini"]


def test_ingest_deletes_runs_older_than_seven_days(m, runs, clock):
    old = runs.write("gemini-3.8-flash-high__old.json", make_rec(1, 1, 0),
                     mtime=clock.t - 8 * 86400)
    fresh = runs.write("gemini-3.8-flash-high__new.json", make_rec(1, 1, 0))
    m.ingest_runs()
    assert not old.exists()
    assert fresh.exists()


# --------------------------------------------------------------------------
# ingest_runs: unparseable corpse counting
# --------------------------------------------------------------------------
def test_corpse_counted_against_slug_pool_as_error(m, runs):
    runs.write_raw("claude-opus-5-5-medium__t1.json", '{"usage": {"input_tok')
    m.ingest_runs()
    tp = m.state["pools"]["third_party"]
    assert tp["errors"] == 1
    assert tp["calls"] == 0
    assert tp["lifetime_tokens"] == 0
    # corpses do not register in the router's error view
    assert m.state["errors"] == []


def test_corpse_argon_slug_counts_against_argon_not_gemini(m, runs):
    runs.write_raw("gemini-4-argon-high__t1.json", "{not json")
    m.ingest_runs()
    assert m.state["pools"]["argon"]["errors"] == 1
    assert m.state["pools"]["gemini"]["errors"] == 0


def test_corpse_empty_file_is_skipped_not_errored(m, runs):
    # Contract change (v0.3.1, review finding #1): dispatch() creates output
    # files empty at spawn, so ingest must NEVER count an empty file — that
    # fabricated one phantom error per >45s run. Runs that die without output
    # are accounted exactly once by the dispatch reaper instead.
    runs.write_raw("gemini-3.8-flash-high__t1.json", "")
    m.ingest_runs()
    assert m.state["pools"]["gemini"]["errors"] == 0
    assert m.state["slugs"] == {}


def test_corpse_without_slug_delimiter_is_not_counted(m, runs):
    runs.write_raw("garbage.json", "{broken")
    m.ingest_runs()
    assert all(p["errors"] == 0 for p in m.state["pools"].values())


def test_corpse_counted_once_and_advances_cursor(m, runs, env):
    path = runs.write_raw("claude-sonnet-5-5-high__t1.json", "{broken")
    mt = path.stat().st_mtime
    m.ingest_runs()
    assert m._cursor >= mt
    m.ingest_runs()
    assert m.state["pools"]["third_party"]["errors"] == 1


def test_corpse_alongside_good_records(m, runs):
    runs.write("gemini-3.8-flash-high__ok.json", make_rec(10, 10, 0))
    runs.write_raw("gemini-3.8-flash-high__bad.json", "{")
    m.ingest_runs()
    g = m.state["pools"]["gemini"]
    assert g["calls"] == 1
    assert g["errors"] == 1
    assert g["lifetime_tokens"] == 20


# --------------------------------------------------------------------------
# 5h window reset preserving week counters / 7d week reset
# --------------------------------------------------------------------------
def test_window_not_reset_within_five_hours(m, runs, clock):
    runs.write("gemini-3.8-flash-high__a.json", make_rec(100, 50, 10))
    m.ingest_runs()
    clock.advance(4 * 3600)
    runs.write("gemini-3.8-flash-high__b.json", make_rec(10, 5, 1))
    m.ingest_runs()
    g = m.state["pools"]["gemini"]
    assert g["calls"] == 2
    assert g["tokens_in"] == 110


def test_window_reset_after_five_hours_preserves_week_counters(m, runs, clock):
    runs.write("gemini-3.8-flash-high__a.json", make_rec(100, 50, 10, status="FAILED"))
    m.ingest_runs()
    g = m.state["pools"]["gemini"]
    assert g["calls"] == 1 and g["errors"] == 1
    week_start = g["week_start"]

    clock.advance(mm.WINDOW_H * 3600 + 60)
    runs.write("gemini-3.8-flash-high__b.json", make_rec(10, 5, 1))
    m.ingest_runs()

    # window counters restarted
    assert g["window_start"] == mm._iso(clock.t)
    assert g["tokens_in"] == 10 and g["tokens_out"] == 5 and g["thinking_tokens"] == 1
    assert g["calls"] == 1
    assert g["errors"] == 0
    # week counters preserved and accumulating
    assert g["week_start"] == week_start
    assert g["week_tokens"] == 160 + 16
    assert g["lifetime_tokens"] == 160 + 16


def test_window_reset_is_per_pool(m, runs, clock):
    runs.write("gemini-3.8-flash-high__a.json", make_rec(100, 0, 0))
    m.ingest_runs()
    clock.advance(mm.WINDOW_H * 3600 + 60)
    runs.write("claude-opus-5-5-medium__b.json", make_rec(7, 0, 0))
    m.ingest_runs()
    # gemini pool untouched (no new gemini run): still holds its old counters
    assert m.state["pools"]["gemini"]["tokens_in"] == 100
    assert m.state["pools"]["third_party"]["tokens_in"] == 7


def test_week_not_reset_before_seven_days(m, runs, clock):
    runs.write("gemini-3.8-flash-high__a.json", make_rec(100, 0, 0))
    m.ingest_runs()
    g = m.state["pools"]["gemini"]
    week_start = g["week_start"]
    clock.advance(6 * 86400)
    runs.write("gemini-3.8-flash-high__b.json", make_rec(5, 0, 0))
    m.ingest_runs()
    assert g["week_start"] == week_start
    assert g["week_tokens"] == 105
    assert g["tokens_in"] == 5  # 5h window did reset


def test_week_reset_after_seven_days(m, runs, clock):
    runs.write("gemini-3.8-flash-high__a.json", make_rec(100, 50, 10))
    m.ingest_runs()
    g = m.state["pools"]["gemini"]
    assert g["week_tokens"] == 160

    clock.advance(7 * 86400 + 3600)
    runs.write("gemini-3.8-flash-high__b.json", make_rec(10, 5, 1))
    m.ingest_runs()

    assert g["week_start"] == mm._iso(clock.t)
    assert g["week_tokens"] == 16
    assert g["window_start"] == mm._iso(clock.t)
    assert g["tokens_in"] == 10
    # lifetime never resets
    assert g["lifetime_tokens"] == 176


def test_malformed_window_start_resets_pool_to_fresh(m, runs):
    g = m.state["pools"]["gemini"]
    g["window_start"] = "garbage"
    g["lifetime_tokens"] = 999
    runs.write("gemini-3.8-flash-high__a.json", make_rec(10, 0, 0))
    m.ingest_runs()
    assert g["lifetime_tokens"] == 10
    assert g["calls"] == 1


# --------------------------------------------------------------------------
# _calibrate_budget
# --------------------------------------------------------------------------
def test_calibrate_sets_budget_from_consumed_with_headroom(m, env):
    p = {"tokens_in": 1000, "tokens_out": 500, "thinking_tokens": 500}
    m._calibrate_budget("argon", p)
    assert m.settings["budgets"]["argon"] == int(2000 * 1.05)
    assert m.settings["budgets"]["argon_calibrated"] is True
    persisted = json.loads(env.settings_file.read_text())
    assert persisted["budgets"]["argon"] == 2100
    assert persisted["budgets"]["argon_calibrated"] is True


def test_calibrate_replaces_explicit_estimate(m):
    m.settings["budgets"] = {"gemini": 99_999_999}
    m._calibrate_budget("gemini", {"tokens_in": 1000, "tokens_out": 500,
                                   "thinking_tokens": 500})
    assert m.settings["budgets"]["gemini"] == 2100


def test_calibrate_replaces_default_estimate(m):
    derived_before = m._pool_derived({"tokens_in": 1000, "tokens_out": 500,
                                      "thinking_tokens": 500,
                                      "window_start": mm._iso(mm._now())}, "argon")
    assert derived_before["budget_tokens"] == mm.DEFAULT_BUDGETS["argon"]
    assert derived_before["budget_calibrated"] is False

    p = {"tokens_in": 1000, "tokens_out": 500, "thinking_tokens": 500,
         "window_start": mm._iso(mm._now())}
    m._calibrate_budget("argon", p)
    derived = m._pool_derived(p, "argon")
    assert derived["budget_tokens"] == 2100
    assert derived["budget_calibrated"] is True
    assert derived["pct_used"] == round(100.0 * 2000 / 2100, 1)


def test_calibrate_only_first_observation_counts(m):
    m._calibrate_budget("argon", {"tokens_in": 1000})
    first = m.settings["budgets"]["argon"]
    m._calibrate_budget("argon", {"tokens_in": 9_000_000})
    assert m.settings["budgets"]["argon"] == first


def test_calibrate_is_per_pool(m):
    m._calibrate_budget("argon", {"tokens_in": 1000})
    assert "gemini_calibrated" not in m.settings["budgets"]
    m._calibrate_budget("gemini", {"tokens_in": 2000})
    assert m.settings["budgets"]["gemini"] == 2100
    assert m.settings["budgets"]["argon"] == 1050


def test_calibrate_falls_back_to_lifetime_then_one(m):
    m._calibrate_budget("argon", {"lifetime_tokens": 400})
    assert m.settings["budgets"]["argon"] == int(400 * 1.05)
    m._calibrate_budget("gemini", {})
    assert m.settings["budgets"]["gemini"] == int(1 * 1.05)


def test_calibrate_persists_across_restart(m, env):
    m._calibrate_budget("argon", {"tokens_in": 1000, "tokens_out": 500,
                                  "thinking_tokens": 500})
    m2 = mm.Meter()
    assert m2.settings["budgets"]["argon"] == 2100
    assert m2.settings["budgets"]["argon_calibrated"] is True


def test_ingest_first_429_calibrates_and_snapshot_marks_dry(m, runs, env):
    runs.write("gemini-4-argon-high__a.json", make_rec(1000, 500, 500))
    m.ingest_runs()
    assert "argon_calibrated" not in (m.settings.get("budgets") or {})

    runs.write("gemini-4-argon-high__b.json",
               make_rec(0, 0, 0, status="ERROR", error="429 RESOURCE_EXHAUSTED"))
    m.ingest_runs()
    assert m.settings["budgets"]["argon"] == 2100
    assert m.settings["budgets"]["argon_calibrated"] is True

    # a later 429 with more consumption does not re-calibrate
    runs.write("gemini-4-argon-high__c.json",
               make_rec(5000, 0, 0, status="ERROR", error="429"))
    m.ingest_runs()
    assert m.settings["budgets"]["argon"] == 2100

    snap = m.snapshot()
    argon = snap["pools"]["argon"]
    assert argon["budget_calibrated"] is True
    assert argon["budget_tokens"] == 2100
    assert argon["hot"] is True
    assert argon["dry"] is True
    assert snap["pools"]["gemini"]["dry"] is False
    on_disk = json.loads(env.state_file.read_text())
    assert on_disk["pools"]["argon"]["dry"] is True


def test_uncalibrated_hot_pool_is_not_dry(m):
    m.record_error("agy", "gemini-3.8-flash-high", "429")
    snap = m.snapshot()
    assert snap["pools"]["gemini"]["hot"] is True
    assert snap["pools"]["gemini"]["budget_calibrated"] is False
    assert snap["pools"]["gemini"]["dry"] is False


# --------------------------------------------------------------------------
# zai_pressure: evidence-based cooling
# --------------------------------------------------------------------------
def _zai_429(m):
    m.record_error("zai", "glm-5.3", "429")


def _glm_ok(m, n=1):
    for _ in range(n):
        m.record_llm("zai", "glm-5.3", {"output_tokens": 20}, 2.0)


def test_zai_not_hot_without_429(m):
    assert m.zai_pressure() == {"hot": False, "last_429_at": None}
    _glm_ok(m, 5)
    assert m.zai_pressure() == {"hot": False, "last_429_at": None}


def test_zai_hot_after_429(m, clock):
    _zai_429(m)
    zp = m.zai_pressure()
    assert zp["hot"] is True
    assert zp["last_429_at"] == mm._iso(clock.t)
    assert zp["success_streak"] == 0


def test_zai_three_glm_successes_clear_hot(m, clock):
    _zai_429(m)
    _glm_ok(m, mm.ZAI_COOL_SUCCESSES - 1)
    assert m.zai_pressure()["hot"] is True
    _glm_ok(m, 1)
    zp = m.zai_pressure()
    assert zp["hot"] is False
    assert zp["success_streak"] == mm.ZAI_COOL_SUCCESSES
    assert zp["last_429_at"] == mm._iso(clock.t)  # still reported as evidence


def test_zai_new_429_reheats_after_cooling(m, clock):
    _zai_429(m)
    _glm_ok(m, 3)
    assert m.zai_pressure()["hot"] is False
    clock.advance(10)
    _zai_429(m)
    zp = m.zai_pressure()
    assert zp["hot"] is True
    assert zp["success_streak"] == 0
    assert zp["last_429_at"] == mm._iso(clock.t)


def test_zai_idle_backstop_cools_without_successes(m, clock):
    _zai_429(m)
    clock.advance(mm.ZAI_429_BACKSTOP_S - 50)
    assert m.zai_pressure()["hot"] is True
    clock.advance(100)  # now past the 5-minute backstop
    zp = m.zai_pressure()
    assert zp["hot"] is False
    assert zp["success_streak"] == 0


def test_zai_backstop_constants_sane():
    assert mm.ZAI_429_BACKSTOP_S == 300
    assert mm.ZAI_COOL_SUCCESSES == 3


@pytest.mark.parametrize("provider,model", [
    ("zai", "anything"),
    ("agy-orchestrator-mw", "whatever"),
    ("openrouter", "glm-5.3-flash"),
])
def test_zai_heating_providers_and_models(m, provider, model):
    m.record_error(provider, model, "429")
    assert m.zai_pressure()["hot"] is True


@pytest.mark.parametrize("provider,model", [
    ("agy", "gemini-3.8-flash-high"),
    ("agy", "gemini-4-argon-high"),
    ("anthropic", "claude-opus-5-5-medium"),
])
def test_zai_ignores_other_provider_429(m, provider, model):
    m.record_error(provider, model, "429")
    assert m.zai_pressure()["hot"] is False


def test_zai_ignores_non_429_errors(m):
    m.record_error("zai", "glm-5.3", "500")
    m.record_error("zai", "glm-5.3", "run:FAILED")
    assert m.zai_pressure()["hot"] is False


def test_record_error_glm_429_resets_streak_and_sets_last_429(m, clock):
    _glm_ok(m, 2)
    assert m._glm_success_streak == 2
    _zai_429(m)
    assert m._glm_success_streak == 0
    assert m.state["last_429_at"] == mm._iso(clock.t)


def test_record_error_agy_429_does_not_reset_glm_streak(m):
    _glm_ok(m, 2)
    m.record_error("agy", "gemini-3.8-flash-high", "429")
    assert m._glm_success_streak == 2


def test_record_llm_only_glm_successes_count_toward_streak(m):
    m.record_llm("anthropic", "claude-opus-5-5-medium", {"output_tokens": 5}, 1.0)
    assert m._glm_success_streak == 0
    m.record_llm("openrouter", "glm-5.3-flash", {"output_tokens": 5}, 1.0)
    assert m._glm_success_streak == 1
    m.record_llm("zai", "other-model", {"output_tokens": 5}, 1.0)
    assert m._glm_success_streak == 2


def test_record_llm_tps_ewma(m):
    m.record_llm("zai", "glm-5.3", {"output_tokens": 100}, 2.0)   # 50 t/s
    assert m.state["glm"]["zai/glm-5.3"]["tps_ewma"] == 50.0
    m.record_llm("zai", "glm-5.3", {"completion_tokens": 100}, 1.0)  # 100 t/s
    assert m.state["glm"]["zai/glm-5.3"]["tps_ewma"] == round(0.7 * 50 + 0.3 * 100, 1)
    assert m.state["glm"]["zai/glm-5.3"]["calls"] == 2


# --------------------------------------------------------------------------
# pool_pressure hot legs
# --------------------------------------------------------------------------
def test_pool_pressure_unknown_pool_is_cold(m):
    assert m.pool_pressure("nonexistent") == {
        "pool": "nonexistent", "pct_used": None, "recent_429": False, "hot": False}


def test_pool_pressure_cold_by_default(m):
    for pool in ("gemini", "argon", "third_party", "other"):
        pr = m.pool_pressure(pool)
        assert pr["hot"] is False and pr["recent_429"] is False


def test_pool_pressure_hot_from_pct_leg(m):
    m.state["pools"]["gemini"]["pct_used"] = 80.0
    pr = m.pool_pressure("gemini")
    assert pr["hot"] is True
    assert pr["recent_429"] is False
    assert pr["pct_used"] == 80.0


def test_pool_pressure_below_threshold_not_hot(m):
    m.state["pools"]["gemini"]["pct_used"] = 79.9
    assert m.pool_pressure("gemini")["hot"] is False


def test_pool_pressure_hot_from_recent_429_leg(m):
    m.record_error("agy", "gemini-3.8-flash-high", "429")
    pr = m.pool_pressure("gemini")
    assert pr["recent_429"] is True
    assert pr["hot"] is True
    assert m.pool_pressure("argon")["hot"] is False
    assert m.pool_pressure("third_party")["hot"] is False


def test_pool_pressure_argon_split_on_429(m):
    m.record_error("agy", "gemini-4-argon-high", "429")
    assert m.pool_pressure("argon")["hot"] is True
    assert m.pool_pressure("gemini")["hot"] is False


def test_pool_pressure_third_party_429(m):
    m.record_error("agy", "claude-sonnet-5-5-high", "429")
    assert m.pool_pressure("third_party")["hot"] is True


def test_pool_pressure_429_leg_expires_after_30_minutes(m, clock):
    m.record_error("agy", "gemini-3.8-flash-high", "429")
    clock.advance(1700)
    assert m.pool_pressure("gemini")["hot"] is True
    clock.advance(101)  # 1801s
    assert m.pool_pressure("gemini")["hot"] is False


def test_pool_pressure_pct_leg_independent_of_expired_429(m, clock):
    m.record_error("agy", "gemini-3.8-flash-high", "429")
    clock.advance(1801)
    m.state["pools"]["gemini"]["pct_used"] = 95.0
    pr = m.pool_pressure("gemini")
    assert pr["recent_429"] is False
    assert pr["hot"] is True


def test_pool_pressure_non_429_errors_do_not_heat(m):
    m.record_error("agy", "gemini-3.8-flash-high", "run:FAILED")
    m.record_error("agy", "gemini-3.8-flash-high", "500")
    assert m.pool_pressure("gemini")["hot"] is False


def test_pool_pressure_substring_429_status_matches(m):
    m.record_error("agy", "gemini-3.8-flash-high", "HTTP 429")
    assert m.pool_pressure("gemini")["hot"] is True


def test_pool_pressure_only_scans_last_50_errors(m):
    m.record_error("agy", "gemini-3.8-flash-high", "429")
    for _ in range(50):
        m.record_error("agy", "claude-opus-5-5-medium", "run:FAILED")
    assert m.pool_pressure("gemini")["recent_429"] is False


def test_pool_pressure_skips_malformed_error_entries(m):
    m.state["errors"].append({"model": "gemini-3.8-flash-high", "status": "429",
                              "ts": "garbage"})
    m.state["errors"].append({"model": "gemini-3.8-flash-high", "status": "429"})
    assert m.pool_pressure("gemini")["hot"] is False


def test_pool_pressure_end_to_end_through_ingest(m, runs):
    runs.write("gemini-4-argon-high__a.json",
               make_rec(1, 1, 0, status="ERROR", error="429 quota"))
    m.ingest_runs()
    assert m.pool_pressure("argon")["hot"] is True
    assert m.pool_pressure("gemini")["hot"] is False


# --------------------------------------------------------------------------
# classifier: routes (ADR-002)
# --------------------------------------------------------------------------
def test_routes_are_the_three_archetypes():
    assert list(cls.ROUTES) == ["hermes_delegate", "plan", "parallel"]


def test_delegate_route_is_fixed_zai():
    assert cls.ROUTES["hermes_delegate"]["pool"] == "zai"
    assert cls.ROUTES["hermes_delegate"]["slug"] == "delegate_task/glm-5.3-flash"


def test_plan_and_parallel_have_no_hardcoded_model():
    for r in ("plan", "parallel"):
        assert "slug" not in cls.ROUTES[r]
        assert "pool" not in cls.ROUTES[r]


@pytest.mark.parametrize("phrase", [
    "we discussed",
    "the bug",
    "review the diff",
    "the error",
    "our repo",
    "from earlier",
    "the conversation",
    "uncommitted",
    "this session",
])
def test_heuristic_context_bound_phrases(phrase):
    assert cls._heuristic(f"Please handle {phrase} for me.") == "hermes_delegate"


def test_heuristic_context_bound_is_case_insensitive():
    assert cls._heuristic("Look at THE ERROR from The Conversation") == "hermes_delegate"


def test_heuristic_context_bound_takes_precedence_over_complex():
    chunk = "Refactor and architect the module we discussed, implement a function"
    assert cls._heuristic(chunk) == "hermes_delegate"


def test_heuristic_coding_and_complex_goes_to_plan():
    # planning over complex code is PLAN work now; the model (opus vs gemini)
    # is the owner's selection, not the classifier's business
    assert cls._heuristic(
        "Evaluate the architecture and implement a migration function") == "plan"


def test_heuristic_complex_only_goes_to_plan():
    assert cls._heuristic(
        "Compare the two vendors and decide which is better") == "plan"


def test_heuristic_coding_only_goes_to_parallel():
    assert cls._heuristic("Implement a function that parses CSV") == "parallel"


def test_heuristic_default_is_parallel():
    assert cls._heuristic("Translate the following paragraph into Spanish.") == "parallel"
    assert cls._heuristic("") == "parallel"


def test_heuristic_result_is_always_a_known_route():
    for chunk in ("", "hello", "refactor", "analyze", "from earlier", "class Foo test"):
        assert cls._heuristic(chunk) in cls.ROUTES


def test_route_from_choice_accepts_key_index_and_desc_prefix():
    assert cls._route_from_choice("plan") == "plan"
    assert cls._route_from_choice("1") == "plan"      # _ROUTE_KEYS order
    assert cls._route_from_choice("2") == "parallel"
    with pytest.raises(ValueError):
        cls._route_from_choice("garbage route name")


def test_classify_default_is_heuristic_without_network(monkeypatch):
    # Owner ruling 2026-11-15: heuristic IS the classifier; the probabilistic
    # chain must not even be attempted unless {"classifier": "kev"} is set.
    def boom(chunk, settings):
        raise AssertionError("ollama must not be called by default")
    monkeypatch.setattr(cls, "_via_ollama", boom)
    res = cls.classify("Evaluate the architecture and implement a migration")
    assert res == {"route": "plan", "probabilities": None,
                   "confidence": None, "via": "heuristic", "errors": []}


def test_classify_falls_back_to_heuristic_when_backends_fail(monkeypatch):
    def boom_ollama(chunk, settings):
        raise RuntimeError("ollama down")

    def boom_lms(chunk, settings):
        raise RuntimeError("lmstudio down")

    monkeypatch.setattr(cls, "_via_ollama", boom_ollama)
    monkeypatch.setattr(cls, "_via_lmstudio", boom_lms)
    res = cls.classify("Review the diff we discussed", {"classifier": "kev"})
    assert res["route"] == "hermes_delegate"
    assert res["via"] == "heuristic"
    assert res["probabilities"] is None and res["confidence"] is None
    assert len(res["errors"]) == 2
    assert res["errors"][0].startswith("ollama:")
    assert res["errors"][1].startswith("lmstudio:")


# --------------------------------------------------------------------------
# model catalog discovery (`agy models` -> models.json)
# --------------------------------------------------------------------------
def test_split_effort_variants():
    assert mm._split_effort("claude-opus-5-5-high") == ("claude-opus-5-5", "high")
    assert mm._split_effort("gemini-4-hydrogen-xhigh") == ("gemini-4-hydrogen", "xhigh")
    assert mm._split_effort("gemini-3.8-flash-medium") == ("gemini-3.8-flash", "medium")
    assert mm._split_effort("gpt-oss-120b-medium") == ("gpt-oss-120b", "medium")
    assert mm._split_effort(
        "gemini-pro-orchestrator-g1-pro") == ("gemini-pro-orchestrator-g1-pro", None)


def test_parse_models_groups_by_base_with_ordered_efforts():
    text = ("Fetching available models...\n"
            "gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)\n"
            "gemini-4-hydrogen-xhigh\tGemini 4 Hydrogen (XHigh)\n"
            "gemini-4-hydrogen-low\tGemini 4 Hydrogen (Low)\n"
            "claude-opus-5-5-high\tClaude Opus 5.5 (High)\n")
    cat = mm.parse_models_output(text)
    hyd = next(b for b in cat["bases"] if b["base"] == "gemini-4-hydrogen")
    assert [v["effort"] for v in hyd["variants"]] == ["low", "high", "xhigh"]
    assert hyd["pool"] == "gemini"
    assert hyd["label"] == "Gemini 4 Hydrogen (High)"
    assert set(cat["slugs"]) == {"gemini-4-hydrogen-high", "gemini-4-hydrogen-xhigh",
                                 "gemini-4-hydrogen-low", "claude-opus-5-5-high"}


def test_parse_models_skips_non_tsv_noise():
    assert mm.parse_models_output("Fetching available models...\nno-tabs\n") == {
        "bases": [], "slugs": []}


def test_parse_models_single_variant_slug_without_effort():
    cat = mm.parse_models_output("gemini-pro-orchestrator-g1-pro\tOrchestrator (Argon)\n")
    b = cat["bases"][0]
    assert b["variants"] == [{"effort": None, "slug": "gemini-pro-orchestrator-g1-pro"}]


def test_discover_models_writes_catalog(monkeypatch, env, clock):
    monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="claude-opus-5-5-high\tClaude Opus 5.5 (High)\n"))
    cat = mm.discover_models()
    assert cat["slugs"] == ["claude-opus-5-5-high"]
    assert cat["fetched_at"] == mm._iso(clock.t)
    assert json.loads(env.models_file.read_text())["slugs"] == ["claude-opus-5-5-high"]


def test_discover_models_error_does_not_raise_or_clobber(monkeypatch, env):
    good = mm.parse_models_output("claude-opus-5-5-high\tClaude Opus 5.5 (High)\n")
    good["fetched_at"] = "2026-11-14T00:00:00+00:00"
    env.models_file.write_text(json.dumps(good))

    def boom(*a, **k):
        raise RuntimeError("agy gone")

    monkeypatch.setattr(mm.subprocess, "run", boom)
    cat = mm.discover_models()
    assert cat.get("error")
    assert json.loads(env.models_file.read_text())["slugs"] == ["claude-opus-5-5-high"]


# --------------------------------------------------------------------------
# route config (owner selection; ADR-002)
# --------------------------------------------------------------------------
def test_route_defaults_are_the_owner_ruling():
    assert mm.ROUTE_DEFAULTS == {
        "plan": ["claude-opus-5-5-high", "gemini-4-hydrogen-high"],
        "parallel": ["claude-sonnet-5-5-high", "gemini-3.8-flash-high"],
    }


def test_get_routes_defaults_when_unset(m):
    assert m.get_routes() == {k: list(v) for k, v in mm.ROUTE_DEFAULTS.items()}


def test_get_routes_reloads_external_writes(m, env):
    assert m.get_routes()["plan"] == mm.ROUTE_DEFAULTS["plan"]
    env.settings_file.write_text(json.dumps({
        "routes": {"plan": ["gemini-4-hydrogen-xhigh"],
                   "parallel": ["claude-sonnet-5-5-high"]}}))
    routes = m.get_routes()
    assert routes["plan"] == ["gemini-4-hydrogen-xhigh"]
    assert routes["parallel"] == ["claude-sonnet-5-5-high"]


def test_get_routes_invalid_entries_fall_back_to_defaults(m):
    m.settings["routes"] = {"plan": "not-a-list", "parallel": [1, "", None]}
    assert m.get_routes() == {k: list(v) for k, v in mm.ROUTE_DEFAULTS.items()}


def test_save_routes_persists_merges_and_visible_to_meter(m, env):
    m._calibrate_budget("argon", {"tokens_in": 1000})  # budgets already on disk
    saved = mm.save_routes({
        "plan": ["claude-opus-5-5-high", "claude-opus-5-5-high",
                 "gemini-4-hydrogen-high"],
        "parallel": ["gemini-3.8-flash-high"]})
    assert saved["plan"] == ["claude-opus-5-5-high", "gemini-4-hydrogen-high"]  # deduped
    on_disk = json.loads(env.settings_file.read_text())
    assert on_disk["routes"] == saved
    assert on_disk["budgets"]["argon"]  # budgets untouched by the picker
    assert m.get_routes() == saved      # running meter sees external write


def test_save_routes_rejects_unknown_route():
    with pytest.raises(ValueError):
        mm.save_routes({"plan": ["claude-opus-5-5-high"], "nope": ["x"]})


@pytest.mark.parametrize("bad", [
    {"plan": [], "parallel": ["x"]},
    {"plan": "claude-opus-5-5-high", "parallel": ["x"]},
    {"plan": ["claude-opus-5-5-high", 42], "parallel": ["x"]},
    "not even a dict",
])
def test_save_routes_rejects_bad_shapes(bad):
    with pytest.raises(ValueError):
        mm.save_routes(bad)


def test_save_routes_validates_against_catalog(env):
    cat = mm.parse_models_output("claude-opus-5-5-high\tClaude Opus 5.5 (High)\n")
    env.models_file.write_text(json.dumps(cat))
    with pytest.raises(ValueError, match="not in catalog"):
        mm.save_routes({"plan": ["claude-opus-5-6-high"],  # future model, undiscovered
                        "parallel": ["claude-opus-5-5-high"]})


def test_save_routes_without_catalog_accepts_any_slug(env):
    # discovery never ran (no models.json): the owner is never blocked
    saved = mm.save_routes({"plan": ["brand-new-model-high"],
                            "parallel": ["claude-sonnet-5-5-high"]})
    assert saved["plan"] == ["brand-new-model-high"]


def test_snapshot_echoes_routes(m):
    snap = m.snapshot()
    assert snap["routes"] == {k: list(v) for k, v in mm.ROUTE_DEFAULTS.items()}


# --------------------------------------------------------------------------
# resolve_model: only selected models, pressure walks the selection
# --------------------------------------------------------------------------
def test_resolve_primary_when_cool(m):
    res = m.resolve_model("plan")
    assert res == {"model": "claude-opus-5-5-high", "pool": "third_party"}


def test_resolve_hot_primary_falls_to_next_selection(m):
    m.state["pools"]["third_party"]["pct_used"] = 90.0
    res = m.resolve_model("plan")
    assert res["model"] == "gemini-4-hydrogen-high"
    assert res["pool"] == "gemini"
    assert res["routed_from"] == "claude-opus-5-5-high"
    assert "hot" in res["reason"]


def test_resolve_all_selections_hot_returns_primary_with_warning(m):
    m.state["pools"]["third_party"]["pct_used"] = 90.0
    m.state["pools"]["gemini"]["pct_used"] = 90.0
    res = m.resolve_model("plan")
    assert res["model"] == "claude-opus-5-5-high"  # never an unselected substitute
    assert "pool_warning" in res


def test_resolve_under_pressure_stays_within_selection(m):
    for pool in ("third_party", "gemini", "argon"):
        m.state["pools"][pool]["pct_used"] = 90.0
    for route in ("plan", "parallel"):
        res = m.resolve_model(route)
        assert res["model"] in mm.ROUTE_DEFAULTS[route]


def test_resolve_delegate_is_fixed():
    m = mm.Meter()
    assert m.resolve_model("hermes_delegate") == {
        "model": "delegate_task/glm-5.3-flash", "pool": "zai"}


def test_resolve_empty_selection_warns(m):
    res = m.resolve_model("plan", {"plan": [], "parallel": ["x"]})
    assert res["model"] is None
    assert "no models selected" in res["pool_warning"]


def test_resolve_selection_order_is_owner_order(m):
    res = m.resolve_model("parallel",
                          {"plan": ["a"], "parallel": ["gpt-oss-120b-medium"]})
    assert res == {"model": "gpt-oss-120b-medium", "pool": "third_party"}


# --------------------------------------------------------------------------
# dispatch hygiene: env scrub (pattern: soyelmismo DirectSDK provider)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cred", [
    "ZAI_API_KEY", "TELEGRAM_BOT_TOKEN", "SLACK_SECRET", "MY_PASSWORD",
    "DATABASE_URL", "REDIS_URL", "MONGO_URI", "GITHUB_AUTH_COOKIE",
    "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "PRIVATE_KEY_STUFF", "WEBHOOK_URL2",
])
def test_scrubbed_env_strips_credentials(monkeypatch, cred):
    monkeypatch.setenv(cred, "leak")
    assert cred not in mm.scrubbed_env(), cred


def test_scrubbed_env_strips_lowercase_credentials(monkeypatch):
    monkeypatch.setenv("zai_api_key", "leak")
    assert "zai_api_key" not in mm.scrubbed_env()


def test_scrubbed_env_keeps_basics_and_passthrough(monkeypatch):
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("HOME", "/Users/x")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/sock")
    monkeypatch.setenv("http_proxy", "http://p:1")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "s3cret")
    env = mm.scrubbed_env(passthrough=("MY_SERVICE_TOKEN",))
    assert env["PATH"] == "/bin" and env["HOME"] == "/Users/x"
    assert env["SSH_AUTH_SOCK"] == "/sock"
    assert env["http_proxy"] == "http://p:1"   # keep-list matches upper-insensitive
    assert env["LC_ALL"] == "en_US.UTF-8"
    assert env["MY_SERVICE_TOKEN"] == "s3cret"  # explicit passthrough wins


def test_meter_subprocesses_run_scrubbed(monkeypatch, env):
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        return SimpleNamespace(stdout="claude-opus-5-5-high\tOpus\n")
    monkeypatch.setenv("ZAI_API_KEY", "leak")
    monkeypatch.setattr(mm.subprocess, "run", fake_run)
    monkeypatch.setattr(mm.shutil, "which", lambda n: "/fake/agy")
    cat = mm.discover_models()
    assert cat["slugs"] == ["claude-opus-5-5-high"]
    assert "ZAI_API_KEY" not in (seen["env"] or {})


# --------------------------------------------------------------------------
# reauth no longer clobbers state.json (owner-found bug, 2026-11-15)
# --------------------------------------------------------------------------
def test_reauth_does_not_write_state(m, env, monkeypatch):
    monkeypatch.setattr(m, "refresh_auth", lambda: None)
    monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: None)
    m.attempt_silent_reauth()
    assert not env.state_file.exists()  # a throwaway Meter must never snapshot


# --------------------------------------------------------------------------
# per-slug usage stats (agy_status / pane)
# --------------------------------------------------------------------------
def test_slug_stats_accumulate(m, runs):
    runs.write("gemini-3.8-flash-high__a.json", make_rec(100, 50, 10))
    runs.write("gemini-3.8-flash-high__b.json",
               make_rec(1, 1, 0, status="ERROR", error="429"))
    runs.write("claude-sonnet-5-5-high__c.json", make_rec(10, 5, 0))
    m.ingest_runs()
    sl = m.state["slugs"]
    assert sl["gemini-3.8-flash-high"] == {"calls": 2, "errors": 1, "tokens": 162}
    assert sl["claude-sonnet-5-5-high"] == {"calls": 1, "errors": 0, "tokens": 15}


def test_slug_stats_count_corpses(m, runs):
    runs.write_raw("claude-opus-5-5-high__dead.json", "{broken")
    m.ingest_runs()
    assert m.state["slugs"]["claude-opus-5-5-high"]["errors"] == 1


# --------------------------------------------------------------------------
# git-fingerprint read-only guard (pattern: gogogoogle)
# --------------------------------------------------------------------------
def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*a):
        r = subprocess.run(["git", "-C", str(repo), *a],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-q", "--allow-empty", "-m", "init")
    (repo / "f.txt").write_text("one")
    git("add", "f.txt")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "f")
    return repo


def test_guard_clean_cycle(m, tmp_path):
    repo = _git_repo(tmp_path)
    before = m.guard_before(str(repo), key="k1")
    assert before["verdict"] == "BASELINE"
    assert m.guard_after(str(repo), key="k1")["verdict"] == "CLEAN"


def test_guard_detects_tracked_edit(m, tmp_path):
    repo = _git_repo(tmp_path)
    m.guard_before(str(repo), key="k2")
    (repo / "f.txt").write_text("changed")
    res = m.guard_after(str(repo), key="k2")
    assert res["verdict"] == "DRIFT"
    assert any("f.txt" in c for c in res["changed"])


def test_guard_detects_new_untracked_file(m, tmp_path):
    repo = _git_repo(tmp_path)
    m.guard_before(str(repo), key="k3")
    (repo / "new.txt").write_text("stuff")
    assert m.guard_after(str(repo), key="k3")["verdict"] == "DRIFT"


def test_guard_dirty_baseline_pins_dirt_and_detects_drift(m, tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("pre-existing dirt")
    before = m.guard_before(str(repo), key="k4")
    assert before["verdict"] == "BASELINE_DIRTY"
    assert m.guard_after(str(repo), key="k4")["verdict"] == "CLEAN"  # dirt pinned
    (repo / "f.txt").write_text("changed")
    assert m.guard_after(str(repo), key="k4")["verdict"] == "DRIFT"


def test_guard_not_a_repo_and_no_baseline(m, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert m.guard_before(str(plain))["verdict"] == "NOT_A_REPO"
    assert m.guard_after(str(plain), key="never")["verdict"] == "NO_BASELINE"


# --------------------------------------------------------------------------
# agy_dispatch: owned dispatch, structural metering (ADR-003)
# --------------------------------------------------------------------------
class _FakeProc:
    def __init__(self, rc=None):
        self.pid = 4242
        self.returncode = rc

    def poll(self):
        return self.returncode


def test_dispatch_spawns_scrubbed_and_structural(m, env, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "leak")
    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw["env"]
        captured["cwd"] = kw.get("cwd")
        captured["start_new_session"] = kw.get("start_new_session")
        return _FakeProc()
    monkeypatch.setattr(mm.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mm.shutil, "which", lambda n: "/fake/agy")
    out = m.dispatch(prompt="Do the thing", model="gemini-3.8-flash-high",
                     name="test task!", timeout_min=7)
    assert out["model"] == "gemini-3.8-flash-high"
    assert "gemini-3.8-flash-high__test-task-" in out["output_file"]
    cmd = captured["cmd"]
    assert cmd[0] == "/fake/agy" and cmd[1] == "-p" and cmd[2] == "Do the thing"
    assert cmd[3] == "--model" and cmd[4] == "gemini-3.8-flash-high"
    assert "7m" in cmd  # list argv: no shell-quoting pitfalls by construction
    assert "ZAI_API_KEY" not in captured["env"]  # credentials never reach agy
    assert captured["start_new_session"] is True
    d = m.state["dispatches"][-1]
    assert d["model"] == "gemini-3.8-flash-high" and d["pid"] == 4242
    assert d["pool"] == "gemini"


def test_dispatch_readonly_records_guard(m, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    # Delegate everything that is NOT our fake agy binary to the real Popen:
    # mm.subprocess IS the stdlib module, so a blind fake would also break
    # subprocess.run() inside the guard's git calls.
    real_popen = mm.subprocess.Popen

    def fake_popen(cmd, **kw):
        if cmd and str(cmd[0]) == "/fake/agy":
            return _FakeProc()
        return real_popen(cmd, **kw)
    monkeypatch.setattr(mm.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mm.shutil, "which", lambda n: "/fake/agy")
    out = m.dispatch(prompt="Read-only analysis", model="claude-opus-5-5-high",
                     name="ro", workdir=str(repo), readonly=True)
    assert out["guard"]["verdict"] == "BASELINE"
    (repo / "f.txt").write_text("tampered")
    res = m.guard_after(str(repo), key=out["guard"]["key"])
    assert res["verdict"] == "DRIFT"


def test_dispatch_validates_inputs(m):
    with pytest.raises(ValueError):
        m.dispatch(prompt="   ", model="x")
    with pytest.raises(ValueError):
        m.dispatch(prompt="ok", model="")


# --------------------------------------------------------------------------
# v0.3.1 review fixes: phantom errors, collisions, traversal, dead runs
# --------------------------------------------------------------------------
def test_ingest_skips_inflight_empty_run_file(m, runs):
    # review finding #1: dispatch() creates the output file empty at spawn;
    # a tick landing mid-run must NOT count a phantom error, and the cursor
    # must not advance past it (the completed file ingests on a later tick).
    p = runs.write_raw("gemini-3.8-flash-high__inflight-x.json", "")
    m.ingest_runs()
    g = m.state["pools"]["gemini"]
    assert g["calls"] == 0 and g["errors"] == 0
    assert m.state["slugs"] == {}
    runs.write_raw(p.name, json.dumps(make_rec(10, 5, 0)))
    m.ingest_runs()
    assert m.state["pools"]["gemini"]["calls"] == 1
    assert m.state["slugs"]["gemini-3.8-flash-high"]["tokens"] == 15


def test_dispatch_run_ids_unique_within_same_second(m, monkeypatch):
    # review finding #2: two same-name dispatches in one turn must not share
    # an output path (second open("wb") would truncate the first mid-flight).
    monkeypatch.setattr(mm.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    monkeypatch.setattr(mm.shutil, "which", lambda n: "/fake/agy")
    a = m.dispatch(prompt="a", model="gemini-3.8-flash-high", name="wave")
    b = m.dispatch(prompt="b", model="gemini-3.8-flash-high", name="wave")
    assert a["output_file"] != b["output_file"]


def test_dispatch_rejects_path_traversal_model(m, monkeypatch):
    # review finding #3: the slug becomes a filename — traversal and spaces
    # are refused before any file is created.
    monkeypatch.setattr(mm.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    monkeypatch.setattr(mm.shutil, "which", lambda n: "/fake/agy")
    with pytest.raises(ValueError):
        m.dispatch(prompt="x", model="../../evil")
    with pytest.raises(ValueError):
        m.dispatch(prompt="x", model="gemini flash")


def test_reap_accounts_dead_run_without_output_exactly_once(m, env, monkeypatch):
    # review finding #1 (dead side): a run that dies with rc!=0 and no output
    # is accounted by the reaper — once — and the empty file never re-counts.
    monkeypatch.setattr(mm.subprocess, "Popen", lambda cmd, **kw: _FakeProc(rc=3))
    monkeypatch.setattr(mm.shutil, "which", lambda n: "/fake/agy")
    out = m.dispatch(prompt="x", model="gemini-3.8-flash-high", name="dead")
    assert out["pid"] in m._dispatched
    m._reap_one(out["pid"], m._dispatched[out["pid"]])
    assert out["pid"] not in m._dispatched
    assert m.state["pools"]["gemini"]["errors"] == 1
    assert m.state["slugs"]["gemini-3.8-flash-high"]["errors"] == 1
    m.ingest_runs()  # empty file must not double-count
    assert m.state["pools"]["gemini"]["errors"] == 1


def test_reap_success_and_written_failure_are_not_phantomed(m, env, monkeypatch):
    monkeypatch.setattr(mm.subprocess, "Popen", lambda cmd, **kw: _FakeProc(rc=0))
    monkeypatch.setattr(mm.shutil, "which", lambda n: "/fake/agy")
    out = m.dispatch(prompt="x", model="claude-sonnet-5-5-high", name="ok")
    m._reap_one(out["pid"], m._dispatched[out["pid"]])  # rc=0: no accounting
    assert m.state["pools"]["third_party"]["errors"] == 0
    # rc!=0 but output WAS written: ingest owns the accounting, reaper stays out
    monkeypatch.setattr(mm.subprocess, "Popen", lambda cmd, **kw: _FakeProc(rc=1))
    out2 = m.dispatch(prompt="y", model="claude-sonnet-5-5-high", name="wrote")
    Path(out2["output_file"]).write_text(json.dumps(
        make_rec(5, 5, 0, status="ERROR", error="429")))
    m._reap_one(out2["pid"], m._dispatched[out2["pid"]])
    assert m.state["pools"]["third_party"]["errors"] == 0  # not yet ingested
    m.ingest_runs()
    assert m.state["pools"]["third_party"]["errors"] == 1  # counted exactly once

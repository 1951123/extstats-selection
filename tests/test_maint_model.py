"""Unit tests for the maintenance-cost corpus model (DB-free).

Covers the ``_maint.json`` schema round-trip and the pure linear-cost math in
``extstats2.core.maint_model`` (the live fitter needs an engine and is exercised
by the DB-timed runner / integration tests, not here).
"""
from __future__ import annotations

import json

import pytest

from extstats2.core.maint_model import (
    MAINT_FILE, MaintParams, maint_path, read_maint, write_maint)


def _params() -> MaintParams:
    return MaintParams(
        backend="postgres",
        fixed_seconds={".climate": {"0": 0.54, "1": 2.10}},
        c_var={".climate": {"0": 0.002, "1": 0.02}},
    )


def test_roundtrip_tmp(tmp_path):
    p = _params()
    # write under a results root -> corpus dir measure/census/postgres/_maint.json
    out = write_maint(tmp_path, "census", "postgres", p)
    assert out.name == MAINT_FILE
    assert out.parent.name == "postgres" and out.parent.parent.name == "census"
    got = read_maint(tmp_path, "census", "postgres")
    assert got is not None
    assert got.backend == "postgres"
    assert got.fixed(".climate", 0) == 0.54
    assert got.fixed(".climate", 1) == 2.10
    assert got.var(".climate", 0) == 0.002
    assert got.var(".climate", 1) == 0.02
    # int keys normalised to str in storage
    raw = json.loads(out.read_text())
    assert list(raw["fixed_seconds"][".climate"]) == ["0", "1"]


def test_maint_path_from_results_root(tmp_path):
    # canonical resolution from a results root: measure/<workload>/<backend>/
    assert maint_path(tmp_path, "census", "postgres") == (
        tmp_path / "measure" / "census" / "postgres" / MAINT_FILE)


def test_linear_cost_and_unmeasured_raises():
    p = _params()
    # one active table at level 1 with 3 stats: 2.10 + 0.02*3 = 2.16
    assert p.maint_for_table(".climate", 1, 3) == pytest.approx(2.16)
    # unmeasured table/level raises
    try:
        p.maint_for_table(".climate", 2, 1)
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_maint_total_and_fallback(tmp_path):
    p_data = {
        "backend": "oracle",
        "fixed_seconds": {".dmv": {"0": 0.54, "1": 2.10}},
        "c_var": {".dmv": {"0": 0.001, "1": 0.01}},
    }
    write_maint(tmp_path, "dmv", "oracle", MaintParams.from_dict(p_data))
    p = read_maint(tmp_path, "dmv", "oracle")
    plan = {".dmv": (1, 5)}          # measured
    assert p.maint_total(plan) == pytest.approx(2.10 + 0.01 * 5)
    # fallback used for an unmeasured table/level
    def fb(table, level, n):
        return 100.0 + 1.0 * n
    tot = p.maint_total({".other": (0, 4)}, fallback=fb)
    assert tot == pytest.approx(100.0 + 4.0)

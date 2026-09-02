"""Tests for the benchmark loaders (database-free).

These verify that the query files under ``benchmarks/`` parse into well-formed
``BenchQuery`` objects matching v1's documented query counts.
"""

from __future__ import annotations

import pytest

from extstats2.bench import load_benchmark, supported_benches
from extstats2.core.queries import BenchQuery


def test_supported_benches_match_v2_scope():
    # v2 drops JOB; supports Census + stats_CEB + its single-table sub-plans.
    assert set(supported_benches()) == {"census", "stats_ceb", "stats_ceb_single"}


def test_census_query_count_and_shape():
    qs = load_benchmark("census")
    assert len(qs) == 468
    for q in qs:
        assert isinstance(q, BenchQuery)
        assert q.bench == "census"
        assert q.ground_truth is not None and q.ground_truth > 0
        assert q.sql.strip().startswith("SELECT")
        assert q.qid.startswith("query.")


def test_stats_ceb_query_count_and_qid():
    qs = load_benchmark("stats_ceb")
    assert len(qs) == 146
    for q in qs:
        assert q.bench == "stats_ceb"
        assert q.ground_truth is not None and q.ground_truth > 0
        # qid is the ground truth (leading || prefix)
        assert str(q.ground_truth) == q.qid


def test_stats_ceb_single_query_count():
    qs = load_benchmark("stats_ceb_single")
    assert len(qs) == 632
    for q in qs:
        assert q.bench == "stats_ceb_single"
        assert q.ground_truth is not None
        assert q.qid.startswith("st.")
        assert q.sql.strip()  # non-empty SQL


def test_unknown_benchmark_raises():
    with pytest.raises(ValueError):
        load_benchmark("job")  # dropped in v2

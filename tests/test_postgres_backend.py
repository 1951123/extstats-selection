"""Integration tests for the PostgreSQL backend (Milestone M2).

These require a running PostgreSQL 16 with the Census data loaded (the v2 dev
environment has this). They are skipped automatically if PG is unreachable, so
the rest of the suite stays green on machines without a live DB.
"""

from __future__ import annotations

import pytest

from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capability, Capacity
from extstats2.bench import load_benchmark
from extstats2.config import DBConfig, get_backend

# Postgres credentials matching the dev environment (superuser password set).
_PG = DBConfig(host="localhost", port=5432, user="postgres",
               password="postgres", dbname="census")


def _pg_reachable() -> bool:
    try:
        import psycopg
        conn = psycopg.connect(host=_PG.host, port=_PG.port, user=_PG.user,
                               password=_PG.password, dbname=_PG.dbname,
                               connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


_NEED_PG = pytest.mark.skipif(not _pg_reachable(), reason="PostgreSQL not reachable")


@pytest.fixture
def backend():
    be = get_backend("postgres", cfg=_PG)
    # ensure a clean slate (no leftover ext stats on climate)
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)
    yield be
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)


@_NEED_PG
def test_estimate_census_query(backend):
    q = load_benchmark("census")[0]
    est = backend.estimate(q)
    assert est.estimate > 0
    assert est.actual == q.ground_truth
    assert est.qerror is not None and est.qerror >= 1.0


@_NEED_PG
def test_measure_single_candidate_cleans_up(backend):
    from extstats2.core.candidates import generate_candidates_per_query
    from extstats2.core.measure import measure_query

    q = load_benchmark("census")[0]
    cands = generate_candidates_per_query([q])[q.qid][:1]
    mes = measure_query(backend, q, cands, capacity_levels=(0,))
    assert mes.estimate_base > 0
    assert len(mes.candidates) == 1
    # no leftover statistics after a clean measure
    assert backend.list_stats(".climate") == []


@_NEED_PG
def test_create_size_drop_roundtrip(backend):
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    obj = StatObject(table=".climate", columns=("iAvail", "iClass"),
                     capability=mcv, capacity=Capacity(0),
                     name="ext_m_test_roundtrip")
    backend.create_stat(obj)
    backend.build_stats([obj], Capacity(0))
    size = backend.stat_size_bytes(obj)
    assert size > 0
    backend.drop_stat(obj)
    assert backend.stat_size_bytes(obj) == 0


@_NEED_PG
def test_maintain_cost_positive_and_monotonic(backend):
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    lo = StatObject(table=".climate", columns=("a", "b"), capability=mcv,
                    capacity=Capacity(0))
    hi = StatObject(table=".climate", columns=("a", "b"), capability=mcv,
                    capacity=Capacity(2))
    assert backend.maintain_cost(lo) > 0
    assert backend.maintain_cost(hi) > backend.maintain_cost(lo)


@_NEED_PG
def test_capacity_ladder_mapping(backend):
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    for lvl, expected in [(0, 100), (1, 1000), (2, 10000)]:
        obj = StatObject(table=".climate", columns=("a", "b"), capability=mcv,
                         capacity=Capacity(lvl))
        assert backend._native_target(obj.capacity) == expected

"""Integration tests for the Oracle backend (Milestone M3).

These require a running Oracle (the v2 dev environment has an Oracle Database
Free in a docker container, with Census CLIMATE loaded). The tests are skipped
automatically if Oracle is unreachable, so the rest of the suite stays green on
machines without a live DB.
"""

from __future__ import annotations

import pytest

from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capability, Capacity
from extstats2.bench import load_benchmark
from extstats2.config import DBConfig, get_backend

# Oracle credentials matching the dev environment.
_OR = DBConfig(host="localhost", port=1521, user="SYSTEM",
               password="lxf82073077", service="FREEPDB1")


def _oracle_reachable() -> bool:
    try:
        import oracledb
        conn = oracledb.connect(user=_OR.user, password=_OR.password,
                                dsn=f"{_OR.host}:{_OR.port}/"
                                    f"{_OR.service or 'FREEPDB1'}")
        conn.close()
        return True
    except Exception:
        return False


_NEED_OR = pytest.mark.skipif(
    not _oracle_reachable(), reason="Oracle not reachable")


@pytest.fixture
def backend():
    be = get_backend("oracle", cfg=_OR)
    # clean slate on CLIMATE
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)
    yield be
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)


@_NEED_OR
def test_estimate_census_query(backend):
    q = load_benchmark("census")[0]
    est = backend.estimate(q)
    assert est.estimate > 0
    assert est.actual == q.ground_truth
    assert est.qerror is not None and est.qerror >= 1.0


@_NEED_OR
def test_measure_single_candidate_cleans_up(backend):
    from extstats2.core.candidates import generate_candidates_per_query
    from extstats2.core.measure import measure_query

    q = load_benchmark("census")[0]
    cands = [c for c in generate_candidates_per_query([q])[q.qid]
             if set(c.columns) == {"iAvail", "iClass"}]
    mes = measure_query(backend, q, cands, capacity_levels=(0,))
    assert mes.estimate_base > 0
    assert len(mes.candidates) == 1
    # an mcv column group on (iAvail,iClass) should materially reduce q-error
    for cm in mes.candidates.values():
        for lv in cm.levels.values():
            assert lv["qerror"] < mes.qerror_base * 0.5
    # no leftover statistics after a clean measure
    assert backend.list_stats(".climate") == []


@_NEED_OR
def test_create_build_drop_roundtrip(backend):
    """Gather is what materialises a group; drop removes it (cleanup)."""
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    obj = StatObject(table=".climate", columns=("iAvail", "iClass"),
                     capability=mcv, capacity=Capacity(0))
    backend.build_stats([obj], Capacity(0))
    # group must now be visible in the catalog
    assert any(s.columns == ("IAVAIL", "ICLASS") for s in backend.list_stats(".climate"))
    size = backend.stat_size_bytes(obj)
    assert size > 0
    backend.drop_stat(obj)
    assert not any(s.columns == ("IAVAIL", "ICLASS")
                   for s in backend.list_stats(".climate"))


@_NEED_OR
def test_list_stats_empty_baseline(backend):
    assert backend.list_stats(".climate") == []


@_NEED_OR
def test_maint_tiers_monotonic(backend):
    tiers = backend.table_maintain_tiers(".climate")
    assert len(tiers) == 3  # ladder levels 0,1,2
    assert all(tiers[i] < tiers[i + 1] for i in range(len(tiers) - 1))
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    assert backend.stat_maintain_var(
        StatObject(table=".climate", columns=("a", "b"),
                   capability=mcv, capacity=Capacity(1))) >= 0.0


@_NEED_OR
def test_ceb_posts_query_transpiles_and_cleans_up(backend):
    """A PG-dialect CEB query (AS alias) is transpiled to Oracle, measurable,
    and leaves no leftover column group on the posts table."""
    from extstats2.core.candidates import generate_candidates_per_query
    from extstats2.core.measure import measure_query

    qs = load_benchmark("stats_ceb_single")
    q = next(x for x in qs if x.qid == "st.12")  # posts, numeric-only
    # clean slate for .posts
    for s in list(backend.list_stats(".posts")):
        backend.drop_stat(s)
    est = backend.estimate(q)  # exercises PG->Oracle transpile + EXPLAIN
    assert est.estimate > 0
    cands = [c for c in generate_candidates_per_query([q])[q.qid]]
    mes = measure_query(backend, q, cands[:1], capacity_levels=(0,))
    assert mes.estimate_base > 0
    assert backend.list_stats(".posts") == []


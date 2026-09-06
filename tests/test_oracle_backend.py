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
def test_measure_dominant_pair_helps_on_correlated_query(backend):
    """On a strongly-correlated worst census query, the mcv column-group given
    by M4 (query.62 -> (iRspouse,iWork89)) materially reduces q-error at full
    sampling, and the Protocol-A measure leaves no column groups behind.

    This is the honest cross-engine claim (see docs/architecture.md §6.3c): the
    improvement is query- and sampling-dependent — q0's near-independent
    predicates are already well estimated by natural single-column stats, so we
    assert on query.62, not q0.
    """
    from extstats2.backend.capabilities import Capacity
    from extstats2.backend.base import StatObject
    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import generate_candidates_per_query
    from extstats2.core.measure import measure_query

    q = load_benchmark("census")[61]  # query.62, truth=45 (very sparse)
    cands = [c for c in generate_candidates_per_query([q], arities=(2,))[q.qid]
             if set(c.columns) == {"iRspouse", "iWork89"}]
    assert cands, "dominant pair candidate required"
    # measure at both S-grid levels. On climate (~2.46M) L1=300k rows (~12%) is
    # the deepest tier the S-grid realizes (the old 100% fix was dropped), and
    # even L0=30k already gives the dominant-pair histogram enough of the sparse
    # 45-row target to make its q-error near-faithful vs the grossly-off base.
    mes = measure_query(backend, q, cands, capacity_levels=(0, 1))
    assert mes.estimate_base > 0
    assert len(mes.candidates) == 1
    for cm in mes.candidates.values():
        for lv in cm.levels.values():
            assert lv["qerror"] < mes.qerror_base * 0.1, (
                "dominant pair must materially cut q-error on a sparse "
                "correlated query")
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
    assert len(tiers) == 2  # S-grid ladder levels 0,1 (fixed-% L2 dropped)
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


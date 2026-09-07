"""Integration tests for the Oracle backend (Milestone M3).

These require a running Oracle (the v2 dev environment has an Oracle Database
Free in a docker container, with Census CLIMATE loaded). The tests are skipped
automatically if Oracle is unreachable, so the rest of the suite stays green on
machines without a live DB.
"""

from __future__ import annotations

import pytest

from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capability, SamplingLevel
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
def test_mcv_dominant_pair_cuts_qerror_no_leftover(backend):
    """On a strongly-correlated worst census query, the mcv column-group given
    by M4 (query.62 -> (iRspouse,iWork89)) materially reduces q-error at S-grid
    depth, expressed via the current backend primitives (build_stats + estimate)
    and leaving no column group behind.

    This is the honest cross-engine claim (docs/architecture.md §6.3c): the
    improvement is query- and sampling-dependent — q0's near-independent
    predicates are already well estimated by natural single-column stats, so we
    assert on query.62, not q0.
    """
    from extstats2.backend.base import StatObject
    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import generate_candidates_per_query

    q = load_benchmark("census")[61]  # query.62, truth=45 (very sparse)
    cols = ("iRspouse", "iWork89")
    assert any(set(c.columns) == set(cols)
               for c in generate_candidates_per_query([q], arities=(2,))[q.qid]), \
        "dominant pair candidate required"
    # clean slate: no prior ext group should distort the natural base estimate
    for s in list(backend.list_stats(".climate")):
        backend.drop_stat(s)
    base = backend.estimate(q).qerror
    # build the dominant 2-col mcv at S-grid level 1 (deeper sampling; the
    # 45-row sparse target still gets enough histogram mass to be repaired).
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    obj = StatObject(table=".climate", columns=cols, capability=mcv,
                     sampling_level=SamplingLevel(1), name="ext_m_dom_l1")
    backend.build_stats([obj], SamplingLevel(1))
    after = backend.estimate(q).qerror
    assert after < base * 0.1, (
        f"dominant pair must materially cut q-error on a sparse correlated "
        f"query (base={base:.3f}, with-pair={after:.3f})")
    # no leftover statistics after a clean build/measure
    backend.drop_stat(obj)
    assert backend.list_stats(".climate") == []


@_NEED_OR
def test_create_build_drop_roundtrip(backend):
    """Gather is what materialises a group; drop removes it (cleanup)."""
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    obj = StatObject(table=".climate", columns=("iAvail", "iClass"),
                     capability=mcv, sampling_level=SamplingLevel(0))
    backend.build_stats([obj], SamplingLevel(0))
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
                   capability=mcv, sampling_level=SamplingLevel(1))) >= 0.0


@_NEED_OR
def test_ceb_posts_query_transpiles_and_cleans_up(backend):
    """A PG-dialect CEB query (AS alias) transpiles to Oracle, EXPLAINs, and a
    build/drop cycle on /posts leaves no leftover column group."""
    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import generate_candidates_per_query

    qs = load_benchmark("stats_ceb_single")
    q = next(x for x in qs if x.qid == "st.12")  # posts, numeric-only
    # clean slate for .posts
    for s in list(backend.list_stats(".posts")):
        backend.drop_stat(s)
    est = backend.estimate(q)  # exercises PG->Oracle transpile + EXPLAIN
    assert est.estimate > 0
    # build the first candidate's group (pure backend), then drop it
    cands = generate_candidates_per_query([q])[q.qid]
    assert cands
    cols = tuple(cands[0].columns)
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    obj = StatObject(table=".posts", columns=cols, capability=mcv,
                     sampling_level=SamplingLevel(0))
    backend.build_stats([obj], SamplingLevel(0))
    backend.drop_stat(obj)
    assert backend.list_stats(".posts") == []


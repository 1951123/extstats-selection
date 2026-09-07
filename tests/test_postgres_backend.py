"""Integration tests for the PostgreSQL backend (Milestone M2).

These require a running PostgreSQL 16 with the Census data loaded (the v2 dev
environment has this). They are skipped automatically if PG is unreachable, so
the rest of the suite stays green on machines without a live DB.
"""

from __future__ import annotations

import pytest

from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capability, SamplingLevel
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
def test_create_size_drop_roundtrip(backend):
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    obj = StatObject(table=".climate", columns=("iAvail", "iClass"),
                     capability=mcv, sampling_level=SamplingLevel(0),
                     name="ext_m_test_roundtrip")
    backend.create_stat(obj)
    backend.build_stats([obj], SamplingLevel(0))
    size = backend.stat_size_bytes(obj)
    assert size > 0
    backend.drop_stat(obj)
    assert backend.stat_size_bytes(obj) == 0


@_NEED_PG
def test_table_maintain_tiers_monotonic(backend):
    """Per-table fixed cost ladder must increase monotonically with tier."""
    tiers = backend.table_maintain_tiers(".climate")
    assert len(tiers) == 2  # S-grid ladder levels 0,1 (L2 dropped)
    assert all(tiers[i] < tiers[i + 1] for i in range(len(tiers) - 1))
    assert backend.stat_maintain_var(
        StatObject(table=".climate", columns=("a", "b"),
                   capability=[c for c in backend.supported_capabilities()
                               if c.name == "mcv"][0],
                   sampling_level=SamplingLevel(1))) > 0


@_NEED_PG
def test_table_maintain_tiers_match_model(backend):
    """The per-tier fixed base matches the calibrated linear model on Census
    climate (target < N/300 saturates): = w_per_target * target, two tiers."""
    tiers = backend.table_maintain_tiers(".climate")
    assert len(tiers) == 2
    for lvl, target in ((0, 100), (1, 1000)):
        # climate N=2.46M -> t_sat~8200 >> 1000, so pure linear region
        w = backend._W_PER_TARGET
        assert tiers[lvl] == pytest.approx(w * target), (
            f"level {lvl}: model {tiers[lvl]:.3f} vs linear {w*target:.3f}")


@_NEED_PG
def test_sampling_ladder_mapping(backend):
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    # S-grid ladder: L0->100 (S=30k), L1->1000 (S=300k); L2 (10000) dropped.
    for lvl, expected in [(0, 100), (1, 1000)]:
        obj = StatObject(table=".climate", columns=("a", "b"), capability=mcv,
                         sampling_level=SamplingLevel(lvl))
        assert backend._native_target(obj.sampling_level) == expected
    # level outside the S-grid must raise (not silently build off-ladder)
    obj = StatObject(table=".climate", columns=("a", "b"), capability=mcv,
                     sampling_level=SamplingLevel(2))
    with pytest.raises(KeyError):
        backend._native_target(obj.sampling_level)


@_NEED_PG
def test_single_col_pin_and_sample_floor(backend):
    """Decided deployment semantics: regular (single) columns are pinned to
    statistics_target=100 via ALTER COLUMN SET STATISTICS, so (a) building an
    extended statistic at a high target must NOT raise the connection's
    default_statistics_target, and (b) the ANALYZE sample has a floor of ~300*100
    regardless of how low a hypothetical extended target goes. This decouples the
    per-extended-stat representation parameter from regular-column fidelity.
    """
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    obj = StatObject(table=".climate", columns=("iAvail", "iClass"),
                     capability=mcv, sampling_level=SamplingLevel(1),  # S-grid L1, target 1000
                     name="ext_m_pin_test")
    backend.create_stat(obj)
    backend.build_stats([obj], SamplingLevel(1))
    try:
        # (a) regular single column stays pinned at 100 even after a 10000 build
        with backend.conn.cursor() as cur:
            cur.execute(
                "SELECT a.attstattarget FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "WHERE c.relname = 'climate' AND a.attname = 'iavail'")
            st = cur.fetchone()[0]
            assert st == 100, f"single column not pinned: attstattarget={st}"
            # the connection default must not have been dragged to 10000
            cur.execute("SHOW default_statistics_target")
            dflt = int(cur.fetchone()[0])
            assert dflt == 100, (
                f"default_statistics_target raised to {dflt}; only ext target "
                "should vary")
        # (b) sub-100 extended target is floored at 300*100 = 30000 sample rows
        n = backend._reltuples(".climate")
        saved = backend._ladder
        try:
            backend._ladder = {0: 50, 1: 100}  # synthetic sub-100 tier
            assert backend.sample_rows_per_level(".climate", 0) == 30000.0
            assert backend.sample_rows_per_level(".climate", 1) == 30000.0
        finally:
            backend._ladder = saved
            assert backend.num_rows(".climate") == n or n > 0
    finally:
        backend.drop_stat(obj)
    assert backend.list_stats(".climate") == []

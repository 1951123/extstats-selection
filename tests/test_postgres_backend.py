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
def test_measure_records_lambda_and_variance(backend):
    """Per-level measurement now records the v1 'lambda' expected-capture and
    the q-error spread over repeats. lambda must grow with capacity; at the
    lowest census tier the driving sparse combo can have lambda < 1 (high
    variance, unreliable single read) while deeper tiers are faithful.
    """
    from extstats2.core.candidates import generate_candidates_per_query
    from extstats2.core.measure import measure_query

    q = load_benchmark("census")[61]  # query.62: truth=45, very sparse
    cands = [c for c in generate_candidates_per_query([q], arities=(2,))[q.qid]
             if set(c.columns) == {"iRspouse", "iWork89"}]
    mes = measure_query(backend, q, cands, capacity_levels=(0, 1, 2),
                        repeats=2, capabilities=["mcv"])
    assert len(mes.candidates) == 1
    cm = next(iter(mes.candidates.values()))
    lam = {lvl: lv["lambda_expected"] for lvl, lv in cm.levels.items()}
    assert all(lv["lambda_expected"] is not None for lv in cm.levels.values())
    assert lam[0] < lam[2]          # deeper sampling => higher expected capture
    assert lam[2] > 1.0             # full scan captures the combo many times
    for lv in cm.levels.values():
        assert "qerror_std" in lv and "qerror_worst" in lv
    # backend sampling contract
    assert backend.num_rows(".climate") is not None
    assert backend.sample_rows_per_level(".climate", 2) is not None


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
def test_table_maintain_tiers_monotonic(backend):
    """Per-table fixed cost ladder must increase monotonically with tier."""
    tiers = backend.table_maintain_tiers(".climate")
    assert len(tiers) == 3  # ladder levels 0,1,2
    assert all(tiers[i] < tiers[i + 1] for i in range(len(tiers) - 1))
    assert backend.stat_maintain_var(
        StatObject(table=".climate", columns=("a", "b"),
                   capability=[c for c in backend.supported_capabilities()
                               if c.name == "mcv"][0],
                   capacity=Capacity(1))) > 0


@_NEED_PG
def test_table_maintain_tiers_match_measured_fixed_analyze(backend):
    """The per-tier fixed base should reproduce measured bare-ANALYZE times
    (Census climate, warm) within tolerance."""
    tiers = backend.table_maintain_tiers(".climate")
    # tiers indexed by level -> target 100/1000/10000
    measured = {0: 0.25, 1: 2.59, 2: 20.55}
    for lvl, tgt in measured.items():
        assert tiers[lvl] == pytest.approx(tgt, rel=0.35), (
            f"level {lvl}: model {tiers[lvl]:.2f} vs measured {tgt}"
        )
    # Saturation: highest tier << pure-linear w*t (=25.6s)
    assert tiers[2] < backend._W_PER_TARGET * 10000.0 * 0.95


@_NEED_PG
def test_capacity_ladder_mapping(backend):
    mcv = [c for c in backend.supported_capabilities() if c.name == "mcv"][0]
    for lvl, expected in [(0, 100), (1, 1000), (2, 10000)]:
        obj = StatObject(table=".climate", columns=("a", "b"), capability=mcv,
                         capacity=Capacity(lvl))
        assert backend._native_target(obj.capacity) == expected


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
                     capability=mcv, capacity=Capacity(2),  # target 10000
                     name="ext_m_pin_test")
    backend.create_stat(obj)
    backend.build_stats([obj], Capacity(2))
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

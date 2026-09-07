"""PG-specific OID-order selector (deployment-stage concern, NOT the generic
core optimizer).  The chosen-stat SET comes from the frozen generic optimizer
(optimize.py / optimize_lambda.build_inner_at_level); THIS module only orders
CREATE (OID) for PG deployment so each query is served by its best applicable
chosen statistic under PG's first-applicable-by-OID planner semantics.  Kept as
a deploy-layer concern (see docs/deploy.md), algorithm still valid.

PG-specific OID-order selector, Phase 2 (two-phase design).

Phase 1 = the *generic* optimiser (untouched, ``optimize.py`` /
``optimize_lambda.build_inner_at_level``): it selects a (colset, param) SET under
a budget, maximising interference-free predicted quality.

Phase 2 (this module): PostgreSQL 16's planner uses, for each query, the first
(co-installed, applicable) multivariate statistic by OID.  So the realised quality
depends on the CREATE order.  Given the FIXED chosen set from Phase 1, this module
finds a CREATE (OID) order that makes each query's *best applicable* chosen stat
be created before its other applicable chosen stats — i.e. it realises the
predicted (single-winner) quality rather than a hijacked one.

Implementation: a DAG/topological approach over the chosen colsets.  For each
query q, let ``b(q)`` = its best applicable chosen colset (lowest isolated
q-error).  Add directed edge ``b(q) -> c`` for every other applicable chosen
colset ``c`` of q ("b(q) must precede c").  A topological order of this DAG (when
acyclic) guarantees every query is served by its best applicable colset.  Cycles
(different queries demanding opposite orders among overlapping colsets) are
broken so some tail queries settle: this is exactly the unavoidable residual of
planner-interference — no single order can give every query its first choice.

No scipy/milp is required: 282 colsets / ~1.4k (query x applicable) is trivially
fast via Kahn's algorithm.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Optional, Sequence


@dataclass
class ChosenStat:
    columns: tuple
    stat_key: str
    qerr_by_query: dict = field(default_factory=dict)   # qid -> isolated e_is


def _best_applicable(qid, idxs, chosen):
    """Return the chosen-index (within ``chosen``) that serves qid best."""
    best_i = None
    best_e = None
    for i in idxs:
        ev = chosen[i].qerr_by_query.get(qid)
        if ev is None:
            continue                       # not measured / not applicable-improving
        if best_e is None or ev < best_e:
            best_e = ev
            best_i = i
    return best_i, best_e


def solve_pg_order(chosen: Sequence[ChosenStat],
                   qbase: dict,
                   applicable: dict) -> dict:
    """Return an ordered list of chosen index (CREATE sequence = OID order).

    ``applicable[qid]`` : tuple of chosen indices applicable to qid (columns ⊆
    the query's predicate columns).  Return includes ``order`` (stat_keys),
    ``realised_mean`` (mean served q-error, unserved queries at baseline),
    and whether the preference graph was acyclic.
    """
    n = len(chosen)
    qids = [q for q in applicable]          # deterministic order

    # edges: best_i -> other applicable chosen of q (best must be created earlier)
    # edge adjacency: children[par] = set of indices that must come after par
    children = {i: set() for i in range(n)}
    indeg = {i: 0 for i in range(n)}
    used_edge = set()
    acyclic = True
    # For each query decide its best applicable improving stat.
    for q in qids:
        b_i, b_e = _best_applicable(q, applicable[q], chosen)
        if b_i is None:
            continue
        for c in applicable[q]:
            if c == b_i:
                continue
            e = (b_i, c)
            if e not in used_edge:
                used_edge.add(e)
                if c not in children[b_i]:
                    children[b_i].add(c)
                    indeg[c] += 1

    # Kahn topological order with deterministic tie-breaking (smallest index first)
    order_idx = []
    # source = indeg 0
    import heapq as hq
    heap = [i for i in range(n) if indeg[i] == 0]
    hq.heapify(heap)
    while heap:
        u = hq.heappop(heap)
        order_idx.append(u)
        for v in list(children[u]):
            indeg[v] -= 1
            if indeg[v] == 0:
                hq.heappush(heap, v)
    if len(order_idx) < n:
        # cycle: append remaining arbitrarily (they couldn't be fully ordered)
        acyclic = False
        remaining = [i for i in range(n) if i not in set(order_idx)]
        order_idx.extend(remaining)

    security = [chosen[i].stat_key for i in order_idx]

    # realised mean: for each query, served by its best *created-before-others*
    # applicable = the lowest-OID applicable chosen stat (first in order).
    # A simple faithful reading: query q is served by the applicable chosen stat
    # that appears earliest in ``order_idx`` (PG picks first by OID).
    pos = {i: p for p, i in enumerate(order_idx)}
    realized = {}
    for q in qids:
        apps = list(applicable[q])
        if not apps:
            continue
        # lowest OID = min pos
        pick = min(apps, key=lambda i: pos[i])
        ev = chosen[pick].qerr_by_query.get(q)
        realized[q] = ev if ev is not None else qbase.get(q, float("inf"))

    # realised mean over ALL queries (qids incl. coverage) - but qbase for all 468
    mean_r = 0.0
    count = 0
    for q in qbase:
        v = realized.get(q, qbase[q])
        mean_r += v
        count += 1
    rmean = mean_r / count if count else float("nan")

    return {"order": [chosen[i].stat_key for i in order_idx],
            "order_idx": order_idx,
            "acyclic": acyclic,
            "realised": realized,
            "realised_mean": rmean,
            "n_queries": len(qids)}


# ---------------------------------------------------------------------------
# Weighted greedy feedback-arc version (Phase 2, dense/cyc deployments)
# ---------------------------------------------------------------------------

def solve_pg_order_fb(chosen: Sequence[ChosenStat],
                      qbase: dict,
                      applicable: dict) -> dict:
    """CREATE-order via weighted greedy feedback-arc-set breaking.

    Same desired outcome as :func:`solve_pg_order`, but built for the dense case
    (census: ~1 giant cyclic component).  For each query q we add preference edge
    ``best_q -> c`` for every other applicable chosen stat ``c`` ("q's best
    applicable colset must precede c").  Edge weight ``w(best_q->c)`` = the q-error
    loss q pays if its best does NOT get to serve it first ~= (its 2nd-best
    achievable e_is) - (its best e_is).  We then greedily find a cyclic region,
    delete its minimum-weight edge (query 'sacrificed': it will settle for a
    later-applicable stat), iterate until the digraph is acyclic, and topologically
    sort into the CREATE order.
    """
    import sys
    sys.setrecursionlimit(1 << 22)
    n = len(chosen)
    qids = list(applicable.keys())

    def best_stuff(q):
        """Return (best_index, sorted applicable by e_is improving)."""
        scored = []
        for i in applicable[q]:
            e = chosen[i].qerr_by_query.get(q)
            if e is None:
                continue
            scored.append((e, i))
        if not scored:
            return None
        scored.sort()
        return scored

    # edges best->other with weights; adjacency
    children = [[] for _ in range(n)]
    weight = {}
    edge_owner = {}           # (u,v) -> q (for bookkeeping of sacrificed query)
    for q in qids:
        s = best_stuff(q)
        if s is None or len(s) < 2:
            continue
        best = s[0][1]
        e1 = s[0][0]
        # loss if best not served first ~= e_is of next achievable best
        e2 = s[1][0]
        w = e2 - e1
        if w < 0:
            w = 0.0
        for (_, c) in s[1:]:
            if best == c:
                continue
            children[best].append(c)
            # for parallel edges across queries keep conservative min-loss
            wk = weight.get((best, c))
            if wk is None or w < wk:
                weight[(best, c)] = w
                edge_owner[(best, c)] = q

    def kahn_order(children_work):
        deg = [len(children_work[i]) for i in range(n)]
        # reverse index count
        rd = {}
        for u in range(n):
            for v in children_work[u]:
                rd[v] = rd.get(v, 0) + 1
        import heapq as hq
        q = [i for i in range(n) if rd.get(i, 0) == 0]
        hq.heapify(q)
        order = []
        while q:
            u = hq.heappop(q)
            order.append(u)
            for v in children_work[u]:
                rd[v] -= 1
                if rd[v] == 0:
                    hq.heappush(q, v)
        return order

    # working adjacency; iteratively cut cheapest edge whose endpoints lie in the
    # (still-cyclic) residue until the graph is acyclic.  Both endpoints in the
    # residue => the edge is on a cycle => removal strictly reduces cyclic content,
    # so this terminates.
    children = {u: list(children[u]) for u in range(n)}   # copy
    removed_edges = []
    while True:
        order = kahn_order(children)
        if len(order) == n:
            break
        residue = set(range(n)) - set(order)               # nodes still on a cycle
        # cheapest edge (u,v) with u in residue and v in residue
        best_edge = None
        best_w = None
        for u in residue:
            for v in list(children[u]):
                if v not in residue:
                    continue
                w = weight.get((u, v), 0.0)
                if best_w is None or w < best_w:
                    best_w = w
                    best_edge = (u, v)
        if best_edge is None:
            break
        u0, v0 = best_edge
        children[u0].remove(v0)
        removed_edges.append(best_edge)

    final_order = kahn_order(children)
    if len(final_order) < n:  # residual (safety) — arbitrary append
        final_order += [i for i in range(n) if i not in set(final_order)]

    pos = {i: p for p, i in enumerate(final_order)}
    realized = {}
    for q in qids:
        if not applicable[q]:
            continue
        pick = min(applicable[q], key=lambda i: pos[i])
        ev = chosen[pick].qerr_by_query.get(q)
        realized[q] = ev if ev is not None else qbase.get(q, float("inf"))
    mean_r = sum(realized.get(q, qbase[q]) for q in qbase) / max(len(qbase), 1)
    return {"order": [chosen[i].stat_key for i in final_order],
            "order_idx": final_order,
            "acyclic": len(removed_edges) == 0,
            "n_feedback_cut": len(removed_edges),
            "removed_edges": removed_edges,
            "realised": realized,
            "realised_mean": mean_r}


__all__ = ["ChosenStat", "solve_pg_order", "solve_pg_order_fb"]

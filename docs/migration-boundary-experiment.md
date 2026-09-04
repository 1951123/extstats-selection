# Experiment (B): migration boundary of single-table extended statistics
**stats_CEB_single (chosen set) → stats_CEB multi-table join workload (145 joins)**

**Question.** Does deploying the single-table extended-stat set (chosen for the
single-sub-plan workload) onto the *shared* base tables give better joins —
i.e. is the causal chain `better single-card -> better join-order -> faster`
real? stats_CEB_single was an "easy" workload (baseline mean ~1.33 -> L1 floor
~1.08); this probe asks whether that improvement propagates to the correlated
multi-join regime where *more* gain was conjectured.

**Protocol** (`scratch/e2e_migrate_b.py`, on a fresh `stats_jb` TEMPLATE clone):
  1. Phase-1 L1 @ 80KB on stats_ceb_single -> 30 chosen (colset,param) MCV,
     28/30 are on columns the join workload filters on a single base table
     (posts 16 / users 9 / votes 2 / comments 1), so they are genuinely
     *applicable* as join-input filters.
  2. baseline: default-ANALYZE all 8 base tables; EXPLAIN the 145 joins, compare
     top-level `Plan Rows` (est) to the true COUNT (ground truth embedded in qid).
  3. deploy the 30 stats per owning table + ANALYZE; re-EXPLAIN the 145 joins.
  Join-ORDER change = structural signature diff of the EXPLAIN plan tree.

## Results (L1 @ 80KB, join-level cardinality error)
| state | mean | geo | median | n >2x | n >10x |
|---|---|---|---|---|---|
| baseline   | 34,961x | 16.8x | 11.4x | 104/146 | 76/146 |
| deployed   | 35,894x | 17.4x | 11.2x | 111/146 | 77/146 |

- Per-join: only **46/146** joined-estimate better by >0.05, **65/146** worse
  (many around the 1-3x noise band), net ~no change.
- **Structural (join-shape/order) diff** — tree-shaped, comparing NodeType +
  relation placement + join keys, *ignoring* row-estimate churn (the flat-name
  count inflates on any detail change and the raw compare inflates on any rows
  change; only the row-free tree compare isolates true shape/order change):
  **only 8 of 143** multi-table join plans changed their structure at all
  (`11075893, 183537163, 18762969, 2016238, 228748307, 35205, 436286, 537352263`).
  And the direction is not systematically good: of those 8, only 3 improved
  (best 537352263 58.7x -> 47.3x), while the high-error ones either did not
  change or got worse (228748307 353.8x -> 756.6x, 11075893 103.2x -> 132.6x,
  436286, 183537163). The dominant (10^3-10^6x) join errors are never fixed.

## Error attribution: stats_CEB's estimation error is dominated by the JOINS,
   not the base-table selection predicates

The boundary result doubles as a clean positive attribution. Three pieces of
evidence, all measured, isolate *where* the error lives in stats_CEB:

1. **Base-table selection predicates are only mildly wrong AND are fixable by
   single-table extended stats.** On stats_CEB_single (the SAME base tables and
   the SAME single-table filter predicates), the default-analyze baseline is
   only ~1.33x and single-table ext stats push it to ~1.08. So the "input card"
   of a join that comes from its WHERE filters is already near-accurate and is
   *not* a large error source.
2. **Once the same tables are joined, the error explodes to ~10x - 10^6x.** The
   145-join root-node card error is median ~11x, geo ~17x, mean ~3e4x, p90 ~300x,
   max ~10^6x. This cannot come from the base-table filters (those are ~1.33x on
   their own); it must come from the join layer — cross-table join selectivity
   (key-correlation / skew / multi-level intermediate cardinality accumulation)
   where the planner's independence-uniformity assumptions break down on the
   correlated StackExchange data.
3. **Deploying exactly the fixes for (1) leaves (2) unchanged.** Migrating the
   28/30 single-table stats that are the join's own base filters changed the
   join error median only 11.4x -> 11.2x, changed join shape in only 8/143
   plans (in a non-systematically-helpful direction), and never touched the
   dominant 10^3-10^6x errors.

=> **Claim (supported).** For stats_CEB, the cardinality-estimation error is
**dominated by the joins** — specifically cross-table join selectivity — and not
by the base-table selection predicates. The predicates contribute only a mild
(~1.3x) error that single-table extended statistics can remove (to ~1.08); but
because single-table stats can only tighten the *input* (selection) side and PG
cannot use them to correct join selectivity between two tables' keys, improving
the base-table predicates does not improve the join estimate. Single-table
extended statistics are therefore scoped precisely to base-table selection
predicates (achievable ~1.08 on this regime); fixing multi-table joins requires
join-aware statistics on cross-table / join-key correlated columns, which is a
different model than this system selects.

## Interpretation (honest limiting/negative result)
The single-table ext stats *are* applied to the join's base-table filters, yet
they do **not** propagate to better multi-table join plans:

- The dominant join-level error here is **cross-table join selectivity**
  (median ~11x up to ~10^6x), which single-table multivariate stats on the
  *filter* columns cannot address: PG uses them for the input's selection
  cardinality, but join-selectivity between two tables is governed by the
  correlation of (join-key) distributions, not by the per-input filter MCVs.
- The single-workload's entire win (base-filter card ~1.33 -> ~1.08) is a ~20-25%
  input-cardinality tightening that is **dwarfed** by the ~10-1000x join-level
  error, so the planner's join sizing/ordering barely moves (8/143 structurally).

=> The causal chain is **refuted as a general mechanism for this carry-over
deployment**: the sub-plan (single-table) benefit does *not* extend to the
correlated multi-join regime. To fix joins you would need stats on the *join
keys / cross-table correlated columns* (a different, join-aware model), which is
explicitly out of scope for this single-table-extended-stat selection system and
is a clean statement of its benefit boundary. (Runtime timing was therefore not
worth measuring: join plans barely change, so runtime cannot improve broadly.)

Artifacts: `results/e2e_migrate_b_L1_B80000.json`; analysis
`scratch/e2e_migrate_b.py`, `scratch/probe_migrate_b.py`.

# Reporting Convention — 报告口径（分母 = candidate-bearing）

**状态:已定 2026-09-06。** 本文件是"所有 workload 级统计(mean / geo / p90 / max / 尾 / n_unrepaired)
以哪个查询集合为分母"的唯一权威约定。论文、各类 experiment driver、后续 scripts 统一遵守。

## 规则
1. **报告分母 = candidate-bearing(arity-2,存在 2 列候选)的查询集合**——
   即语料目录 `results/per_lambda/<bench>/<backend>/` 下实际存在的 `<qid>.json` 文件所对应的那一批
   query(代码上 = `measure_lambda_io.list_qids` / `optimize_lambda.load_lambda_problem` 的枚举范围)。
2. 进一步排除 `truth == 0`(est/0 q-error 无定义)与 NaN/缺该 level baseline 的查询。
3. no-candidate 查询(无 2 列候选)与 truth==0 查询都**不充当分母**,也**不进优化问题**(measurement side
   四个 driver `measure_sgrid / measure_census_parallel / measure_dmv_parallel / measure_stceb_parallel`
   都 `if not cands: continue`,不产出这类文件)。
4. 三 bench 永远并列/对比时,每个 bench 标 "over N candidate-bearing / M total(ratio)",分母透明。

## 三 bench 分母实际值
| bench | 总 query M | 报告分母 N (candidate-bearing) | 另排除 truth=0 | no-cand 占比 | 语料文件 |
|---|---|---|---|---|---|
| census | 468 | **467** | 0 | 0.2% | results/per_lambda/census/postgres (query.*.json) |
| dmv | 1965 | **1926** | 2 (dmv.173, dmv.943) | 2% | results/per_lambda/dmv/postgres (dmv.*.json) |
| stats_CEB_single | 632 | **180** | 0 | **71.5%** | results/per_lambda/stats_ceb_single/postgres (st.*.json) |

> 备注:dmv 有效分母常写作 ~1924(1926 − 2 truth=0)。

## 不对称警示(必须遵守)
census/dmv 覆盖近全(≥98%),排除的 no-cand 对报告无实质影响(它们本就多为准/无相关对象)。
**stats_CEB_single 只对 180/632 ≈ 28.5% 的子集报告** —— 其余 452 条无候选、多为易/准查询被整体移除。
因此 stats_CEB_single 的 mean 是"该工作负载中能用 ext-stat 改进的那部分"的优化视图,不是整盘 workload
的改善幅度。**跨 bench 并列时不可把 stats_CEB_single(28.5%)与 census/dmv(~100%)当作同构整盘量直接比**;
若要整盘对照,额外补一行 "no-cand 以各自 baseline 计入" 的全量口径。

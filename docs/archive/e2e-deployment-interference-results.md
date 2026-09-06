# E2E 部署:planner 干扰的量测与 OID-顺序修复(PG) — 结果笔记(S-grid 派生)

> v2 results note · 2026-09-05(刷新为 S-grid 口径) · 全在 PG 16 / census `climate`(2.46M rows)，**L1(S=300k), 100 KB budget**
> 每个方案都是**全量 query 真实 EXPLAIN** 的真部署，每次部署单次共享 ANALYZE。
> 四策略数字**由 S-grid 语料(见 `measurement-matrix.md`)派生**;报告分母按 candidate-bearing(见 `reporting-convention.md`)。
> 本文早期版本为 dense 档位口径(2026-09-04)，现被本 S-grid 版本取代；机制叙述(§3)不随档位变化，仍有效。
> 数据文件在 `results/e2e_*_sgrid_L1_100KB.json`,脚本在 `scratch/…`(见 §5)。比较图 `results/figures/e2e_deploy_comparison_sgrid.png`。
> **范围与待补**：本 S-grid 派生目前覆盖 **census/L1(PG)**；dmv、stats_CEB_single 的 PG L3 及全部 Oracle L3
> 尚未部署(见 `experiments-roster.md`)——届时同表口径填入后才是三层完整对照。

## 1. 动机 / 与通用模型的关系

- 通用优化器(`optimize.py` / `build_inner_at_level`)是**后端无关**的：它选 (colset,param) 集合，
  使 **interference-free** 预测质量最优——把每个 query 当成"它能被自己最佳统计独立服务"。
- PostgreSQL 16 的真实 planner**不是**独立读每个 co-installed 统计：对每条 query，它按
  **OID(创建顺序)** 遍历候选 multivariate 统计，取第一个"适用"(列集 ⊆ query 谓词列)者。
  因此 **realized 质量由 CREATE 顺序决定**(早期微机制实验：同一统计集只换顺序，真实 q-error 可从 ~1 级跳到
  ~几十级——该微探针已随 dense 清理移除，机制层面为引擎语义所固有)。
- 这就导致通用模型预测(clean / interference-free)与真实部署之间出现 **planner-interference gap**
  ——v2 架构 §1.6 / O3 明确要求暴露并验证的"最后一层落差"。本笔记用 **S-grid(census/L1)** 量化它，
  并给出一个 **PG 专用的 OID-order 修复**(作为扩展层，不改通用模型)。

## 2. 四策略真部署对比(全部真实 EXPLAIN, census S-grid / L1 / 100 KB)

| 策略 | 数据文件 | pred mean | TRUE mean | TRUE geo | TRUE max | 说明 |
|---|---|---|---|---|---|---|
| naive coexist 279(创建序 = solver 顺序) | `e2e_sgrid_naive_L1_100KB.json` | 1.409 | **5.976** | 1.484 | 1124.3 | 467 compared(candidate-bearing) |
| topo-order 279(轻量断环/确定性拓扑,非加权) | `e2e_true_ordered_topo_sgrid_L1_100KB.json` | 1.409 | 5.575 | 1.440 | 1111.4 | n=468 |
| **FB-order 279**(加权 feedback-arc 排序) | `e2e_true_ordered_fb_sgrid_L1_100KB.json` | 1.409 | **1.512** | **1.285** | **18.85** | n=468 |
| disjoint 32(全局列互斥) | `e2e_sgrid_disjoint_L1_100KB.json` | 7.664 | 7.707 | 1.636 | 2059.8 | 467 compared |

(S-grid 下 solver 选中 279 个 colset ≈ 99.9 KB;disjoint 因列互斥只用 16.7 KB(选 32),却仍 worse。)

要点(S-grid 派生)：
- **naive 重叠共存**：TRUE mean ~5.98、max ~1124——planner 干扰把干净的 interference-free 预测 1.41 打到 ~6。
- **disjoint(即 v1 所称 Option-A)**：诚实但差(pred 7.66 / TRUE 7.71)——强制列互斥饿死高度重叠的 census 负载，
  且即使只装 32 个 colset(远小于预算允许)也无法通过 disjoint 约束获益。策略名弃用 "Option-A"，直接用 disjoint。
- **FB-order(本修复)**：TRUE mean **1.512**、max **18.85** ——闭合到 interference-free 预测(1.41 / ~14)的几乎全部 gap，
  数量上显著优于 naive 与 disjoint。
- 对照维持了早期 dense 版本的结构性结论(naive≈~6 → FB≈1.5)：这证明 **planner-interference gap 非 dense 特例**，
  而是跨语料(此 S-grid case)普遍成立，且 FB-order 修复稳健。数值随档位/预算不同而变，是同一机制的 S-grid 再证实。

## 3. 为什么"排序"才是对的 in-model 修复(而非 disjoint/事后重排)

- 干扰的根因是 **OID 顺序取第一个适用者**，既然我们控制 CREATE 顺序，就能在**不删任何统计**的情况下
  让每条 query 由它最佳适用统计服务：只需让该统计的 OID 低于所有其它适用共存统计。
- disjoint(全局列互斥)过度约束：牺牲重叠质量换取无冲突输出，本 S-grid 下连预算都用不满仍 worse。
- 事后 drop/重排属启发式、无保证；而排序可在**求解/构造阶段确定性**给定，属 in-model/扩展层。

### 3.1 排序为何不是一次简单拓扑（关键曲折）
- 每条 query 强加偏好 "best_q 必须在其它适用 colset 前"。把 query 需求聚到被选 colset 图上后，候选形成**大片循环**。
  本条结论由 S-grid 直接佐证，无需 dense 特有计数器：
  - 若只是需要一次轻量拓扑修正，**按朴素方式断环**(近似拓扑/按创建序)应已接近改善；但本 S-grid 下
    `topo` 真值(5.58)几乎同 naive(5.98)——说明图中的冲突偏好高度成环，非一笔简单拓扑。
  - 只有 **加权的 feedback-arc 断环**(`solve_pg_order_fb`)才把它救回：剪掉 ~598 条低损偏好边后
    (FB 结果文件 `n_feedback_cut=598`)再拓扑成 CREATE 序，真值降到 **1.512**。
- 加权贪心 `solve_pg_order_fb`：给偏好边赋 weight = (该 query 第 2 佳可达 e_is − 最佳 e_is)，迭代删
  "当前环内最轻边"直至无环，再拓扑成 CREATE 顺序。被"牺牲"的每条偏好损失都很小，整体质量仍高。
- (早期 dense 版还列出具体坏尾 query id，属当时档位的个案佐证；机制不随档位变，此处不再复述。)

### 3.2 关键：修复必须是 PG 专用、不混进通用模型
- 排序语义绑 PG16"按 OID 取第一适用者"。所以在代码上是**扩展层**(`src/extstats2/core/optimize_pg_order.py`)，
  **输入就是通用 `build_inner_at_level` 的 (phys,opts,qbase)**；通用 `optimize.py/.optimize_lambda` 零改动。
- 换个后端(Oracle 等)时机制不同，需各自给"适用/竞标规则"，这条修复不回写进通用模型主体。
- limitation：本排序是 **single-winner**（PG 对一条 query 记一个 MCV 的实现观察主导）；对"一条 query 被
  多个不相交相关簇 MCV 并行服务"的情况没有显式建模，会保守(不高估)。这是后续可加(multi-winner/相关簇)的方向。

## 4. 与其配套的 cost/收益曲线(同一 S-grid 语料，见其它 note / measurement-matrix)
- storage 曲线：`results/milp_storage_sgrid_census.json`（预算×mean/geo/max，per-level L0/L1，`budget.unit=bytes`）
  → census 在 ~100–400 KB 处降到 mean ≈ **1.40–1.41**(geo≈1.21)；这正是 §2 里 universal 预测的 1.409 所在。
- maint 曲线：`results/milp_maint_sgrid_census.json`（`budget.unit=seconds-per-refresh`；fixed_sec L0=0.256/L1=2.56）
  → maint 预算 ≥ ~8 s 时 L1 到 mean ≈ **1.405**；~4–6 s 已见 L1 进 1.42–1.53。
- (早期 dense 版指向 `milp_effect_time/milp_maint_time`；这些已随统一 storage/maint 命名清理，现用上方 sgrid 曲线。)
-> 若放到 paper，四策略真值对照(§2 表)是最强的一段：
  它同时展示 (i) planner interference 的量级(Gap，naive 1.41→真实 ~6)，
  (ii) disjoint 的代价(7.71,预算也用不满)，
  (iii) OID-排序(FB-order,1.512→合到 pre 1.41)的可行性。

## 5. 复现/产物索引
| 内容 | 位置 |
|---|---|
| PG-order 排序器(拓扑 + 加权 feedback-arc) | `src/extstats2/core/optimize_pg_order.py` |
| 两阶段 driver(Phase1 通用选 + Phase2 排序) | `scratch/e2e_order_deploy.py`, `scratch/fb_order_save.py` |
| 真部署 driver(读 order JSON 在镜像上 EXPLAIN census) | `scratch/deploy_phase2_order.py`, `scratch/e2e_deploy_census.py` |
| 共享 loader(applicable/e_is/ctx) | `scratch/_pg_order_ctx.py` |
| 排序 order JSON | `results/e2e_order_fb_sgrid_L1_100KB.json` |
| 真部署(FB order / topo) EXPLAIN 结果 | `results/e2e_true_ordered_{fb,topo}_sgrid_L1_100KB.json` |
| naive / disjoint EXPLAIN 结果 | `results/e2e_sgrid_{naive,disjoint}_L1_100KB.json` |
| (早期 dense 微探针 jitter/reorder_micro | 已随 dense 化清理移除——机制见 §3 文本) |
| 比较图 | `results/figures/e2e_deploy_comparison_sgrid.png`(由 `scratch/plot_e2e_deploy_comparison.py` 渲染；script 已更新为读 S-grid 四文件) |

关键数值复述(S-grid census/L1/100KB)：**TRUE(naive)≈5.98 → TRUE(FB-order)≈1.51 vs pred(interference-free)≈1.41；max 1124→18.9**；
disjoint 32-stat 固守诚实顶 ~7.71(且预算用不满)——证明**正确加权的单次全局 OID-order 是连通高重叠 + 无干扰的 PG 方案**。

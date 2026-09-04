# E2E 部署:planner 干扰的量测与 OID-顺序修复(PG) — 结果笔记

> v2 results note · 2026-09-04 · 全在 PG 16.15 / census `climate`(2.46M rows, L1, 100 KB budget)
> 每个方案都是**全 468-query EXPLAIN** 的真实部署，每次部署单次共享 ANALYZE。
> 提供的数据/脚本在 `scratch/…` + `results/…`(见文末)，图在 `results/figures/`。

## 1. 动机 / 与通用模型的关系

- 通用优化器(`optimize.py` / `build_inner_at_level`)是**后端无关**的：它选 (colset,param) 集合，
  使 **interference-free** 预测质量最优——把每个 query 当成"它能被自己最佳统计独立服务"。
- PostgreSQL 16 的真实 planner**不是**独立读每个 co-installed 统计：对每条 query，它按
  **OID(创建顺序)** 遍历候选 multivariate 统计，取第一个"适用"(列集 ⊆ query 谓词列)者。
  因此 **realized 质量由 CREATE 顺序决定**(见微实验 `reorder_micro.py`:query.274 同一统计集，
  只换顺序，真实 q-error 从 1.08 变到 70.5)。
- 这就导致通用模型预测(clean/oracle)与真实部署之间出现 **planner-interference gap**
  ——v2 架构 §1.6 / O3 明确要求暴露并验证的"最后一层落差"。本笔记量化它，并给出一个
  **PG 专用的 OID-order 修复**(作为扩展层，不改通用模型)。

## 2. 四策略真部署对比(全部真实 EXPLAIN 468 query)

| 策略 | 数据文件 | pred mean | TRUE mean | TRUE geo | TRUE max |
|---|---|---|---|---|---|
| naive coexist 282(创建序=solver 顺序) | `e2e_deploy_L1_100KB.json` | 1.404 | **6.170** | 1.488 | 1126.5 |
| topo-order 282(naive 断环的拓扑序) | `e2e_true_ordered_L1_100KB.json` | 1.404 | 5.784 | 1.439 | 1122.5 |
| **FB-order 282**(加权 feedback-arc 排序) | `e2e_true_ordered_fb_L1_100KB.json` | 1.404 | **1.505** | **1.282** | **18.6** |
| Option-A disjoint 33（全局列互斥） | `e2e_deploy_L1_100KB_disjoint.json` | 7.681 | 7.727 | 1.636 | 2065.5 |

图：`results/figures/e2e_deploy_comparison.png`（mean / geomean / max-log，pred vs TRUE 逐指标并列）。

要点：
- **naive 重叠共存**：TRUE mean 6.17、**max 1126**——planner 干扰把预测 1.40 打到 6+。
- **Option-A（disjoint）**：诚实但差(pred 7.68 / TRUE 7.73)——强制列互斥饿死高度重叠的 census 工作负载。
- **FB-order（本修复）**：TRUE mean **1.505**、max **18.6** ——闭合到 interference-free 预测(1.40 / 14)的几乎全部 gap，
  且数量上显著优于 Option-A。

## 3. 为什么"排序"才是对的 in-model 修复(而非 Option-A/B)

- 干扰的根因是 **OID 顺序取第一个适用者**，既然我们控制 CREATE 顺序，就能在**不删任何统计**的情况下
  让每条 query 由它最佳适用统计服务：只需让该统计的 OID 低于所有其它适用共存统计。
- Option-A(全局 disjoint)过度约束(它牺牲质量来换取无冲突/可组合输出)，不如排序保留全部重叠质量。
- Option-B(事后 drop/重排)是启发式、无保证；而排序可在**求解/构造阶段确定性**给定，属 in-model/扩展层。

### 3.1 排序为何不是一次简单拓扑（关键曲折）
- 每条 query 强加偏好 "best_q 必须在其它适用 colset 前"。把 query 需求聚到被选 colset 图上后，
  这个有向图**几乎是一个 273/282 巨型循环分量**(环强连通分量数=1)。
- **naive 断环**(Kahn 后把剩余大环按 index 任意 append)几乎无改善(TRUE 5.78≈naive 6.17)，一度误导成
  "排序无救"。**加权的 feedback-arc 断环**才把它救回来。
- 加权贪心 `solve_pg_order_fb`：给偏好边赋 weight = (该 query 第 2 佳可达 e_is − 最佳 e_is)，迭代删
  "当前环内最轻边"直至无环(共剪 ~608 条低损偏好边)，再拓扑成 CREATE 顺序。被"牺牲"的每条偏好损失都很小，
  整体质量仍高(TRUE 1.505)。
- 独立坏尾 query(274→~1.09、335→1.13、403→1.57、59→1.97、61→4.26)都被其最佳适用统计服务，
  从被 hijack 的 109/375/1126 级回到各自 mask 预测。

### 3.2 关键：修复必须是 PG 专用、不混进通用模型
- 排序语义绑 PG16"按 OID 取第一适用者"。所以在代码上是**扩展层**(`src/extstats2/core/optimize_pg_order.py`)，
  **输入就是通用 `build_inner_at_level` 的 (phys,opts,qbase)**；通用 `optimize.py/.optimize_lambda` 零改动。
- 换个后端(Oracle 等)时机制不同，需各自给"适用/竞标规则"，这条修复不回写进通用模型主体。
- limitation：本排序是 **single-winner**（PG 对一条 query 记一个 MCV 的实现观察主导）；对"一条 query 被
  多个不相交相关簇 MCV 并行服务"的情况没有显式建模，会保守(不高估)。这是后续可加(multi-winner/相关簇)的方向。

## 4. 与其配套的 cost/收益曲线(见其它 note)
本套装同属已完成的一套：
- 收益/求解时间：`results/milp_effect_time_L{0,1}.json` / 图 `results/figures/milp_effect_time.png`
  (storage budget vs mean/geomean/max & solve time，quality log-y 锚定 qerr=1)。
- 维护预算：`results/milp_maint_time.json` / 图 `results/figures/milp_maint_time.png`
  (maint budget vs quality; fixed L0=0.256 s / L1=2.56 s 的门槛)。
-> 若放到 paper，四策略真值对照(表 / e2e_deploy_comparison.png)是最强的一段：
  它同时展示 (i) planner interference 的量级(Gap)，(ii) Option-A 的代价，(iii) OID-排序（本扩展）的可行性。

## 5. 复现/产物索引
| 内容 | 位置 |
|---|---|
| PG-order 排序器(拓扑 + 加权 feedback-arc) | `src/extstats2/core/optimize_pg_order.py` |
| 两阶段 driver(Phase1 通用选 + Phase2 排序) | `scratch/e2e_order_deploy.py`, `scratch/fb_order_save.py` |
| 共享 loader(applicable/e_is/ctx) | `scratch/_pg_order_ctx.py` |
| 排序 order JSON | `e2e_order_fb_L1_100KB.json` |
| 真部署(FB order)EXPLAIN 结果 | `e2e_true_ordered_fb_L1_100KB.json` |
| 微机制证据(query.274 reorder) | `scratch/reorder_micro.py` |
| 图 | `results/figures/e2e_deploy_comparison.png` |

关键数值复述：**TRUE(naive)6.17 → TRUE(FB-order)1.505 vs pred 1.404；max 1126→18.6**；
Option-A 33-stat 固守诚实顶 7.73——证明**正确加权的单次全局 OID-order 是连通高重叠 + 无干扰的 PG 方案**。

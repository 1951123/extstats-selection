# 部署 (deploy) — 从选择到真值：planner 干扰、四策略与引擎边界

> 本文档记录**选好统计之后怎么部署得到真值**（L3 层）：在镜像库上把 model 选的集合
> **真建出来、真 EXPLAIN**，对照 model 的 interference-free 预测。它量化
> planner-interference gap、评估"乐观叠加 vs 诚实 disjoint vs 修复排序"等策略，并
> 交代引擎/数据边界。
> 数值均自 `results/e2e_*_sgrid_*.json` 重派生；测量/优化见
> [`measure.md`](measure.md) / [`optimize.md`](optimize.md)。

## 1. 问题：model 预测为何不等于真值

optimize 的 model 假设每个 query 由它最佳统计**独立服务**（interference-free）。
真实 PG planner 不是这样：对一条 query，它按 **OID(创建)顺序**遍历候选多列统计，
取**第一个适用**者。于是当多个适用（列集⊆query 谓词）统计共存时，谁被用取决于
创建顺序 → **realized 质量由 CREATE 顺序决定** → 出现 model(clean) 与真实部署之间的
**planner-interference gap**。

对策：既然能控制 CREATE 顺序（扩展层`optimize_pg_order`，不改通用 model），就可以
在**不删除任何统计**的前提下排出一个顺序，让每条 query 由它最适合的统计服务——
关键是要正确断掉"近似巨型环"的冲突偏好（见 §3）。

## 2. 部署配置（本文件四策略均在此口径）

- bench: census（owner `climate`, S-grid 语料，L1=300k 档）、budget **100 KB**、
  level L1, `results/e2e_*_sgrid_L1_100KB.json`；每次部署在镜像上一次共享 ANALYZE,
  全量真实 EXPLAIN（candidate-bearing 分母）。
- 报告分母 = candidate-bearing(见 measure §5)：naive/disjoint 文件
  `n_queries_compared=467`；FB/topo 文件 `n=468`（含 1 条非 candidate-bearing 兜底）。

## 3. 五类真值档与四策略命名

实验中出现的真值文件与语义（文件名直连 results/）：

| 策略 | 语义 | 结果文件 |
|---|---|---|
| naive(coexist,创建序=solver 顺序) | 不修干扰 | `e2e_sgrid_naive_L1_100KB.json` |
| topo-order | 轻量确定性断环 | `e2e_true_ordered_topo_sgrid_L1_100KB.json` |
| **FB-order** | 加权 feedback-arc 断环后部署 | `e2e_true_ordered_fb_sgrid_L1_100KB.json` |
| disjoint(列互斥;旧称 Option-A) | 直接全局列互斥不重叠 | `e2e_sgrid_disjoint_L1_100KB.json` |
| （order JSON 中间件） | 排序解(供 deploy) | `e2e_order_{fb,topo}_sgrid_L1_100KB.json` |

（"+true_ordered" = deploy_phase2 真 EXPLAIN；"+order" = Phase2 排序解本身。）

## 4. 四策略 census 真值对照（S-grid 重派生）

| 策略 | pred mean | TRUE mean | TRUE geo | TRUE max |
|---|---|---|---|---|
| naive coexist | 1.409 | **5.98** | 1.484 | 1124.3 |
| topo-order | 1.409 | 5.58 | 1.440 | 1111.4 |
| **FB-order** | 1.409 | **1.51** | **1.285** | **18.85** |
| disjoint(32 colset, 16.7KB) | 7.66 | 7.71 | 1.636 | 2059.8 |

图：`results/figures/e2e_deploy_comparison_sgrid.png`。

结论（S-grid）：
1. **interference gap 是真实的**：interference-free 预测 1.41，naive 重叠共存却落到
   TRUE ~5.98 / max ~1124——不排序则 planner 干扰吃掉几乎全部增益。
2. **简单 topo 不够**：topo(l~5.58)≈naive(5.98)，说明冲突偏好高度成环，一棵普通
   拓扑顺序救不回。**加权 feedback-arc 才闭合**：FB-order TRUE 1.51/max 18.85，
   回到 model 预测 1.41 的接近全 gap。
3. **disjoint(列互斥)诚实但差**：pred 也诚实(=7.66)，TRUE 7.71，且因互斥只装 32
   colset/16.7KB 用不满预算——用它求"无冲突输出"的代价是牺牲重叠收益。
   （策略名弃 "Option-A"，就叫 disjoint。）

## 5. 为什么排序是对的解（机制，取自 architecture §3 原则 + 本节实证）

- 根因是"OID 顺序取第一适用者"；既然我们控制创建顺序，就可让每条 query 的最佳
  适用统计 OID 最低，而不删任何统计。
- 每条 query 强加"best 必须排在其它适用者前"，聚成候选图后冲突高度成环；naive
  断环(top近似创建序)≈naive，证明需要**加权** feedback-arc：
  - `solve_pg_order(order_fb)` 给偏好边赋 weight≈(第2佳可达 − 最佳可达)，迭代删环
    内最轻边至无环（census 剪 ~598 条低损偏好边，`n_feedback_cut=598`），再拓扑成
    创建顺序。被牺牲偏好损失很小 → 真值 1.51。
- 局限：单 winner 模型（PG 一条 query 主要由单个 co-installed MCV 服务）；对"一条
  query 被多个 disjoint 相关簇并行服务"没显式建模，会保守（不高估）。多-winner/相关
  簇为后续方向如 architecture 所述。

## 6. 引擎与数据边界（哪些结论只是 census/PG/L1 的）

- 本节的数字结论**只覆盖 census cluster、PG、L1、100 KB**。跨 bench/跨后端/跨档
  尚未有 S-grid 重派生的 L3：
  - dmv、stats_ceb_single 的 PG L3、以及 Oracle 的 L3 → **待部署/待测**（measure 表
    的覆盖缺口同步）。
  - "单表 extstats 迁移到多表 join workload 的有效边界"（早期 stats_CEB 多表观察）
    属**历史需重派生**项，本文档不嵌入其旧数（遵循"不引用 archive、结论重派生"
    约定）：要给出目前可信的四策略边界，需在 stats_CEB(多表) 的 S-grid 语料上重跑
    deploy 才有真值主张。

## 7. 产物索引

| 项 | 位置 |
|---|---|
| 排序器 | `src/extstats2/core/optimize_pg_order.py` |
| 排序/部署 drivers | `scratch/e2e_order_deploy.py`, `fb_order_save.py`, `deploy_phase2_order.py`, `e2e_deploy_census.py` |
| 共享 ctx loader | `scratch/_pg_order_ctx.py` |
| 四策略真值 JSON | `results/e2e_{naive,disjoint,true_ordered_*}_sgrid_L1_100KB.json` |
| 比较图 | `results/figures/e2e_deploy_comparison_sgrid.png` |

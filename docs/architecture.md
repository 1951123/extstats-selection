# 通用化扩展统计选择 — 设计 (architecture)

> 本文档只描述**设计思路**：方法为何、目标是什么、系统如何分层、关键机制与
> 取舍背后的理由。它**不承载具体实验结论**——测量 / 优化 / 部署三个阶段的
> 语料、方法与结论分属 [`measure.md`](measure.md)、[`optimize.md`](optimize.md)、
> [`deploy.md`](deploy.md)；本文档引用它们而非内嵌其数值。
>
> 旧的 v2 设计/实验笔记在 `docs/archive/`，仅作历史。本文档是自 S-grid 起点的
> 干净设计入口，不搬运 archive 里的确数；确数一律经 `results/` 重派生。

## 1. 我们在解决什么问题

底层任务：对给定 workload $Q=\{q_1,\dots,q_n\}$，选择并部署一组某引擎的
**扩展统计**（多列相关），使得在预算内 workload 的基数估计误差尽量小。

workload 上一个可行解的"理想真值"= 实际部署后在真 query 上实测的误差。其中
至少三个维度同等重要：

- **质量(q-error)**：$q_i$ 估计基数与真实基数的偏差；
- **存储成本**：统计对象占用的字节；
- **维护成本**：统计刷新一次的代价（PG `ANALYZE` / Oracle `GATHER_TABLE_STATS`）。

我们不打算用"在整个 workload 上穷举部署 + 实测每个解"的白盒做法，因为它在
两点上不可行：(a) workload 级测量贵，(b) 解空间爆炸（列组合 × 采样档）。

## 2. 方法：query-level 预测量 + 结构化模型 + 高效优化

绕开 (a)(b) 的三层结构：

1. **query-level 预测量（便宜）**：对每个候选统计 `s`、每条 query `q_i`，在
   **s 单独激活**时测该查询的 q-error `e_is`，及该统计的存储/维护成本。它只
   量"单统计对单查询"的孤立影响，不量整组部署。
2. **结构化模型**：给定部署集合 $T_i$，用模型把逐 query 的 `e_is` 合成该查询
   的估计误差。默认取**乘性近似**（log 空间可加），并受剪枝约束（同一表内
   选中列不重叠、稀疏部署）保护"独立性"——独立性是乘性可加成立的前提。
3. **高效优化**：当模型具上述结构性质后（稀疏→目标线性化），写成 MILP 用
   `scipy.optimize.milp` 求**模型内全局最优**，不靠启发式搜索。

一句话：用便宜 query-level 预测量 + 有结构性质的合成模型 + 结构优化，并以
**端到端真值验证**守住"模型最优 ≠ 真值最优"这一最后落差。

## 3. 关键设计理据与取舍

- **one-stat sufficiency 是"数据现象"，不是普适公理。** 大量坏 query 的误差由
  *单个（够深的）主导相关簇*驱动，修复它通常一个设计良好的 2 列统计就够——
  这在部分 bench/数据上成立（如 census、stats_CEB_single），在 DMV 上则是边界
  反例（部分坏尾需更高 arity/更多列，arity-2 + cap=1 修不动）。因此报告以**在该
  投影（引擎单 MV 语义 + arity-2 候选 + cap=1 部署）下的可达结果**为口径，而不把
  one-stat 当普适理论主张。
- **`per_query_cap=1` 是精度档，可选。** 通用问题允许每 query 选 k≥0 个统计；
  cap=1 只是默认的效率-精度折衷（目标线性化、保持稀疏可解）。后端/数据需要时可
  放开到 K（目标非线性化，属独立工作）。
- **planner 干扰是独立性的反例，靠"排序"修复、且需 PG 专用。** 独立性的前提
  是选中统计列不重叠；但真实 planner 按 OID 顺序取第一个适用统计，重叠共存时会
  出现干扰。修复必须是**扩展层**（决定 CREATE 顺序的排序器），不能回写进通用模型
  ——详情见 `deploy.md`。
- **跨引擎统一是有条件的，由"结构性质契约"保证。** 不同引擎（PG 扩展统计 vs
  Oracle 列组）的统计机制不同，但可用"稀疏性/独立性"两个决定性维度把优化器归到
  少数 MILP 类；成本结构与目标聚合是类内实例参数。不满足决定性契约的后端落到它
  能支持的最强优化器类，而非被当作"同模型"硬跑。
- **capacity 是"采样轴 λ × 表示轴 param"，二者关系引擎相关。** 采样多深（λ）
  是跨引擎的物理量；表示多细（param）绑后端对象形态（PG 的 `statistics_target`
  一维标量、受 Chaudhuri floor `S≥300·target` 限制；Oracle 的表示多为引擎自决的
  桶/结构、无 engine floor）。因此 capacity 在抽象层被命名/解码为 (λ,param) 两轴。
  关于 λ-param 晶格与 PG/Oracle 差异的机制叙述与引擎细节在 `measure.md`。

## 4. 系统分层

```
src/extstats2/
├── core/       数据库无关算法核心：candidates(列组合) · measure(调度) ·
│               optimize(MILP) · build/estimate(经 backend 抽象)
├── backend/    后端抽象：base(接口/性质契约) · capabilities(统计能力模型) ·
│               postgres(PG16) · oracle(23ai)
├── plan/       测量协议：protocol_a(通用逐候选隔离) · protocol_m(mask 加速, 仅 PG)
├── bench/      基准加载 census/stats_ceb(stats_CEB)/stats_ceb_single + 镜像管理
├── cli.py / config.py
```

**分层原则**
- `core/` 只依赖 `backend.base` 的抽象接口（类型/契约），不直接 import 具体引擎。
- `backend/` 实现接口并**声明结构性质**（决定 core 选哪类 optimizer）。
- 数据库差异（DDL、目录、EXPLAIN 读数、采样语义、mask 支持）全部封装在 backend；
  算法核心可测试、可扩展新引擎。
- 各 bench 的**镜像多库并行测量**属实验基建，归 `measure.md` 协议层，不进核心。

## 5. 与三类生命阶段文档的分工

| 本文档 | measure.md | optimize.md | deploy.md |
|---|---|---|---|
| 设计思路 / 为何这样解 | 怎么测(S-grid 语料/db 覆盖/协议/fidelity) | 测好之后怎么选(budget × quality 曲线/argmin) | 选好之后真部署(干扰/四策略/引擎边界) |
| 机制原则(剪枝/λ-param/契约) | 每 bench 每 db 语料口径 | storage/maint 曲线数值与分析 | L3 真值数值与结论 |
| 承接方法 | 承接 query-level 测量 | 承接结构化模型 | 承接端到端验证(O3) |

> 引用约定：本文档把机制性"是否/为什么"讲清；数值性"是多少、复现见哪"一律
> 指向 `measure/optimize/deploy` 与 `results/`。

# 通用化扩展统计选择 — 设计 (architecture)

> **模型的真相源在 [`model.md`](model.md)**：变量（`S` 轴、`p` 表示、`λ` 派生）与
> 因果方向以那里为准。本文档是 model 的一个**视图**，只描述设计思路；
> 方法为何、目标是什么、系统如何分层、关键机制与
> 取舍背后的理由。它**不承载具体实验结论**——测量 / 优化 / 部署三个阶段的
> 语料、方法与结论分属 [`measure.md`](measure.md)、[`optimize.md`](optimize.md)、
> [`deploy.md`](deploy.md)；本文档引用它们而非内嵌其数值。
>
> 旧的 v2 设计/实验笔记在 `docs/archive/`，仅作历史。本文档是自 S-grid 起点的
> 干净设计入口，不搬运 archive 里的确数；确数一律经 `results/` 重派生。

## 1. 我们在解决什么问题

底层任务不是"随便选一些统计"，而是：**在 workload $Q=\{q_1,\dots,q_n\}$ 上部署一组
某引擎的扩展统计（多列相关），使某一代价最小化**。一个可行解的价值由把它**实际部署
到真 query 上之后**直接测出的误差给出，至少含三个同等重要的维度：

- **质量(q-error)**：$q_i$ 估计基数与真实基数的偏差；
- **存储成本**：统计对象占用的字节；
- **维护成本**：统计刷新一次的代价（PG `ANALYZE` / Oracle `GATHER_TABLE_STATS`）。

> 注：由于统计基于**采样**，同一解即便在同一 workload 上重测也会带估计噪声；这是
> 真值本身的抖动。本设计不把这种抖动当误差遮掩，而在测量层以 **λ/fidelity** 显式
> 记录并保守化（见 [`measure.md`](measure.md) §1.2），优化层再消费之（`optimize.md`）。

### 1.1 朴素基线为何不可行：枚举 + workload-wide 实测

最直觉的思路是白盒穷举：把每个可行解都实际建出来、跑一遍 workload、读真误差，
取最小：
$$\min_{\text{可行解 } S}\, \text{workload-measure}(S).$$

这在概念上给出"真最优"，但在**两处独立崩溃**：

- **(a) workload 级测量贵。** 每评估一个候选解，都要把它在真 workload 上逐个建立
  统计、逐条跑 query、比对真值。代价随 query 数 × 候选数近似地线性~更坏增长。
- **(b) 枚举本身不可行。** 解空间是"选哪些 `(表, 列集)` + 各自采样/档级"的组合爆炸
  ——仅单数据集可见的候选即数千组、再乘档数，全局可行解呈天文数字。

### 1.2 只修 (b) 的补救——黑盒搜索——仍不解决本质

一种误以为便宜的路是把 workload 级实测当"询价 oracle"，靠搜索（贪心/局部/进化）
在少量询价后逼近：**搜索只绕过了 (b) 枚举，没有解决 (a) 测量贵**。每一次"询价"
仍是一次完整且昂贵的 workload 级实测——只要询价单价高，搜索依然慢。把工作重心放
在"如何让单次询价便宜"才是根本。

## 2. 方法：query-level 预测量 + 结构化模型 + 高效优化

**核心动作是把贵的询价从 workload 级"降维"到 query 级**，再合成、再在模型上优化：
每一层的"为什么"都锚定在避开 (a)/(b)：

1. **query-level 预测量（便宜，直接改进 (a)）**：对每个候选统计 `s`、每条 query `q_i`，
   在 **s 单独激活**时测 `q_i` 的 q-error `e_is`，以及该统计的存储/维护成本。它只量
   "单统计对单查询"的孤立影响——远便宜于"部署一整套后实测"，因其不复用整组状态、
   无需为每个全局解付费。
2. **结构化合成模型（把 query 级 `e_is` 变成 workload 级估计）**：给定部署集合 $T_i$，
   用模型合成查询 $i$ 的估计误差。默认取**乘性近似**（log 空间可加）
   $$\log e_i(T_i)\approx\log e_i^0+\sum_{s\in T_i}\log\frac{e_{is}}{e_i^0},$$
   并受**剪枝**保护"独立性"（同一表内选中列不重叠 + 稀疏部署）——独立性正是乘性
   可加成立的前提（§3）。
3. **高效优化（在模型上求最优，改进 (b)）**：当模型具上述结构性质后（稀疏→目标
   线性化），问题写成 **MILP**（创建 $y_s$、选用 $x_{is}$ 二值），用
   `scipy.optimize.milp` 求**模型内全局最优**，不靠搜索求局部。枚举爆炸随即消失。

一句话：**用便宜的 query-level 预测量 + 有结构性质的合成模型 + 结构优化**，绕开
(a)(b)；并因"模型最优 ≠ 真值最优"显式留出端到端真值验证（§3 / `deploy.md`），守住
最后一层落差。

形式化（符号与完整约束）见 `optimize.md §1.1/§1.2`；本页只讲为何这样设计。

## 3. 关键设计理据与取舍

- **one-stat sufficiency 是"数据现象"，不是普适公理。** 大量坏 query 的误差由
  *单个（够深的）主导相关簇*驱动，修复它通常一个设计良好的 2 列统计就够——
  这在部分 bench/数据上成立（如 census、stats_CEB_single），在 DMV 上则是边界
  反例（部分坏尾需更高 arity/更多列，arity-2 + cap=1 修不动）。因此报告以**在该
  投影（引擎单 MV 语义 + arity-2 候选 + cap=1 部署）下的可达结果**为口径，而不把
  one-stat 当普适理论主张。
- **`cap` = 1 或 >1 是"两 regime"的切分（由 optimizer class 承载,不是自由开关）。**
  通用问题允许每 query 选 k≥0 个统计;但两个 regime 数学不同,故 API 各自锁死:
  当 cap=1 时用 `SPARSE_LINEAR`（精确算术均值,`solve_ilp` 在入口强制
  `per_query_cap==1`,否则 `ValueError`）;当 cap>1/None 时用 `MULTIPLICATIVE`（几何
  surrogate）。不存在"把 sparse 允许 cap=K"这种中间态。
- **cap>1 用 Option-A 语义：独立性要求同 query 内选中的列不重叠。** 乘性/几何
  surrogate（cap>1 或 None）是**独立性模型**——把"同一 query 用的统计"当作可加独立的
  前提是它们不共享列（§3 planner 干扰反例即为反对）。因此只要走乘性解码，就在
  **每一 query 上加列重叠互斥行**（即便显式给了 cap=K>1 也一并加），不靠 cap=1 兜底。
- **乘性 surrogate 有下界 ≥1 的线性行（q-error 物理下界）。** 几何 surrogate
  $\hat e_i=e_i^0\prod_s(e_{is}/e_i^0)^{x_{is}}$ 理论上可被多个强统计一起推到 <1，但那
  是物理上不可能的 q-error；在幂对数空间里
  $\sum_s \log(e_{is}/e_i^0)\,x_{is}\ge -\log e_i^0$ 是**每 query 一条线性行**，保证
  solver 优化到的 surrogate ≥1，从而 solver objective 与最终 decode 一致（不在解后补
  `max(·,1)`）。
- **planner 干扰是独立性的反例，靠"排序"修复、且需 PG 专用。** 独立性的前提
  是选中统计列不重叠；但真实 planner 按 OID 顺序取第一个适用统计，重叠共存时会
  出现干扰。修复必须是**扩展层**（决定 CREATE 顺序的排序器），不能回写进通用模型
  ——详情见 `deploy.md`。
- **跨引擎统一是有条件的，由"结构性质契约"保证。** 不同引擎（PG 扩展统计 vs
  Oracle 列组）的统计机制不同，但可用"稀疏性/独立性"两个决定性维度把优化器归到
  少数 MILP 类。**目标聚合不是可选实例参数**，而是由类/档唯一决定：cap=1 →
  sparse-linear（精确算术均值）、cap>1/None → multiplicative（几何均值 surrogate，
  带 ≥1 的 floor）；`worst`/`p90`/`geo` 只是求解后的 **evaluation metrics**。不满足
  决定性契约的后端落到它能支持的最强优化器类，而非被当作"同模型"硬跑。
- **capacity 是"采样轴 λ × 表示轴 param"，二者关系引擎相关。** 采样多深（λ）
  是跨引擎的物理量；表示多细（param）绑后端对象形态（PG 的 `statistics_target`
  一维标量、受 Chaudhuri floor `S≥300·target` 限制；Oracle 的表示多为引擎自决的
  桶/结构、无 engine floor）。因此 capacity 在抽象层被命名/解码为 (λ,param) 两轴。
  关于 λ-param 晶格与 PG/Oracle 差异的机制叙述与引擎细节在 `measure.md`。

## 4. 系统分层

```
src/extstats2/
├── core/       数据库无关算法核心：candidates(列组合) · measure_sampling(S-grid/
│               sample-first 测量, 含协议-M) · optimize(MILP 双regime) ·
│               optimize_sgrid(外层 S-grid 选择 + 内层 representation MILP) ·
│               predicates/queries · maint_fit / maint_model(维护费建模)
├── backend/    后端抽象：base(接口/性质契约) · capabilities(统计能力模型) ·
│               postgres(PG16) · oracle(23ai)
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

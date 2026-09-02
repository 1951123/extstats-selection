# extended-stats-optim-v2 — 通用化架构设计

> 状态: **设计阶段 (draft)**。本文档是 v2 代码库的蓝图，实现前请先 review。
> 目标: 把 v1 (`extended-stats-optim`) 的贡献从 **PostgreSQL 专属** 泛化到
> 多个数据库后端（首期 **PostgreSQL + Oracle**），同时保留 v1 的三项核心成果：
> (1) one-stat sufficiency；(2) Protocol-A / Protocol-M 测量；(3) budgeted ILP 分配。

---

## 1. Methodology: 为什么是 "query-level 预测量 + 结构化模型 + 高效优化"

> 本章是整个 v2 方法的**第一性论证**：从最原始的 workload-level 优化问题出发，
> 逐步收缩到本项目采用的方案。每一层"为什么这么设计"的答案都锚定在本章的字面
> 命题上，v2 的分层（§2 之后）是本章结论的工程化表达。

### 1.1 最原始的优化问题 (workload-level)

我们面对的底层任务不是"选一些统计"，而是：

> **在 workload $Q = \{q_1, \dots, q_n\}$ 上部署一组统计，使某个代价最小化。**

一个**可行解**就是一个要在 workload 上实际部署的统计集合。workload 上每个可行解
的**真实价值**由直接在 workload 上的实测给出，包含至少三个维度：

- **q-error** —— 部署后各查询的基数估计误差（如 $\max(\text{est},\text{act})/\min(\text{est},\text{act})$）；
- **存储成本** —— 统计对象占用的磁盘空间；
- **维护成本** —— 统计的构建/刷新代价（对 PG 是 ANALYZE，对 Oracle 是
  GATHER_TABLE_STATS，均受容量参数影响）。

> 注：由于数据库统计基于**采样**，同一解即使在相同 workload 上重测也会引入
> 一定的估计误差；这是真值本身的噪声，先不展开（见 §1.7 的开放点清单）。

### 1.2 朴素基线：枚举 + workload-level 实测

最朴素的方案是把问题当作一个**白盒穷举**：

$$
\min_{\text{可行解 } S} \; \text{workload-measure}(S),
\qquad \text{对每个 } S \text{ 做一次 workload-level 实测}
$$

这在概念上给出"真最优解"，但在**独立的两处**崩溃：

- **(a) workload-level 测量非常贵。** 对每个候选解，都要在真实 workload 上
  逐个建立统计、逐个跑查询、读取基数并比较真值。一轮的成本随查询数与候选数
  线性甚至更坏增长。
- **(b) 枚举本身计算不可行。** 解空间是"选哪些 `(table, columns)` 组合 + 各自
  容量级"的组合爆炸。仅 Census 全局去重即有 19245 个候选组合，每个又有多档
  容量；全局可行解因而更是天文数字（候选分布详见 v1 文档 §1.1）。

朴素方案因此既不便宜、也不可能遍历。

### 1.3 只修 (b) 的黑盒搜索：仍不解决 (a)

如果我们**只**试图绕过枚举爆炸，一个自然想法是**黑盒搜索**：把 workload-level
实测当作一个"询价 oracle"——不再逐个枚举，而是靠某种搜索（贪心、局部搜索、
进化等）在少量询价后逼近最优。

> 关键判断：**搜索只解决了 (b) 枚举，完全没有解决 (a) 测量贵。** 搜索的每一次
> "询价"仍然是一次完整、昂贵的 workload-level 实测；搜索只是试图用更少的询价
> 次数换取一个次优但可接受的结果。只要询价单价高，搜索就依然是慢的。

### 1.4 本项目方案：query-level 预测量 + 结构化模型 + 高效优化

我们把"贵的询价"从 workload-level **降维到 query-level**，再用一个**数学模型**
把 query-level 的预测量合成成 workload-level 表现的估计，最后在**模型上跑高效
优化算法**（而非搜索）。三层结构：

1. **query-level 预测量**（便宜）：对每个候选统计 `s`、每个查询 `q_i`，在
   **一次统计单独激活**的情形下测出 `q_i` 的 q-error `e_is`，以及该统计的
   存储/维护成本。这一步的测量成本远低于"在整个 workload 上部署一组统计后
   实测"——它只测单个统计对单个查询的孤立效应。

2. **结构化模型的 workload-level 估计**：给定要部署的统计集 $T_i$，用模型把
   query-level 的 `e_is` 合成成查询 $i$ 的估计 q-error，再取 workload 均值/最坏等
   作为整体目标。v1 采用**乘性近似**（log 空间可加）：
   $$
   \log e_i(T_i) \;\approx\; \log e_i^0 + \sum_{s \in T_i} \log\!\Big(\frac{e_{is}}{e_i^0}\Big),
   $$
   $\{y_s\}$ 控制创建哪些物理统计，$\{x_{is}\}$ 控制查询 $i$ 选择哪些，存储预算
   与维护成本进入约束/目标。

3. **高效优化算法**：当模型具备特定**结构性质**时，上述问题可写成整数线性规划
   （$y_s, x_{is} \in \{0,1\}$），用现成的 MILP 求解器（`scipy.optimize.milp`）
   求得**模型内全局最优**，而不是靠搜索求局部。

这同时绕开了 (a) 与 (b)：
- 测量只做 **query-level**（单统计、单查询），不再为每个全局可行解付费；
- 求解走 **结构化优化**，不在天文数字的解空间里搜索。

### 1.5 模型的性质：可计算性的来源，与"用剪枝换可信度"

**核心论点**：我们的"高效优化算法"之所以可行，完全依赖模型的**结构性质**。
这些性质不是白送的，而是通过**有意识地剪枝解空间**换来的：

- **独立性 / 可加性。** 乘性近似的有效性前提是"各统计对查询的影响独立"；
  v1 用**强制 query 内列不重叠**（约束 3）来保障，使 log 空间真正可加、
  模型成为可解的 MILP。缺少此性质则模型失真或不可解。
- **稀疏性（one-stat sufficiency）。** v1 的最终模型取更强的特例
  `per_query_cap = 1`（每个查询**最多选一个**统计）。在该限制下目标从乘性
  近似**退化为精确线性**（无需 log 变换、无近似误差）：
  $$
  e_i(T_i) = e_i^0 - \sum_s (e_i^0 - e_{is})\, x_{is},
  \qquad \sum_s x_{is} \le 1.
  $$
  这条性质的**经验来源**由 v1 文档
  `docs/extended-statistics-selection.md` §5.5 的机制实验支撑：单表选择谓词上，每个查询的
  误差被**一个主导相关列簇**驱动，一个最佳列组合的 MCV 已捕获几乎全部增益；
  加入第二、三个不重叠候选几乎无增量，甚至会因 **planner 干扰**（joint
  interference）反而变差。这一性质在 Wide 的 Census 宽表与 stats_CEB_single
  上都验证成立（k=1/2/3 结果相同），排除了"小候选空间假象"。

> **设计洞察:为保留可计算性 + 真实性，我们主动地把可行解集限制在模型可信的
> 子集上（稀疏 + 列不重叠）。** 换句话说，优化算法的"精确性"是对**模型内**的
> 精确，而模型对**真值**的忠实，取决于我们是否愿意把解空间剪到模型可信区。

### 1.6 模型与真值的差距：为什么需要端到端验证

由于 query-level 模型估计的是"若独立性成立则 workload 表现"，一旦独立性
被破坏（尤其 **planner 干扰**），估计即失真。因此"模型内最优"仍须用
**workload-level 真值**核对：

- v1 专门做了 **mask-vs-true 验证**（检查 query-level mask 测量与真实建统计后
  的 q-error 是否一致）以及**端到端验证**（把 ILP 选出的集合真建出来、真测
  workload q-error）。这些就是在"模型估计 vs workload 真值"之间建立桥梁。
- 这是"模型估计最优"与"真实最优"之间的最后一层落差，必须显式留出一个验证
  阶段，而不是假设模型永远可信。

### 1.7 三个开放点（后续设计需显式处理）

- **[O1] 维护成本：已完成可加近似建模（含已知 max 语义偏差）。** 原始问题三个维度里，
  v1 的 MILP 只有 q-error（目标）与存储成本（约束）。v2 现已加入**部署后刷新**
  维护成本：每个物理统计带 `maint_cost`，作为与存储并列的**硬预算约束**
  $\sum_s m_s y_s \le M$（`backend.maintain_cost()` → `core/measure.py` 记录 →
  `core/optimize.py` 的 `maint_budget`）。两点明确：
  - **可加近似**：PG 实测 ANALYZE 成本由 `targrows`（= 整表最大 target）决定，是
    **max 语义**而非可加。当前按用户决策采用可加近似以保持线性可解，max 语义的
    偏差作为已知近似记录（后续可精化为带指示变量的分段约束）。
  - **语义区分**：`maint_cost` 是部署后一次刷新的代价（进 ILP）；测量阶段的
    实验成本（protocol 每轮 CREATE/ANALYZE/EXPLAIN）**不入模型**，二者刻意分离。
  - 目标保持纯 q-error，维护成本仅作硬约束（与存储预算并列）。
- **[O2] planner 干扰是有条件成立的独立性的反例。** 模型独立性靠剪枝（列不重叠
  + 稀疏）来保护；但真实规划器在"非稀疏、重叠统计共存"时可能违反它。设计应
  明确"模型可信区"的边界，并让剪枝约束与其对齐。
- **[O3] 验证阶段不可省略。** 模型的最优 ≠ 真值的最优；端到端/mask-vs-true 验证
  是 v2 的必要阶段，而非可选。

### 1.8 对 v2 分层的直接含义

本章的结论直接决定了 §2 之后的代码分层：

- **query-level 预测量** → `core/measure.py`（协议选择：Protocol-A 通用 /
  Protocol-M 加速）+ `plan/`（协议驱动）。
- **结构化模型 + 高效优化** → `core/optimize.py`（MILP，直接移植 v1；
  模型性质即可解性的锚点）。
- **模型可信度/验证** → `core/verify.py`（端到端对照，见 O3）。
- **跨数据库等价测量** → `backend/` 抽象（使同一套 query-level 测量与模型在
  PG/Oracle 上成立）。MySQL/其他引擎同理，只须实现 backend 接口。

> **一句话总结：** 我们不用"贵的 workload-level 白盒询价"，也不用"把昂贵询价
> 包起来靠搜索节省次数"的黑盒，而是用**便宜的 query-level 预测量** + **有结构
> 性质的合成模型** + **结构优化算法**，并以端到端验证守住"模型 vs 真值"的最后
> 落差——这正是 v2 如何从 workload-level 原始问题出发、在"测量贵 + 枚举爆炸"
> 双重约束下落到一个可计算且可信的方案。

### 1.9 待决矛盾：估计模型与优化模型的跨后端一致性

> **状态：立场已拍板（软选择乙+甲 + 目标结构立场 B），接口已落地 `69315b9`。** 本节
> 记录 v2 的一个 **核心架构矛盾**及其消解路径。它直接决定 `core/optimize.py` 是
> "单一共享 MILP"还是需抽象成"可挑选优化器"，以及 backend 接口是否需为此增加
> "结构性质契约"。
>
> **已落地的代码映射（2026-09-02）：**
> - `backend/base.py`：`StructuralProps` 数据类（决定性维度 `sparse_one_stat` /
>   `disjoint_supported`；实例维度 `maint_structure` / `capacity_model` /
>   `supports_objectives`）+ `Backend.structural_props()` 契约方法。
> - `core/optimize.py`：`OptimizerClass`（`SPARSE_LINEAR` / `MULTIPLICATIVE`）、
>   `select_optimizer_class(props, objective)` 软选择 + `solve_ilp(optimizer_class,
>   objective)`；PG 默认 `sparse_linear`（`per_query_cap` 语义下线性精确解码），
>   默认 `multiplicative` 向后兼容。
> - 不支持的 objective → `select_optimizer_class` 抛 `ValueError`（不静默用错模型）。

**矛盾。** 方法论 §1.4-1.5 的核心是：query-level → workload-level 的**估计模型**
必须具备某些结构性质（独立性、稀疏性），高效优化算法才存在。但若不同 DBMS 的
统计机制（PG 扩展统计 vs Oracle 列组）导致**估计模型结构不同**，那么"什么性质
成立"就不同 → **可用优化算法不同** → 优化模型变成 DBMS 特定。这与 v2"核心跨
后端共享"的目标直接冲突。

**消解分析的证据。** 这个矛盾并非不可消解，关键是区分两层性质：

- **稀疏性（one-stat sufficiency）很可能是 DBMS 无关的数据属性。** 它由*相关
  性结构*而非*统计机制*驱动：一个查询往往由一个主导相关列簇决定误差，修复它
  通常一个设计良好的统计就够。v1 §5.5 的 generality check 特意在最宽、最高
  误差、候选最密的 Census 上验证，正是为排除"这是 PG/小候选空间假象"的可能。
  因此稀疏性大概率能跨后端延续。
- **独立性需靠剪枝"买"，剪枝可由后端承诺。** PG 里独立性也不是无条件成立
  （v1 §5.9 实证 planner interference），是靠 `global_disjoint`（选出统计全局
  列不相交）剪枝获得的。Oracle 列组机制不同，但只要后端**承诺也能提供
  "稀疏 + 列不相交"的剪枝**，估计模型就落入相同结构类 → 优化模型可共享。

**消解路径（结构性契约，软选择）。** 把"统一性"从*当然成立的 postulate* 降为
*由后端性质校验保证的有条件统一*。契约是**软选择**而非硬拒绝：契约不拒绝后端，
而是让核心为每个后端**选择它能支持的最强优化器**：

- **共享层（优化模型的"类"统一）**：所有后端都落在 **MILP 这个类**内。优化器
  "类"只由决定性维度（稀疏性、独立性）分成两种：**稀疏线性 MILP** 与
  **通用乘性 MILP**；目标聚合与成本结构作为所选类的**实例参数**（min-max 是在
  该类上加 $t$ 变量、Oracle fixed_only 是表级固定项等），不构成新优化器类。
  这样"统一" = "所有后端都用 MILP 类 + 决定性维度决定两种形式"，既比"每个 DBMS
  一套优化器"统一，又比"所有后端用同一个精确模型"诚实。
- **后端层（结构性质契约）**：后端通过 `structural_props()` 暴露其估计模型与
  成本模型的性质。**核心先按"决定性维度"（稀疏性、独立性/不相交）选出优化器
  "类"（稀疏线性 MILP vs 通用乘性 MILP），再把成本结构与目标聚合作为该类 MILP
  的*实例参数*填充——不是另选优化器。** 契约维度分两类：

  **决定性维度（决定优化器"类"）：**

  1. **稀疏性**（`sparse_one_stat: bool`）——该后端能否支持"每查询单统计即可捕获
     主导相关"，使目标退化为**精确线性**（§1.5）。PG：是（已实证）。Oracle：待验证。
     不满足 → 落在**通用乘性近似** MILP 类（仍是 MILP，但目标表达式不同）。
  2. **独立性/不相交**（`disjoint_supported: bool`）——能否通过"选出统计全局列
     不相交"保证联合效应独立（免 planner 干扰 / 使 $e_i(T_i)$ 合成可信）。PG：是
     （`global_disjoint` 实测预测比 1.000）。Oracle：机制不同，待验证。它决定该类
     MILP 要施加的剪枝约束集合。

  **实例参数维度（不改变优化器"类"，只填充约束/目标形态）：**

  3. **成本结构**（`maint_structure ∈ {fixed+var, fixed_only, ...}`、`capacity_model`）
     ——维护成本是"固定+可加变动"还是"纯固定"，容量是"每统计"（PG target）还是
     "每表/每次采样"（Oracle estimate_percent）。这决定存储/维护预算约束的具体形式，
     是同一个 MILP 内约束的实例化。
  4. **协议/测量**（`protocol_m: bool`，已有）——决定测量成本与可行性；间接影响
     能否廉价获得 query-level 预测量（属测量层，不影响优化器结构）。

- **目标函数结构：同一个共享 MILP 的可参数化变体（已拍板：立场 B）。** 当前目标
  是最小化 workload 平均 q-error，但"平均"只是多种聚合之一。**聚合方式不改变
  优化器"类"**——它只改变共享 MILP 的目标函数形态（同一类，实例不同）：
  - **算术均值**：目标逐查询可分离（$\min \sum_i e_i$），与稀疏模型结合 → 线性，
    MILP 友好。
  - **几何均值**：等价于 $\min \sum_i \log e_i$（单项仍是线性的 log-improvement），
    仍可进 MILP（等价地换聚合即可，同实例家族）。
  - **最坏情况（min-max）**：$\min \max_i e_i$，需引入共享变量 $t$ 与逐查询约束
    $e_i \le t$，但仍是对**同一个** MILP 的线性化实例化，不是新优化器类。
  - **p 分位**：通常**不可**直接线性化，需要专门处理 —— 即便如此，也只是该实例
    家族的一个受限成员，不构成独立优化器类。
  因此目标**聚合方式**是一个 core/CLI 的实例参数（`objective='mean'|'geomean'|
  'worst'|'p90'`），后端通过契约声明"它在哪些聚合下能提供可信的估计"
  （`supports_objectives`），二者匹配后，聚合被填入**已被决定性维度选定的那个
  共享 MILP 类**的目标函数 —— **优化器"类"由维度 1/2 决定，聚合/成本只是实例化**。

**校验（软选择语义）。** 核心在初始化时读取 `structural_props()`，**先用决定性
维度（稀疏性 + 独立性）为后端选定优化器"类"**，再把目标聚合与成本结构作为该类的
实例参数填充：

- 稀疏 + 不相交 → **稀疏线性 MILP 类**（最快、共享）。
- 不支持稀疏（但不相交） → **通用乘性 MILP 类**（同一 MILP 族，目标为 log 乘性）。
- 在这两类之上，目标聚合 (`mean|geomean|worst|p90`) 与成本结构 (存储/维护预算)
  作为**实例参数**：min-max 就在该类上加 $t$ 变量与 $e_i \le t$ 约束；Oracle 的
  `fixed_only` 维护就以表级固定项实例化，均不另选优化器。
- 契约缺项或冲突 → 明确报错，迫使后端显式声明能力，而非静默用错模型。

**代价。** 统一性从"免费假设"变为"由契约校验保证的有条件统一"，并需为核心
`optimize` 与 backend 接口增加契约校验与优化器类选择。**已拍板立场**：软选择
（乙+甲）+ 目标结构立场 B——**以乙为框架（契约 + 优化器类选择抽象），以甲为默认
（默认走共享稀疏 MILP），目标聚合与成本结构是该类的实例参数（不另选优化器）**，
不满足决定性契约的后端不是被拒绝，而是被分配到它能支持的优化器"类"。

---

## 2. 动机与背景

v1 是一个高质量的研究工具，其 **算法核心**（候选生成 → 测量 → 预算分配）与
**数据库实现细节**（DDL 语法、系统目录、规划器输出、采样语义）并未解耦，导致
难以迁移到其它数据库。具体耦合点（已在 v1 仓库笔记中详述）：

| 耦合点 | v1 位置 | 依赖的 PG 机制 |
| --- | --- | --- |
| 测量 (Protocol-A) | `measure.py` | `CREATE STATISTICS` / `ANALYZE` / `EXPLAIN (FORMAT JSON)` / `DROP STATISTICS` |
| 测量 (Protocol-M) | `measure_mask.py` | `pg_statistic_ext_data` 目录、`pg_mcv_list` 类型、`ALTER STATISTICS ... SET STATISTICS`、`statext_is_kind_built()` |
| 统计类型 | `stats.py` | PG 的 `dependencies` / `ndistinct` / `mcv` 三类 |
| 估计提取 | `estimate.py` | `EXPLAIN (FORMAT JSON)` 的 `Plan Rows` 键 |
| 谓词解析 | `predicates.py` | `sqlglot(read="postgres")` |
| DB 连接 | `db.py` / `config.py` | `psycopg` + libpq |

v2 的核心思想：**把"数据库系统是什么"与"统计选择算法做什么"解耦**，通过一个
**后端抽象层 (backend abstraction)** 表达数据库差异，让算法核心在任意实现该接口
的数据库上运行。

---

## 3. 架构总览

```
src/extstats2/
├── core/                     # ★ 数据库无关的算法核心（v1 可移植，不 import 任何后端）
│   ├── queries.py            #   通用查询表示（= v1 BenchQuery，已通用）
│   ├── predicates.py         #   基于 sqlglot 的谓词列提取（去掉 PG dialect 硬编码）
│   ├── candidates.py         #   候选列组合生成（列集合层面，与语法无关）
│   ├── build.py              #   把"统计能力请求"翻译成可执行对象（跨后端）
│   ├── measure.py            #   测量调度：给定一个后端与候选，产出 per-(cand,capacity) q-error
│   └── optimize.py           #   ★ MILP 预算分配（直接移植 v1 optimize.py，它本就通用）
│
├── backend/                  # ★ 后端抽象层（"数据库系统"）
│   ├── base.py               #   抽象基类 Backend / Capability / StatObject / Estimate
│   ├── capabilities.py       #   统计能力模型（跨后端的统一概念）
│   ├── catalog.py            #   目录模型：统计对象清单、大小、payload 访问（抽象化 v1 的 pg_statistic_ext_data）
│   ├── postgres.py           #   PG16 后端（移植 v1 的 Protocol-A + Protocol-M 作为加速）
│   └── oracle.py             #   Oracle 后端（DBMS_STATS column groups + extended histograms）
│
├── plan/                     #   测量协议调度（Protocol-A 通用；Protocol-M 后端专属优化）
│   ├── protocol_a.py         #   通用逐候选隔离
│   └── protocol_m.py         #   mask 协议（仅 PG；通过 backend.measure_flags 声明支持）
│
├── bench/                    #   基准加载（通用查询 + ground truth 格式）
│   └── loaders.py            #   census / stats_ceb / stats_ceb_single 等 loader（沿用 v1 parsers）
│
├── cli.py                    #   CLI 入口（bench -> measure -> optimize -> verify）
└── config.py                 #   路径 / 后端选择 / 预算等配置
```

**分层原则：**
- `core/` 只依赖 `backend.base` 的**抽象接口**（类型标注），绝不直接 import `postgres` / `oracle`。
- `backend/` 提供接口各实现。核心通过工厂（如 `config.get_backend(name)`）获取实例。
- 这样 `core/` 的可测试性高（可 mock backend），新增数据库只需新增一个 `backend/<db>.py`。

---

## 4. 统计能力抽象 (Capabilities)

v1 把 PG 的 `dependencies/ndistinct/mcv` 写死。v2 把它们提升为**跨后端的统计能力
(capability)**。每个能力描述"统计能捕获哪种相关关系"，各后端映射到自身的实现：

| 能力 (core 概念) | 语义 | PG16 映射 | Oracle 映射 |
| --- | --- | --- | --- |
| `dependency` | 函数依赖（列子集→列） | `CREATE STATISTICS (dependencies)` | 无直接等价（可基于 `sys_op_dtf...` 或忽略） |
| `ndistinct` | 列组合的 distinct 组合数 | `CREATE STATISTICS (ndistinct)` | `DBMS_STATS` column group 估算 |
| `mcv` | 列组合上的 most-common-values | `CREATE STATISTICS (mcv)` | **column group statistics** (`DBMS_STATS.CREATE_EXTENDED_STATS` / `GATHER_TABLE_STATS` 的 `METHOD_OPT` 中声明) |

**关键设计决策：** 每个后端声明它**支持的能力集合** `backend.supported_capabilities()`
与每项能力的**容量参数模型**。这样"容量"（capacity）这个核心算法维度也被统一抽象：

- PG: 容量 = `statistics_target`（整数，`SET default_statistics_target` / `ALTER STATISTICS ... SET STATISTICS`）。
- Oracle: 容量 ≈ 采样比例 `ESTIMATE_PERCENT`（`DBMS_STATS`，如 `AUTO_SAMPLE_SIZE` = 100%）与直方图桶数 `BUCKETS`。

```python
# backend/capabilities.py
@dataclass(frozen=True)
class Capability:
    """A cross-backend statistical capability."""
    name: str                      # "dependency" | "ndistinct" | "mcv" (core names)
    # Each backend maps this to its own object/DDL. 语义由各后端解释。
    native_kind: str               # e.g. PG: "dependencies"; Oracle: "column_group"
    # Capacity model: how 'capacity' is materialised in this backend.
    capacity_param: str            # e.g. "statistics_target" | "estimate_percent"
```

core 只谈论 `Capability`（`dependency/ndistinct/mcv`）和**抽象的容量值**
（归一化如 `0..1` 的采样强度或级别索引），由后端换算成具体参数。这使
`core/optimize.py` 的存储预算模型（`cost_bytes`）依然成立——每个后端必须能报告
某 (capability, columns, capacity) 的**字节成本**。

---

## 5. 后端抽象接口 (base.py)

所有后端实现的最小接口（编辑权限最小可生存接口）。目标是接口小、表述稳定，
因为 core 依赖它。

```python
# backend/base.py
class Backend(Protocol):
    # ---- 元信息 ----
    def name(self) -> str: ...
    def supported_capabilities(self) -> list[Capability]: ...
    def has_protocol_m(self) -> bool: ...          # 是否支持 catalog-mask 加速

    # ---- 生命周期 / DDL（抽象掉 CREATE STATISTICS / DBMS_STATS）----
    def create_stat(self, obj: StatObject) -> None: ...
    def drop_stat(self, obj: StatObject) -> None: ...

    # ---- 构建（抽象掉 ANALYZE / GATHER_TABLE_STATS）----
    def build_stats(self, objs: list[StatObject], capacity: Capacity) -> None: ...

    # ---- 估计（抽象掉 EXPLAIN Plan Rows / DBMS_XPLAN Cardinality）----
    def estimate(self, query: BenchQuery) -> Estimate: ...

    # ---- 目录（抽象掉 pg_statistic_ext_data / ALL_STAT_EXTENSIONS / USER_TAB_COL_STATISTICS）----
    def stat_size_bytes(self, obj: StatObject) -> int: ...
    def list_stats(self, table: str) -> list[StatObject]: ...

    # ---- 隔离测量（抽象掉 Protocol-A / Protocol-M）----
    def isolate(self, keep: set[StatObject], table: str) -> "IsolationCtx": ...
    # 通用实现 = Protocol-A（建→测→删→重建）。PG 覆写为 Protocol-M（mask）。
    # IsolationCtx 负责 finally 恢复；core 无需知道内部用哪种协议。

    # ---- 采样/容量换算 ----
    def capacity_to_native(self, cap: Capacity) -> Any: ...
class Estimate:          # 抽象掉 "Plan Rows"/"Cardinality" 键
    estimate: int
    raw: Any
    actual: Optional[int]
class StatObject:        # 抽象掉 PG 统计对象命名/列、Oracle 隐藏列
    table: str
    columns: tuple[str, ...]
    capability: Capability
    capacity: Capacity
    name: str            # 后端生成的可读写标识（PG 对象名 / Oracle 扩展名）
class IsolationCtx:      # 记录要恢复的后端状态；with 块退出时 backend.restore()
```

**协议选择策略：**
```python
def measure_candidates(backend, query, cands, protocol=None):
    if protocol is None:
        protocol = "m" if backend.has_protocol_m() else "a"
    # "a" 总是通用可用；"m" 是可选加速
```

---

## 6. 后端细节

### 6.1 PostgreSQL (`backend/postgres.py`)
- 直接移植 v1 的 Protocol-A (`measure.py`)、Protocol-M (`measure_mask.py`)、
  `estimate.py`、`stats.py`、`catalog` 查询。
- 唯一变化是把函数签名切到抽象基类，SQL 字符串留在后端内部。
- `has_protocol_m() == True`（这是 PG 独有的加速，暴露为可选能力）。

### 6.2 Oracle (`backend/oracle.py`)
- **统计对象**: 用 Oracle 的 **column group statistics**（12c+ 通过
  `DBMS_STATS.GATHER_TABLE_STATS` 的 `METHOD_OPT -> FOR COLUMNS (a,b,c)`，
  或用 `DBMS_STATS.CREATE_EXTENDED_STATS` 显式创建扩展统计）。每个列组产生一个
  系统命名的隐藏列（如 `SYS_STU...`），可在 `USER_TAB_COL_STATISTICS` /
  `ALL_STAT_EXTENSIONS` 查到。
- **构建**: `DBMS_STATS.GATHER_TABLE_STATS(ownname, tabname, estimate_percent=..., method_opt=>'FOR COLUMNS (a,b) SIZE ...')`。
- **估计**: `EXPLAIN PLAN FOR <sql>` + `DBMS_XPLAN.DISPLAY`，从计划中读
  `Cardinality`（输出基数）字段。COUNT 查询需同样重写为 `SELECT *` 读取过滤后基数。
- **目录**: `USER_STAT_EXTENSIONS` / `USER_TAB_COL_STATISTICS`（`NUM_DISTINCT`、`NUM_BUCKETS`）读取大小/容量。
- **隔离协议**: Oracle 无 catalog-mask（不能 NULL 掉一列统计而不影响其它），故
  `has_protocol_m() == False`，退化为 **Protocol-A**（建→`GATHER_TABLE_STATS`→`EXPLAIN PLAN`→删→重建）。
- **能力映射**: 主要是 `mcv`(column group histogram) 与 `ndistinct`(group)；
  `dependency` 标记为不支持（或以后用基于关联分析的自定义统计补充）。
- 连接用 `python-oracledb`。

---

## 7. 核心算法 (core/) — 可复用 v1 的部分

| 模块 | v1 来源 | 改动 |
| --- | --- | --- |
| `optimize.py` | 直接移植 | **无改动**（MILP 模型本就通用：budget + overlap-free + shared-resource） |
| `candidates.py` | 移植 | 从"列集合"层面泛化（本就用 `(table, columns)` 表示，无需改） |
| `predicates.py` | 移植 | 去掉 `read="postgres"`，改用通用 dialect 或后端提供的 dialect |
| `estimate.py` | 抽象化 | 移入 backend：core 只调用 `backend.estimate()` |
| `queries.py` | = v1 `BenchQuery` | 不变 |

**MILP 模型不动**（v1 已验证 + v2 扩展）：目标 $\sum_i w_{is}x_{is}$，约束
(1) 存储预算 $\sum_s c_s y_s \le C$，(2) 选择须已创建 $x_{is}\le y_s$，
(3) 查询内重叠禁止 $x_{is_a}+x_{is_b}\le 1$，(4) 同组合 level 互斥，
(5) 可选 global-disjoint，(6，v2 新增) **维护预算** $\sum_s m_s y_s \le M$
（`maint_cost` 可加，`maint_budget=None` 时不施加，向后兼容）。全部用
`scipy.optimize.milp`。

---

## 8. 配置与 CLI

```python
# config.py
@dataclass
class Config:
    backend: str            # "postgres" | "oracle"（可扩展）
    bench: str              # census | stats_ceb | stats_ceb_single
    budget_bytes: int
    capacities: tuple[...]  # 通用容量级别（core 侧）
    protocol: str | None    # None = 后端自动选 a/m
```

CLI 流程与 v1 一致：`generate → measure → optimize → verify`，但每一步都
面向 `Backend` 抽象。

---

## 9. 里程碑

1. **M1 — 脚手架**（本次）：目录、`base.py` / `capabilities.py` / `catalog.py` 抽象、
   `config.py`、README、本文档。`core/optimize.py` 从 v1 移植。
2. **M2 — PG 后端**：移植 v1 全部 PostgreSQL 逻辑到 `backend/postgres.py`，
   通过一条 census 冒烟测量验证 `core` 跑通 PG 路径（含 Protocol-M）。
3. **M3 — Oracle 后端**：实现 `backend/oracle.py`（column groups + Protocol-A），
   验证同一 `core` 无改动跑 census 或 stats_CEB。
4. **M4 — 交叉验证**: 同一份核心算法在 PG/Oracle 上对比结果，证明抽象层真正通用。

---

## 10. 未决问题 / 决策点

> 方法论层面的开放点见 §1.7 的 [O1]/[O2]/[O3]（维护成本建模、planner 干扰与
> 模型可信区、验证阶段）。本章列出的是**工程实现**层面的决策点：

- [ ] `dependency` 能力在 Oracle 是否实现，还是仅标记不支持？建议首期**不实现**
      （core 的 MILP 能处理"某后端不支持某能力"，只需把该能力候选权重置为无增益）。
- [ ] 容量归一化：core 用 `[0..1]` 采样强度还是级别索引？建议**级别索引**
      （如 `(0,1,2)` 映射到各自的原生级别），因为 PG 的 `statistics_target` 与
      Oracle 的 `estimate_percent`/`BUCKETS` 无线性可逆映射。
- [ ] q-error 定义与 zero 处理：保持 v1 语义（`max/min`，零侧用下限 1）。
- [ ] 连接层：`python-oracledb`（thin 模式）还是 `cx_Oracle`？建议 thin。
- [ ] 备份/恢复的并发安全：Protocol-M 备份表命名需 per-session 唯一（沿用 v1 约定）。

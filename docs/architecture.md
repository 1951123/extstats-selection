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

- **[O1] 维护成本：部署后刷新成本，Y-two-layer（表激活阶梯 + 每统计变动）。** 原始
  问题三个维度里，v1 的 MILP 只有 q-error（目标）与存储成本（约束）。v2 加入
  **部署后刷新**维护成本。要点：
  - **模型是"表激活 + 每统计"两层，不是"每统计重复计表固定"。** 物理上因
    `targrows`=max(target) 使一次 ANALYZE 采样共享，同一张表选 K 个统计也只付
    一次固定扫描。初版错误地把整表固定成本乘到每个选中统计重复相加；已修正为：
    `maint(S) = Σ_{表t} base(t, max level on t) [每表一次, 按最高档] + Σ_{s∈S} var(s)`。
  - **backend**: `table_maintain_tiers(table)`（每 target 档的固定 base；index=
    达到的最高 level）+ `stat_maintain_var(obj)`（每统计变动态）。`measure.py` 的
    `maint_cost` 现只承载 var。
  - **optimize**: `MaintProfile.table_base_tiers` 注入后，求解器加表激活/档位
    指示变量 $w_{t,\ell}$，维护预算 = 每激活表的阶梯固定 + 每统计 var（线性 MILP）。
    不传 `MaintProfile` 时退化为 additive fallback（向后兼容）。
  - **固定成本非线性（校准）**：ANALYZE 采样 `~min(300·t, 表行数)` 行 → 固定成本
    分段线性增长到全表扫描饱和点 $t_{sat}\approx N/300$。实测 Census `climate`
    校准 $w\approx0.00256$ s/target, `t_sat≈8194`；tiers [0.26,2.56,20.98] 复现
    实测 [0.25,2.59,20.55]。
  - **语义区分**：`maint_cost`/tiers 是部署后一次刷新代价（进 ILP）；测量阶段实验
    成本**不入模型**。目标保持纯 q-error，维护成本仅作硬约束。
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
│   ├── __init__.py           #   load_benchmark(name) 分发 + supported_benches
│   ├── census.py             #   Census loader（沿用 v1 parsers/census）
│   ├── stats_ceb.py          #   stats_CEB loader（v1 parsers/stats_ceb）
│   └── stats_ceb_single.py   #   stats_CEB 单表子计划 loader（v1 parsers/stats_ceb_single）
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
    supported: bool = True
    primary: bool = False          # core capability (repairs selection cardinality)
```

**收敛：MCV 是核心能力（`primary`）。** v1 实证表明，修复*选择谓词基数 (selection
cardinality)* 的 q-error 只有**多列值分布**这一能力（PG `mcv` / Oracle column-group
histogram）直接有效——`ndistinct` 改善的是 distinct-count 估算、`dependencies` 只
消特定函数依赖，对本目标的贡献有限/间接。因此 v2 把该能力标记 `primary=True`，
**默认测量（`measure_query` 不指定 capabilities 时）只 probe `primary` 能力**（见
`default_measure_capabilities`）。`dependency`/`ndistinct` 仍可声明/支持，但不作为
测量主力。

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

### 6.1 PostgreSQL (`backend/postgres.py`) — **已实现（M2，commit `a353cd5`）**
- 已移植：`estimate`（EXPLAIN JSON → Plan Rows）、`create/drop/build`（CREATE/
  DROP/ALTER STATISTICS + ANALYZE）、`stat_size_bytes`（pg_statistic_ext_data）、
  `list_stats`（stxkeys → 列解析）、`isolate`（**Protocol-A**，异常安全恢复）、
  `maintain_cost`（fixed+var 模型估计）、backend 自有 capacity ladder
  （level→statistics_target）。
- **Protocol-M（catalog-mask）为后续增强**：当前 `has_protocol_m() == False`，
  `isolate()` 走通用 Protocol-A（drop/rebuild）。这是 PG 独有的加速，待实现
  CatalogDriver over `pg_statistic_ext_data` 后改为 True 并启用 mask。
- 连接：psycopg 惰性连接，autocommit；`default_statistics_target` 由
  `set_capacity`/`build_stats` 控制。

### 6.2 Oracle (`backend/oracle.py`) — **已实现（M3）**
- **统计对象**: Oracle 的 **column group statistics**（12c+，通过
  `DBMS_STATS.GATHER_TABLE_STATS(..., METHOD_OPT => 'FOR COLUMNS (a,b) SIZE n')`
  构建）。每个列组在 `USER_STAT_EXTENSIONS` 里记一条，内部是系统命名的隐藏列
  （`SYS_STU...`），其直方图细节在 `USER_TAB_COL_STATISTICS`。扩展对象的身份是
  **列集合**（`DROP_EXTENDED_STATS` 按列表达式 drop），不是可写对象名——backend 的
  StatObject 因此按列集合识别、drop、list（与本层 Protocol-A 隔离的列集合匹配一致）。
- **构建**: `GATHER_TABLE_STATS(ownname, tabname, estimate_percent=<cap>,
  METHOD_OPT => 'FOR ALL COLUMNS SIZE AUTO FOR COLUMNS (...) SIZE <buckets>')`。
  一个表一次 GATHER 会在一趟采样里建好全部所需列组（**FIXED_ONLY / per_scan**），
  并**保留自然单列直方图**（SIZE AUTO）——这让"无列组的基线"与"加列组后"的测量都是
  在健康单列统计上进行的（M4 发现：若用 SIZE 1 会清掉单列直方图，造成虚假的大幅
  改善假象，见 §6.3a）。`mcv`(primary) 建列组+直方图（SIZE=buckets）；`ndistinct`
  只建列组（SIZE=1）。
- **估计**: 重写 `COUNT(*)`→`SELECT *`，`EXPLAIN PLAN SET STATEMENT_ID=<literal>` +
  `plan_table` 读根节点 `Cardinality`，读后按 statement_id 清理。注意 STATEMENT_ID
  必须是字符串字面量（bind 会 ORA-01780）；且 Oracle 不接受 `SELECT FROM`（缺表达式），
  故本后端重写为 `SELECT *`。基准 SQL 是 **PG 方言**（`FROM t AS a` 别名、`::type`
  强转），Oracle 原生不接受——estimate 前用 **sqlglot PG→Oracle 转译**（`AS` 别名去
  掉、`::timestamp`→`CAST(... AS TIMESTAMP)`），使 stats_CEB 的 PG 方言查询也能在
  Oracle 上 EXPLAIN。这是"基准 SQL 需要引擎可移植"这一跨后端一致性问题的一环。
- **目录**: `USER_STAT_EXTENSIONS`（存在/列解析）+ `USER_TAB_COL_STATISTICS`
  （隐藏列 `NUM_BUCKETS * AVG_COL_LEN` 作单调 size 代理；extension 是 CLOB，不能在
  SQL 里 `=` 比较，改在 Python 侧匹配列集合）。
- **隔离协议**: Oracle 无 catalog-mask（不能单独 NULL 掉一列），`has_protocol_m() ==
  False`，走 **Protocol-A**（建/`GATHER`/测/删/重建成按列集合匹配的版本）。
- **能力映射**: `mcv`(column group histogram, **primary**) 是修复 selection 基数
  q-error 的能力。它能否带来收益取决于查询：在已被自然单列直方图估准（base≈1.8）的
  近独立谓词上几乎无增益；在**多列强相关、极稀疏命中**的查询（query.62/184/61，
  base≈1000-4000）上能把 qerr 压到个位数（见 §6.3c）。`ndistinct`(group) 支持；
  `dependency` 不支持。
- **维护成本模型**: 列组共享一趟 GATHER 扫描 → `MaintStructure.FIXED_ONLY` +
  容量模型 `per_scan`（estimate_percent 是每次整表扫描的采样抽屉）。`table_maintain_tiers`
  按 CLIMATE(~2.46M) 标定：estimate 1%→0.54s、10%→2.10s、100%→21.7s（degree=1，按行数
  缩放）；`stat_maintain_var` ≈ 常数（无额外按统计的扫描）。
- 连接：python-oracledb thin，autocommit；owner = 当前 schema (SYSTEM)。基准表都以未加引
  号、大写形式匹配（Oracle 折叠未引号标识符为大写）。

### 6.3 跨后端交叉验证（M4）— 结论与教训

在 Census `climate`（PG `census` 与 Oracle `SYSTEM.CLIMATE` **同为 2,458,285 行**，
逐行一致）上，用同一份 benchmark 与同一套 `core/` 做交叉验证。

**(a) 测量假象修正：基线必须用"自然单列统计"。** 最初的 Oracle 基线报出 qerr≈32，
而 PG 只报 ~1.8 —— 看似引擎天差地别。追查发现这是 **Oracle 侧自造的假象**：本后端
`build_stats` 早期用 `FOR ALL COLUMNS SIZE 1`（不建单列直方图），反复 mcv gather 后
把表的单列直方图**全清掉**，于是"无扩展统计"基线被压到极差，再建列组"修"回来看起来
改善巨大，但大部分改善来自重建单列统计而非列组本身。**教训**：跨后端对比前，两端都必须
从各自引擎的*自然单列统计*出发（PG `ANALYZE`；Oracle `GATHER ... FOR ALL COLUMNS SIZE
AUTO at 100%`）；为此两后端都增加 `restore_natural_stats()`（PG 单列 ANALYZE，Oracle
整表 SIZE AUTO），Oracle `build_stats` 也改为保留自然单列直方图（SIZE AUTO）再加列组。

**(b) 修假象后，两引擎的自然基线逐查询对齐**（468 条 census 查询）：PG 平均 qerr
≈25.0、中位 1.27；Oracle ≈25.2、中位 1.28；log-qerr 的 Pearson 相关系数 ≈ **1.0**，
中位 PG/Oracle 比值 1.0 —— 同一份数据上两引擎的"单列统计估计质量"逐查询一致。最大
误差的查询（qerr 数百–数千）两引擎**完全相同**：query.184/465/62/61 等，均为
**多列强相关、且 combo 命中极稀疏行（truth≈13–107）**的查询。

这个对齐还**量化了"extended statistics 的价值边界"**（`cross_scale baseline`，
与两引擎**逐条完全相同、Jaccard=1.0** 的高误差查询集合）：

| base q-error ≥ | PG 条数 | Oracle 条数 | （两引擎集合一致） |
| --- | --- | --- | --- |
| 1 | 468 | 468 | 100%（整库 median≈1.28，多数本就够好） |
| 2 | 104 | 104 | 100% |
| 5 | 42 | 42 | 100% |
| 10 | 27 | 27 | 100% |
| 100 | 9 | 9 | 100% |
| 1000 | 4 | 4 | 100% |

即：468 条 census 里，只有 **22%（base≥2）~ 9%（base≥5）** 的查询单列统计没估准
、真正需要 extstats；且"哪些查询需要"由数据决定、引擎无关。这界定了后续"该为哪
些查询买列组/投预算"的靶子 —— 一个小的、引擎不变的尾部。

**(c) 主导列组引擎无关（one-stat sufficiency 的跨引擎证据）。** 对这些最坏查询，两端
各自独立枚举 2 列 mcv 列组并报告最优者（数值为复现脚本 `results/cross_focus.json`；
两者皆用引擎全表采样建列组）：

| 查询 (truth) | 自然基线 PG / Oracle | 主导对 (两引擎各自最优) | 非主导对照对 |
| --- | --- | --- | --- |
| query.62 (45) | 2054 / 2069 | **`(iRspouse,iWork89)`**：PG 45, Oracle 2.5 | 其余 ~1432–2069 |
| query.184 (13) | 4228 / 4162 | **`(iDisabl1,iRspouse)`**：PG 2.8, Oracle 1.3 | 其余 ~88–4162 |
| query.61 (107) | 1028 / 1035 | **`(iDisabl2,iYearsch)`**：PG 32, Oracle 1.1 | 其余 ~1001–1035 |

PG 与 Oracle **各自独立认定同一 2 列组是唯一能大幅修复该查询的主导组**（agreement=True
对所有三查询），其余列对在**两端都无效**（qerr 仍 ~10²–10³）。结论：**"哪个列组成的统计
值得买"是数据的列相关结构的性质，不是引擎实现的性质** —— PG 的 `mcv` 与 Oracle 的
column-group histogram 只是同一"多列值分布"能力的不同落地。这就是 §1.4 one-stat
sufficiency / MCV-core 收敛在跨后端意义上的实证支撑。（注：查询修复后 PG/Oracle 的绝对
主导列组行 qerr 略有差异，是两引擎采样与直方图像限差异；量级、主导列组与"非主导无效"的
结论完全一致。）

**(d) 规模化的 top-k 可修复性（PG，Protocol-A；`cross_scale topk`）。** 取自然基
qerr>5 的 top-20 条（PG 枚举全部 2 列 mcv，`statistics_target=100`）：

- **20 条里 10 条能被单一 2 列组修到 qerr≤5**（query.184 4098→4.1、query.62 2086→
  2.8、query.221 115→1.3、query.335 21→1.2、query.104 20→1.5、query.403 27→1.7 …）。
  这量化了 one-stat sufficiency 在更大样本上的胜率（高频：约一半强修正）。
- 另外 10 条其最优 2 列组仍 >5（query.465 修到 41、query.61 修到 45、query.382 修到
  15 …）—— 它们要么误差跨多个相关列对（需 ≥2 组）、要么在 2 列粒度下就修不动。
- **Oracle 抽验这 3 条**强烈一致：query.184 (dom `(iDisabl1,iRspouse)` Ora 1.3)、
  query.62 (`(iRspouse,iWork89)` Ora 2.5) 上 Oracle 复现了 PG 的量级；**query.465 是
  反例**——PG 的 `(iDisabl1,iYearsch)` 把 PG 从 2369 修到 41，但该列组在 Oracle 上
  几乎不动（仍 2363）。即：在更大样本上，**主导列组并非永远引擎间一致**；PG 独有的桶
  语义会让某个列组只对 PG 有效。这是 (c) 小样本结论在规模化时的诚实边界，也是 M4/后续
  要量化的"两引擎可修复集重叠度"，而非无条件的逐查询一致。

复现：`cross_focus`（小样本 3 条，perfect agreement）+ `cross_scale topk`（PG top-20 +
Oracle 抽验）。结果 JSON 落在 `results/`。

**(e) 测量↔部署采样一致性的实证（[O-采样]）**。测量某 extstat 时的容量档，是否与部署
时一致？在 PG 上对 query.62（truth=45，主导对 `(iRspouse,iWork89)`）做拆分实验，把
"扩展统计自身的目标"与"整表 `default_statistics_target`（基础单列重扫档）"两个旋钮分开
控制：

| 实验 (ext `SET STATISTICS` / base `default_statistics_target`) | qerr |
| --- | --- |
| baseline（自然，base=100，无 ext） | 2089 |
| A) ext 10000 / **base 100**（部署于自然 base） | 2.5 |
| B) ext 10000 / base 10000（当前 v2 测量语义：把全局档也抬到候选档） | 2.5 |
| C) ext 1000 / base 100 | 1.32 |
| C′) ext 1000 / base 10000 | 2.5 |

**结论，两条：**
1. **抬 base 档（B≈A）对候选本身量级基本无偏**：v2 测量时顺带把 `default_statistics_target`
   抬到候选档（保持全表 `targrows = max target` 的共享扫描假设）并不会改变候选的真实修
   复幅度（A 与 B 都 =2.5）。因此"测量把 base 重扫到候选档"不是偏差来源。
2. **真正的对照要锁在扩展统计自身的 `SET STATISTICS`（capacity level）**：ext-target
   1000→qerr 1.32、10000→2.5（非单调：对极稀疏 45 行目标，过采样反而引入噪声）。所以
   部署时必须**复现被选 stat 的 per-object target**（`ALTER STATISTICS ... SET STATISTICS`
   = 测量时该 capacity 档），否则测量选出的档在部署时是另一档、效果不同。

即：v2 的"capacity = 整表该档扫描 + 该对象 `SET STATISTICS` 同步设同值"在 PG 上自洽
（表级共享扫描假设由 MaintProfile 的 Y-two-layer 承担）；不一致风险收敛到"部署要复现
per-object target"，而非"要复现 base 档"。

**(f) 采样保真度（λ）契约 — v1 fidelity 纳入 v2（记录+契约阶段）。** v1 的
Sec.8 / query.184 已确证：一个稀疏驱动组合在每个容量档的期望采样次数
`λ = (truth/N)×sample_rows(level)` 决定该档读数是否可靠 —— λ≪1 时 MCV/列组直方图
**是否捕获该组合是随机二值** → 单次 ANALYZE 的 q-error **高方差**、Protocol-M 的低容量
数字系统性过于乐观；λ≳5 才保真。这在 v2 中作为跨后端"能力契约"落地：

- `Backend.sample_rows_per_level(table, level)`（PG ≈ `min(300·statistics_target, N)`；
  Oracle ≈ `estimate_percent%·N`）+ `Backend.num_rows(table)`；两引擎都实现。
- `core/measure.measure_query` 在**每个 (候选, level)** 输出新增 `lambda_expected`、
  `qerror_std`、`qerror_worst`（repeat>1 时）——把不确定度本身变成测量观测，而非只记
  均值。PG query.62 实测：L0 λ≈0.55 <1 → repeats [1.5,2.1,4.8] 高方差；L1 λ≈5.5 →
  低方差且最优（1.48）；L2 λ≈45 → 恒被捕获但非单调回落（2.5）。

当前为**记录与契约**阶段（选项③第一步）：λ/方差进入产出的 phase1 字段。让优化器消费
（如在 λ<1 档不自信/罚乐观、用 λ 或方差校正后收益而非均值）留作下一阶段；每个数据集/容量档
的敏感区间差异性（Census 小档、CEB 大档）也在该字段上可被探测，不必硬编码 ladder。

**(g) 采样 vs 统计"参数"的解耦（[O-sampling/param]）——概念上与 PG 的实现耦合。**
把统计对象的容量进一步拆成**两个独立轴**：
- **λ / 采样轴（capture）**：一次 ANALYZE 抽多少行，决定驱动组合能否被可靠采到、q-error
  是否可信（即 (f) 的 fidelity）。成本随它近似线性（扫描量）。
- **param / 表示轴（representation）**：在**给定样本**上，参数化摘要用多细去刻画（MCV 保留
  多少项、直方图多少桶）。它决定能把已采到的分布压到多低的表示误差（并通常影响存储）。

**实证（PG query.62，`(iRspouse,iWork89)`；`N≈2.458M`，全表饱和点 target≈`N/300`≈8193）：**

| statistics_target | ≈采样行 min(300·t,N) | MCV_items | qerr |
| --- | --- | --- | --- |
| 50 / 100 | 15k / 30k | 14 | 1.45 |
| 1000 | 300k | 15 | 1.29（此带最优） |
| 5000 | 1.5M | 18 | 2.81 |
| 8193（饱和）… 20000 | 全表（样本已封顶） | 19 | 2.5 |

**读法三条：**
1. **param 轴确实存在且有独立上限**：target 超过饱和点后采样已全表不变，MCV_items 封在
   19、qerr 不再改善——"采样已够捕获，加表示无益"。表示精度本身是一个独立、且**非单调**的
   维度：不是越细越好，饱和/超参后反而出现低频尾噪声（5000/8193 → qerr 2.5–2.8 走差）。
2. **PG 用一个 `statistics_target` 把 (λ, param) 耦合，且是"统计上有依据"的耦合**：它既经
   `targrows≈300·target` 拨采样行，又把同一 target 设成该对象的桶数/MCV 上限。PG 的
   `minrows = 300·target`（`analyze.c` 引 Chaudhuri–Motwani–Narasayya SIGMOD'98）给出
   **表示参数的一个样本下界：要 `k` 桶可信须采约 `300k` 行**。故该耦合不是纯实现巧合：PG 用一个
   `statistics_target` 同时表达了"采样档 λ"和"表示参数"，并把两者绑进**一条 engine 强制的
   Chaudhuri floor** —— 你**不能**要求"细 param + 浅样本"(Direction B)，那在统计上自相矛盾
   (细桶无足够独立样本填充)；你**可以**利用的是另一侧(Direction A)：凡是某对象已把扫描抬深，
   其余薄对象就从同一份共享深样本里"免费"取更稳的粗桶(见 §7bis λ-carrier)。
3. **Oracle 不强制这条 floor，λ/param 是真·解耦的两个 GATHER 参数**：`estimate_percent`(≈采样/λ,
   表级每 GATHER 全局、可直接设)与 `SIZE buckets`(≈param, 逐列组)独立；engine 允许"细桶+浅扫"
   (PG 拒绝的 Direction B)——是否统计健全**不被 engine 兜底，留作优化器层的可选护栏**。

**对 v2 的意义**：capacity 在**抽象/跨后端**上是两个**具名轴** (λ, param)，但它们之间的关系是
**backend 依赖的，不是一个普适等式**——这正是 backend 抽象该承载的部分：
- **PG**：λ 不是可直接设的自由量，而是**经 `300·max(param)` 由所选对象实现**的编码受限量；
  其 Chaudhuri floor 由 **engine 强制**；要"深 λ 而全薄对象"须显式加 **λ-carrier**(抬 max)。
- **Oracle**：λ=`estimate_percent` 直接、独立、表级全局；param=`buckets` 解耦；floor **不强制**，
  若也要统计健全则它是 model 层的可选护栏(数据相关，非铁律)。
切忌把任一侧的形态当作普适模型：既不要把 PG 的"λ 由 max param 派生"当成到处成立(对 Oracle 错)，
也不要把 Oracle 的"两轴完全自由"当成到处成立(对 PG 只能近似表达 λ)。
实现上"把 capacity 扩成 (λ,param) 两维 + 各 backend 声明 λ 如何实现/floor 是否强制"属后续工作
(不在本次 M-commit 范围)；本小节固定**论点与实证**。

> **研究范围决策（2026-09-02）：单列 target 定死为 100，不是决策变量。** 本项目的正题是
> **extended statistics（多列相关）**，单列 tuning 不是研究对象。故 PG 把普通列 `attstattarget`
> 钉死在 `ALTER COLUMN SET STATISTICS 100`（`_ensure_single_columns_pinned`），`default_statistics_target`
> 恒 100，且**部署时不把单列随 λ/扫描抬上去免费变细**（放弃 Direction A 的 free-rider 用于单列）。
> 于是：
> - 单列退居**固定基态反事实**：ext 的 `Δ_is` 与 `e^0_i` 都在"单列=100"下量测——**one-stat
>   sufficiency 不因 marginal 变化受污染**（sufficiency 是 correlation 层论断，见 v1 论文）；
> - λ 在 PG 一侧**完全由 ext 对象的 target 决定**（单列恒 100<ext 档，从不成为 max，不抬 λ）；
> - 逐列提升/单列 free-rider/把单列当第二决策维 等项**不纳入本模型**。
> λ-carrier（抬 max 的 decoy）仍保留，但**仅服务于"深 λ 而所有真实 ext 对象皆薄"这一 fidelity
> 场景**，与单列无关。

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
(5) 可选 global-disjoint，(6，v2 新增，可选) **维护预算**：表激活阶梯 + 每统计
变动成本。当传入 `MaintProfile`（`table_base_tiers` 每表各 target 档的固定成本
+ `PhysicalStat.maint_cost` 作每统计变动态）时，求解器加表激活/档位指示变量
$w_{t,\ell}$，使**每被激活表只付一次固定 ANALYZE 成本**（按其选中统计最高档），
加每统计变动态；不传 `MaintProfile` 时退化为 additive fallback（`maint_cost`
可加，`maint_budget=None` 不施加，向后兼容）。全部用 `scipy.optimize.milp`。

---

## 7bis. 解耦容量 (λ, param) 的优化模型（设计基准，未实现）

> 这是 §6.3(g) 论点 + 一路讨论收敛成的**正式模型 spec**，供后续求解器实现参照；
> 当前 `core/optimize.py` 仍是 §7 的 per-stat-level 模型，二者在实现上尚未合并。

> **范围声明：本模型的决策空间只含 extended statistics。单列（regular column）target 定死为
> 100，不是决策变量**（详见 §6.3g 末"研究范围决策"）。单列仅作为固定基态反事实存在——ext 的
> 每个可行 param $p$、$e^0_i$、$\Delta_{is}$ 都在"单列恒 100"下量测；PG 侧也正是靠单列恒 100
> （不为 max）让 λ 完全由所选 ext 的 param 决定。故下方所有 $y_{C,p}$ 中的 $C$ 都是**多列组合**，
> 不含单个列；想要"修单列选择性"不在本模型范围内。

**把 capacity 拆成两个具名轴、各后端声明二者关系**（见 §6.3g、§6.3f）：
- **λ（表级扫描档）**：一次 ANALYZE/GATHER 采多少行。跨后端它是一个**真·有内容的轴**
  （Oracle `estimate_percent` 是可直接设、独立的表级全局 GATHER 参数；PG 则是经
  `targrows≈300·target` 由所选对象实现、需另述的编码受限量）。λ 主导一次扫描的 fixed
  维护成本，并决定 fidelity（能否捕获驱动组合）。
- **param（统计表示参数）**：每个统计对象的表示细节（MCV 项 / 直方图桶数），是**逐对象**
  决策，主导该对象的存储与其表示误差；非单调（捕获已足后再加并不更优，§6.3g）。

**λ 与 param 不是完全正交、绑定与否是 backend 声明属性。** 抽象层保留两个具名轴；每个
backend 在其编码层声明：(a) **λ 如何被实现**——PG: $\lambda=300\cdot\max_s p_s$（被所选对象
抬到哪算哪，故"深 λ 但全薄对象"须加 **λ-carrier**）；Oracle: $\lambda$=直接输入的
`estimate_percent`。(b) **Chaudhuri floor 是否强制** $\lambda\ge300\,\max_s p_s$（保证每个
param 都有足量独立样本支撑）——PG: engine `minrows` 强制；Oracle: 不强制，"细桶+浅扫"
(Direction B) 可表达，是否采用是优化器层可选护栏。核心逻辑（objective/query-level
fidelity/保守化）不改，只把该绑定当作一笔由 backend 提供的**可行性/成本**输入。

**决策变量**（内层，给定表级 λ 后）：对列组 $C$、可选 param $p$：
$$y_{C,p}\in\{0,1\}\ (\text{建一个 param=}p\text{ 对象}),\qquad x_{i,(C,p)}\le y_{C,p}.$$
λ 是**表级量，不进对象下标**：同一表所有被选统计共享该布局的 λ。param 的可选集合被该表的
λ-bind（§6.3g）切出上界——PG 侧 $p\le\lambda/300$（engine 已强制，故 optimizer 只需在预扫网格
里**不 offer 越界组合**）；Oracle 侧无 engine 上界，是否把 $p$ 限制在 $\lambda/300$ 由 optimizer
作为可选护栏决定。

**内层（给定每表 λ，固定/共享扫描在此 λ）**——选 (列组, param) 以最小化保守的 workload 目标：
$$\min\ \underbrace{\tfrac1{|Q|}\sum_i \hat e_i^{(\lambda,T_i)}}_{\text{query-level, 见 fidelity}}
\quad\text{s.t.}\quad
\begin{cases}
\sum_p y_{C,p}\le 1,\\
x_{is_a}+x_{is_b}\le 1\ (\text{查询内重叠, 乘性近似可信前提}),\\
p\ \text{可行域受该表 λ-bind(§6.3g)},\\
\sum_{C,p} c^{(\text{param})}_{C,p}\,y_{C,p}\le B,\\
\text{fixed 维护}=f_t(\lambda_t)\ \text{每被激活表一次},\quad \text{var 维护}=\sum \text{var}^{(\text{param})}y.
\end{cases}$$

> **PG 的 λ 编码（可选分支）**：PG 无"SET λ"扫描旋钮，表级 λ 由所选对象经 `max(param)` 实现。
> 若把决策直接写为逐对象 param，则每表 $\lambda_t=300\cdot\max_s p_s$ 是**派生量**（写进
> $f_t$ 与 fidelity）；表达"深 λ、无真实对象愿扛"时加一个 **λ-carrier**（抬 max 的 stat，
> 测量后 mask/drop，见 §6.3f/Protocol-M）即可。Oracle 则直接把 $\lambda_t$=`estimate_percent`
> 当输入旋钮，无需 carrier。两分支都由同一 core objective/fidelity 消费，差异只在 backend 的
> "λ 如何实现/是否需 carrier"编码层。

**query-level 的 fidelity → 不硬删、保守化。** λ 是否够捕获是**逐查询**量
$\lambda_{q}=\mathrm{truth}_q\cdot\mathrm{sample}_N(\lambda)/N_t$。**不作为硬删**：
低 λ 的查询不用乐观均值，而改用其该 λ 下的**保守估计**（如 `qerror_worst` / 上界 / 由
`qerror_std` 抬高——即用已记录的 §6.3f 字段），再进入上述 workload 平均。

**外层**——对 λ∈Λ 各解一遍内层 MILP，比较时计入"这次部署的 fixed 扫描成本"，取：
$$\min_{\lambda\in\Lambda}\ \Big[\ \text{innerObj}^{(\lambda)}\ (\text{已含 query-level 保守化}) \
+\ \sum_t \rho_t\,f_t(\lambda_t)\ \Big].$$
不做**整档一刀切拒绝**：某 λ 恰使某个(些)query 落入低 fidelity 时，该 λ 只是在这些 query 上
保守化偏高、从而在 cost 权衡下自然不敌更大 λ；它不会被从 Λ 里删掉。这里的 $\lambda_t$ 是**每表**
的扫描档：Oracle 是可直接输入的 `estimate_percent` 档；PG 在逐档预扫时经"把该表对象设到
对应 target、其余 ≤ 该值使 max=该档"来实现（与 §6.3f 的按 tier 固定扫描成本一致）。
PG 若想表达"λ 深、无真实对象愿扛"，再加 λ-carrier 即可（见上"PG 的 λ 编码"框）。

**λ 的离散化与 param 预扫网格**：预测量原则(§1.1-1.8)——不允许"边解边补测"。故 Λ 取
**少数离散表级扫描档**(沿用现有 ladder，如现有 {10,100,1000,…} 或 Oracle {1,10,100}%)，
每个 λ 档下对全部候选×param 预扫。**param 的上界被该表 λ-bind 切掉**(§6.3g)：PG 只预扫
$p\le\lambda/300$（engine 已强制，越界组合本就不可建）；Oracle 无 engine 上界，可按需把
护栏杆 $p\le\lambda/300$ 作为可选施加。是否按 fidelity 边界(ω≈1/5)加密各 λ 档留作数据驱动的
后续开点，不硬编码 ladder。

**骨架验证（PG，Census）**：对 3 条最坏相关查询的已知主导对，测其在各扫描档 target
(=statistics_target) 下的 q-error 与 λ（PG 耦合限制：λ/param 同由 target 携带，故逐档
读数即该档表现）：

| 查询 (truth, 主导对) | tgt50 | tgt100 | tgt250 | tgt1000 | tgt2500 | tgt10000 |
| --- | --- | --- | --- | --- | --- | --- |
| q.184 (13, (iDisabl1,iRspouse)) | 2.77 (λ.08) | 1.38 | 1.69 | **1.08 (λ1.6)** | 1.38 | 1.30 |
| q.62 (45, (iRspouse,iWork89)) | **45 (λ.27)** | 2.07 (λ.55) | **1.64 (λ1.4)** | 2.18 | **1.45** | 2.50 |
| q.61 (107, (iDisabl2,iYearsch)) | **98.8 (λ.65)** | 46.5 | 16.5 | 3.67 | **1.01 (λ33)** | 1.11 |

读数：(1) **λ 越过 ~1 前后是 q-error 的剧变边界**（q.62 tgt50 λ.27→qerr45, tgt100 λ.55→2.07：
这正是 fidelity 阈值，跨引擎同构地可观测到）；(2) **大档单调性弱/回落**（q.62 在 2500 的 1.45 反而
到 10000 变 2.5），故外层需**搜索而非盲目抬 λ**；(3) 诱饵列组各档都平(dDepart 系 ~3400)——"该 λ 档
下哪个列组有效"在固定档时才是内层 MILP 要解决的可解释问题。证实 §7bis 的两层结构在 PG 上行为合理；
真正的实现仍在 §7bis 之外未做。

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
3. **M3 — Oracle 后端（已完成）**：实现 `backend/oracle.py`（column groups +
   Protocol-A，按列集合隔离），验证同一 `core` 无改动跑 census。MCV 收敛与
   FIXED_ONLY/per_scan 维护模型已在 §6.2 / §4 记录。
4. **M4 — 交叉验证（已完成核心验证）**：同一份 `core` 在 PG/Oracle 上对比，证明抽象层真正
   通用。关键结论见 §6.3：*自然单列基线逐查询对齐（log-qerr corr≈1.0）*、*修正了"Oracle
   基线偏差"的测量假象（改用自然单列统计基线）*，且*最坏相关查询的主导列组两引擎选定
   一致（query.62/184/61，agreement=True，复现脚本 `extstats2.eval.cross_focus`）*。
   收尾可选：在完整 workload 上用 ILP 对比两端整体"买哪些列组"、以及协议-M/更精细的成本。

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

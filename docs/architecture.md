# extended-stats-optim-v2 — 通用化架构设计

> 状态: **设计阶段 (draft)**。本文档是 v2 代码库的蓝图，实现前请先 review。
> 目标: 把 v1 (`extended-stats-optim`) 的贡献从 **PostgreSQL 专属** 泛化到
> 多个数据库后端（首期 **PostgreSQL + Oracle**），同时保留 v1 的三项核心成果：
> (1) one-stat sufficiency；(2) Protocol-A / Protocol-M 测量；(3) budgeted ILP 分配。

---

## 1. 动机与背景

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

## 2. 架构总览

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
│   └── loaders.py            #   census / job / stats_ceb 等 loader（沿用 v1 parsers）
│
├── cli.py                    #   CLI 入口（bench -> measure -> optimize -> verify）
└── config.py                 #   路径 / 后端选择 / 预算等配置
```

**分层原则：**
- `core/` 只依赖 `backend.base` 的**抽象接口**（类型标注），绝不直接 import `postgres` / `oracle`。
- `backend/` 提供接口各实现。核心通过工厂（如 `config.get_backend(name)`）获取实例。
- 这样 `core/` 的可测试性高（可 mock backend），新增数据库只需新增一个 `backend/<db>.py`。

---

## 3. 统计能力抽象 (Capabilities)

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

## 4. 后端抽象接口 (base.py)

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

## 5. 后端细节

### 5.1 PostgreSQL (`backend/postgres.py`)
- 直接移植 v1 的 Protocol-A (`measure.py`)、Protocol-M (`measure_mask.py`)、
  `estimate.py`、`stats.py`、`catalog` 查询。
- 唯一变化是把函数签名切到抽象基类，SQL 字符串留在后端内部。
- `has_protocol_m() == True`（这是 PG 独有的加速，暴露为可选能力）。

### 5.2 Oracle (`backend/oracle.py`)
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

## 6. 核心算法 (core/) — 可复用 v1 的部分

| 模块 | v1 来源 | 改动 |
| --- | --- | --- |
| `optimize.py` | 直接移植 | **无改动**（MILP 模型本就通用：budget + overlap-free + shared-resource） |
| `candidates.py` | 移植 | 从"列集合"层面泛化（本就用 `(table, columns)` 表示，无需改） |
| `predicates.py` | 移植 | 去掉 `read="postgres"`，改用通用 dialect 或后端提供的 dialect |
| `estimate.py` | 抽象化 | 移入 backend：core 只调用 `backend.estimate()` |
| `queries.py` | = v1 `BenchQuery` | 不变 |

**MILP 模型不动**（v1 已验证）：目标 $\sum_i w_{is}x_{is}$，约束
(1) 存储预算 $\sum_s c_s y_s \le C$，(2) 选择须已创建 $x_{is}\le y_s$，
(3) 查询内重叠禁止 $x_{is_a}+x_{is_b}\le 1$。全部用 `scipy.optimize.milp`。

---

## 7. 配置与 CLI

```python
# config.py
@dataclass
class Config:
    backend: str            # "postgres" | "oracle"（可扩展）
    bench: str              # census | job | stats_ceb
    budget_bytes: int
    capacities: tuple[...]  # 通用容量级别（core 侧）
    protocol: str | None    # None = 后端自动选 a/m
```

CLI 流程与 v1 一致：`generate → measure → optimize → verify`，但每一步都
面向 `Backend` 抽象。

---

## 8. 里程碑

1. **M1 — 脚手架**（本次）：目录、`base.py` / `capabilities.py` / `catalog.py` 抽象、
   `config.py`、README、本文档。`core/optimize.py` 从 v1 移植。
2. **M2 — PG 后端**：移植 v1 全部 PostgreSQL 逻辑到 `backend/postgres.py`，
   通过一条 census 冒烟测量验证 `core` 跑通 PG 路径（含 Protocol-M）。
3. **M3 — Oracle 后端**：实现 `backend/oracle.py`（column groups + Protocol-A），
   验证同一 `core` 无改动跑 census 或 stats_CEB。
4. **M4 — 交叉验证**: 同一份核心算法在 PG/Oracle 上对比结果，证明抽象层真正通用。

---

## 9. 未决问题 / 决策点

- [ ] `dependency` 能力在 Oracle 是否实现，还是仅标记不支持？建议首期**不实现**
      （core 的 MILP 能处理"某后端不支持某能力"，只需把该能力候选权重置为无增益）。
- [ ] 容量归一化：core 用 `[0..1]` 采样强度还是级别索引？建议**级别索引**
      （如 `(0,1,2)` 映射到各自的原生级别），因为 PG 的 `statistics_target` 与
      Oracle 的 `estimate_percent`/`BUCKETS` 无线性可逆映射。
- [ ] q-error 定义与 zero 处理：保持 v1 语义（`max/min`，零侧用下限 1）。
- [ ] 连接层：`python-oracledb`（thin 模式）还是 `cx_Oracle`？建议 thin。
- [ ] 备份/恢复的并发安全：Protocol-M 备份表命名需 per-session 唯一（沿用 v1 约定）。

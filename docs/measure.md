# 测量 (measure) — S-grid 语料、引擎覆盖与测量口径

> 本文档记录**如何测量**：语料设计（S-grid dataset-bound 采样）、逐 bench/逐 db
> 测量基建与覆盖矩阵、每条 (候选,level) 记录了哪些读数、以及 candidate-bearing
> 报告分母。
> 方法与结论均来自 `results/` 现成产物（重派生），不搬运 archive。
> 关联：设计思路见 [`architecture.md`](architecture.md)；选统计见 [`optimize.md`](optimize.md)。

## 1. S-grid：dataset-bound 采样设计

测量"该买哪些多列统计、要采样多深"需要先定采样档。S-grid 用一个**与数据集绑定**的
全局采样行数网格跨 PG/Oracle 施加，使同一语料在两个引擎上可比：

$$S_{rows}\in\{30000,\,300000\}.$$

- **PG**：`statistics_target = S/300` → level L0≈`100`、L1≈`1000`（`single_target`）。
  一次 ANALYZE 的采样行 `targrows≈min(300·target, N)`（Chaudhuri floor）。
- **Oracle**：每 **owner 表**给 `estimate_percent = 100·min(S,N)/N`（realized 行数
  $=\min(S,N)$；小表因 $\min(S,N)=N$ 饱和于 100%）；表示分辨率（桶/`SIZE`）由引擎
  自决。realized-S 随表行数分三类见 §1.1。

动机：错误的"跨档公平"会给"调采样导致基线漂移"的不公平对比；把采样行 S 作为
自变量、所有单列都随 $\theta=S/300$（PG）或随 `est%`（Oracle）同深，让 no-ext 基线
与 ext 候选在**同一采样态**里配对，只差是否含扩展统计——这是同-$S$ 的公平对照。

### 1.1 表行数三类情况：per-table realized S

全局 $S_{rows}\in\{30000,300000\}$ 是"请求深度"，因每表行数 $N$ 不同，**实际被采样
的行数（realized S）按 owner 表行数分三类**。两引擎实现的是同一条 realized-S：
$$S_{\text{realized}}(t, \ell)=\min(S_{\ell},\,N_t),\qquad\text{PG: }S=\min(300\cdot\text{target},N_t),\ \ \text{Oracle: }est\%=\tfrac{100\min(S,N_t)}{N_t}.$$

| 类 | 表行数 N | L0(请求 30000) | L1(请求 300000) | effective 采样点 |
|---|---|---|---|---|
| **小表 (tiny)** | $N<30000$ | 两 tier 都 $>N$ → realized $=N$（全表） | 同左（仍全表） | $N$（**L0≡L1 一个点**，饱和于全表） |
| **中表 (mid)** | $30000\le N<300000$ | realized $=30000$（部分） | realized $=N$（**全表**） | $\{30000,\;N=\text{full}\}$ |
| **大表 (large)** | $N\ge300000$ | realized $=30000$（target≈100） | realized $=300000$（target≈1000） | $\{30000,\;300000\}$ |

要点（重派生自 `src/extstats2/backend/{oracle,postgres}.py` 的实现语义）：

- **小表**：两档都超过 N，只能全表采样（Oracle `est%=100`；PG target 被 `n/300` 封顶
  到 <100）。此时 L0/L1 的**采样深度无差别**——只剩表示参数（Oracle 引擎自决桶 / PG 无
  更高 target 空间）可言，被选统计会因表太小而在两档等价，从而测量里只有 1 个有效
  采样点。
- **中表**：L0 是一个真部分档（采 3 万行），L1 恰触及全表；这是 L1 "到全表的过渡带"，
  反映 dataset-bound 的一个边界：请求 300000 但表只有几十万行内即封顶。
- **大表**：主流研究表两档各是真部分采样（L0≈3 万行 / L1≈30 万行），distinct 采样点 =
  $\{30000,300000\}$——只有在此类上 S-grid 的 λ 轴才真正拉开。

**实际例（stats_CEB 一个 bench 内三种表行数并存，非常能展示为何 S 需按表记）**：
| stats_CEB owner 表 | N(行) | 类 | realized 采样点 |
|---|---|---|---|
| `tags` / `postlinks` | 1032 / 11102 | 小表 | 全表（L0≡L1） |
| `users` / `badges` / `posts` / `comments` | ~40k / ~80k / ~92k / ~174k | 中表 | $\{30000,\ \text{full}\}$ |
| `posthistory` / `votes` | ~303k / ~328k | 大表 | $\{30000,\ 300000\}$ |

> 故跨 bench/跨 owner 表比较时，不能假定每张表都有两个不同采样点：要先查
> `_meta.extra.table_s_rows`（每 owner 表每级 `S_rows`/`estimate_percent`）再引用。这也是
> 为何 `_meta.tiers.S_rows` 不写成单值而是按表落 `table_s_rows`（一表一意，stats_CEB 跨
> 多表各有各 N）。

## 2. 测量引擎/基建

- **协议**：PG 优先 Protocol-M（catalog-mask 加速，把统计设为目标而不重扫整表）；
  Oracle 单 DB、共享 catalog、无 clone-mirror → 只能串行 Protocol-A（建/GATHER/测/
  删，按列集合匹配隔离）。`measure_query_*` 据此自动分派。
- **并行**：PG 用**镜像库池**（如 `dmv_m1..m8` / `census_m1..m8`）多 worker 并行测同
  一 bench；Oracle 因单 DB 共享目录无法镜像，只能 serial。
- **resumable / 去重**：测量驱动可断点续跑；元数据去重（保留唯一不同
  `table_s_rows` 等关键字段），避免重复行污染语料。

## 3. 每条 (候选, capacity-level) 记录什么（测量产物的 schema 要点）

对选中的候选列组 × 采样级（L0/L1），每条查询测量并落盘：

- query-level q-error（该候选单独激活、同一采样态下的误差）；
- no-ext 基线 q-error（该 S 态无扩展统计）；← 由同采样态给出，构成 `e^0`
- 该统计在该级下的**存储字节**与**维护秒**（供 optimize 消费）；
- 采样捕获量（λ 式：组合出现的期望次数）与方差相关字段（标识低可信档，不硬删，
  供优化保守化，见 architecture §3）。

测量在**候选级 skips**、只测该 bench 实际会出现/需要的候选，避免测出"永不被
单独使用"的死候选。

## 4. 语料与引擎覆盖矩阵

| bench | owner 表 | 语义 | PG | Oracle |
|---|---|---|---|---|
| census | `climate` | 宽表，458 谓词 query 系 | ✅ 已测 | ⏳（早前 ORA 清理后待重测） |
| stats_CEB(single) | stats_CEB 子计划表 | 多表 join 式 workload 的单表子计划 | ✅ 已测 | ✅ 已测 |
| dmv | dmv 主表 | 窄表、大量坏尾需高 arity | ✅ 已测 | ⏳ 进行中（serial） |

> 行数以 **ok_files（候选可用）** 为口径，见下节。Oracle 侧单 DB → 覆盖会落后于 PG。

### 4.1 当前 PG 三 bench 的已落地语料（重派生自 `results/report_pg_sgrid_3bench.json`）

| bench | total queries | ok_files(=both levels) | L1 improveable | L1 heavy(base>4) | L1 2col 修不动(>4) | distinct colset |
|---|---|---|---|---|---|---|
| census | 468 | 467 | 461 | 53 | 15 | 2253 |
| stats_ceb_single | 632 | 180 | 112 | 9 | 0 | 35 |
| dmv | 1965 | 1926 | 1718 | 54 | 18 | 36 |

解读（供后代引用，勿搬回 archive）：
- **census**：绝大多数 query 在 L1 可被 2 列 mcv 改进；仍留 ~15/467 条即便 arity-2
  也修不到 ≤4（需更高 arity 或更深采样）。
- **stats_ceb_single**：单表子计划上误差本就不重（heavy 很少、0 unrepairable-2col），
  是"selection 谓词基线较健康"的 bench。
- **dmv**：总可改进条数最多（L1 1718/1926），修不动的 2col 反例 18 条为 **one-stat
  sufficiency 的边界反例**（需 >2 列 / 更深采样），量化后的 exact 样本以数据为准。

## 5. candidate-bearing 报告分母

报告指标只对**该查询实际有候选可用（arity-2 列组 ∩ workload 可见）**的查询统计，
不对全 query 集盲目平均——否则会被大量"本就无多列关系、任何扩列都无益"的查询
稀释；而在把"无法改进"伪报成"改进失败"的方向上不诚实。

- 判据：候选集含至少一组可用于该查询多列谓词的组合。
- 用法：measure/optimize/deploy 的质量/收益一律以 candidate-bearing 为分母的
  mean（主）+ geo/p90/max；预算轴统一为 **storage(bytes)** 与 **maint
  (seconds-per-refresh)** 两套（见 optimize）。

## 6. 覆盖缺口（诚实清单，演进用）

- **Oracle**：census、dmv 的 S-grid 已测/进行中分布见上表；Oracle 侧需在单 DB 上
  串行补齐，且 `tiers`/`est%` 按每 owner 表（`table_s_rows`）口径。
- **λ 待复测项**：本设计在部分 bench 上若需"高 arity / 更深采样"才可修的反例，
  需要额外候选集；现状是 arity-2 + L0/L1 已落地，更高档为下一步（对齐 optimize）。

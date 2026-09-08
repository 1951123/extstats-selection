# 测量 (measure) — S-grid 语料、引擎覆盖与测量口径

> 本文档记录**如何测量**：语料设计（S-grid dataset-bound 采样）、逐 bench/逐 db
> 测量基建与覆盖矩阵、每条 (候选,level) 记录了哪些读数、以及 candidate-bearing
> 报告分母。
> 方法与结论均来自 `results/` 现成产物（重派生），不搬运 archive。
> 关联：设计思路见 [`architecture.md`](architecture.md)；选统计见 [`optimize.md`](optimize.md)。

## 1. S-grid：dataset-bound 采样设计

测量"该买哪些多列统计、要采样多深"需要先定采样档。S-grid 用一个**与数据集绑定**的
**请求(请求档)采样行网格** $\{S_{\ell}\}$ 跨 PG/Oracle 施加，使同一语料在两个引擎上可比：
$$S_{\ell}\in\{30000,\,300000\}\qquad(L0:\ S_{0}{=}30000,\ L1:\ S_{1}{=}300000).$$

> 术语定标：$S_{\ell}$ 是**请求档**(requested depth)；因每表行数 $N$ 不同，实际采样行
> $S_{\text{realized}}=\min(S_{\ell},N)$（见 §1.1），两引擎用不同 knob 实现同一条 realized-S。

- **PG**：`statistics_target = S_ℓ/300` → L0≈`100`、L1≈`1000`(`single_target`)；一次 ANALYZE
  实际采样行 `= min(300·target, N)`（Chaudhuri floor；即 realized）。
- **Oracle**：每 **owner 表**给 `estimate_percent = 100·min(S_ℓ,N)/N`（realized 行数
  $=\min(S_{\ell},N)$；小表因 $\min(S_{\ell},N)=N$ 饱和于 100%）；表示分辨率（桶/`SIZE`）由引擎
  自决。

动机：错误的"跨档公平"会给"调采样导致基线漂移"的不公平对比；把采样档作为自变量，
所有单列都随请求档同深（PG 设 target $=S_{\ell}/300$；Oracle 设 `est%` 到同 realized-S），
让 no-ext 基线（该 $S_{\text{realized}}$ 态无扩展统计）与 ext 候选在**同一采样态**里配对，
只差是否含扩展统计——这是**同 realized-S 的公平对照**，不是只比"请求档"。

### 1.1 表行数三类情况：per-table realized S

全局请求档 $S_{\ell}\in\{S_{0}{=}30000,\,S_{1}{=}300000\}$ 是"请求深度"，因每表行数 $N$
不同，**实际被采到的行数 $S_{\text{realized}}(t,\ell)=\min(S_{\ell},N_t)$ 按 owner 表行数分三类**。
两引擎实现的是同一条 realized-S（PG 经 `300·target`、Oracle 经 `est%`）：
$$S_{\text{realized}}(t,\ell)=\min(S_{\ell},\,N_t);\quad
  \text{PG: }S_{\text{realized}}=\min(300\cdot\text{target},N_t),\quad
  \text{Oracle: }est\%=\tfrac{100\,\min(S_{\ell},N_t)}{N_t}.$$

| 类 | 表行数 N | L0：$S_0{=}30000$ | L1：$S_1{=}300000$ | effective 采样点(realized) |
|---|---|---|---|---|
| **小表 (tiny)** | $N<30000$ | 两档都 $>N$ → realized $=N$（全表） | 同左（仍全表） | $N$（**L0≡L1 一个点**，饱和于全表） |
| **中表 (mid)** | $30000\le N<300000$ | realized $=30000$（部分） | realized $=N$（**全表**） | $\{30000,\;N=\text{full}\}$ |
| **大表 (large)** | $N\ge300000$ | realized $=30000$（target≈100） | realized $=300000$（target≈1000） | $\{30000,\;300000\}$ |

要点（重派生自 `src/extstats2/backend/{oracle,postgres}.py` 的实现语义）：

- **小表**：两档都超过 N，只能全表采样（Oracle `est%=100`；PG target 被 `n/300` 封顶
  到 <100）。此时 L0/L1 的**采样深度无差别**——只剩表示参数（Oracle 引擎自决桶 / PG 无
  更高 target 空间）可言，被选统计会因表太小而在两档等价，从而测量里只有 1 个有效
  采样点。
- **中表**：L0 是一个真部分档（realized 采 $S_0{=}3$ 万行），L1 恰触及全表；这是 L1
  到全表的过渡带：请求档 $S_1{=}300000$ 已≥N、realized 被封顶于 N(全表)——dataset-bound 的
  一个边界。
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

### 1.2 λ 与 fidelity：表行数三类决定了"每条查询能采到多少个真值行"

**符号定义（先厘清，避免混淆三个量）：**

| 符号 | 名称 | 定义 / 含义 | 落盘字段 |
|---|---|---|---|
| $N_t$ | 表行数 | owner 表 $t$ 的总行数 | `num_rows` |
| $S_{\text{realized}}(t,\ell)$ | **采样行数(采样数)** | 档 $\ell$ 实际采多少行 $=\min(S_{\ell},N_t)$ | `sample_rows_per_level` |
| $f_{t,\ell}$ | 采样比例 | $S_{\text{realized}}/N_t$ | （可派生） |
| $\text{truth}_q$ | 查询真值 | 该查询真正命中的行数 | `actual` |
| $\lambda_q(t,\ell)$ | **期望捕获量** | $\lambda=f_{t,\ell}\cdot\text{truth}_q$：查询命中的行指望在样本里出现几次 | `lambda_expected` |
| **fidelity** | 可信性判定 | 由 λ 高低得出：λ≪1→不可信；λ≫1→保真 | 无独立字段（看 λ） |

要点：**λ 不是"采样数"**——它是"采样比例 × 查询真值"的交互量（还依赖 truth，是逐查询
的量）；只有在大表上真值刚好等于全表采样那档时才和采样数同量级。**fidelity 也不是 λ 本身**，
而是对 λ 落在哪一侧的**可信性判定**（代码落盘的是 `lambda_expected`；fidelity 是从 λ 推得的
结论，没有独立字段）。

- $\lambda\ll 1$：单次采样**很可能根本看不到**驱动该查询的组合 → 实测 q-error **高方差 /
  不可信**（配合 `qerror_std`/`qerror_worst`；重复测 1 次以上时取保守值而非乐观均值）。
- $\lambda\gg 1$：采到多次 → 测量**保真**（组合必被捕获，读数稳定）。

**关键：λ 是"逐表、逐查询 truth"的量，但三类表行数决定了 $f_{t,\ell}$（每表每级能采多大比例）**，
因此把三类与 fidelity 直接挂钩：

| 类 | $f$ 在这类的形态 | fidelity 含义 |
|---|---|---|
| **小表** | 两档都 $f=1$（全表） | $\lambda=\text{truth}$（truth≥1 的组合必被采到）→ **采集永不掉保真**，无采样方差问题；L0 与 L1 的 λ 相同。余下唯一可能限制是**表示参数**（Oracle 引擎自决桶 / PG 封顶 target），非采样 |
| **中表** | $f_{L0}=30000/N\in(0,1)$，$f_{L1}=1$ | L1 全表 → $\lambda=\text{truth}$ 保真；L0 是部分比例 → 仅当中等稀疏 truth 时 λ 会偏小。转折：$N$ 越靠近 30000，L0 比例越大；越靠近 300000，L0 比例越薄（如 `comments` 174k → $f_{L0}\approx0.17$) |
| **大表** | $f_{L0}=30000/N$、$f_{L1}=300000/N$（两者都 <1） | 两档都可能让稀疏 truth 的 λ 跌破保真线；且 $f_{L1}\approx10\,f_{L0}$(因 S 之比=10) → **同一条稀疏 query，L1 的 λ 约是 L0 的 10 倍**。最稀疏的坏尾在此类上两档都 λ≪1 → 高方差的根因，也解释了为何"需更高 arity / 更深采样"的反例大多落在 census/dmv 这类大表 |

**量化例（大表，重派生自真实 N）**：
- census `climate` N≈2.46M：$f_{L0}\approx0.0122$、$f_{L1}\approx0.122$。truth=100 的 query →
  λ(L0)≈1.2、λ(L1)≈12（L1 保真、L0 边缘）；truth=8 → λ(L0)≈0.10、λ(L1)≈0.98（两档都不可信，
  属"组合极稀疏、capture 随机二值"的高方差坏尾）。
- dmv N≈11.6M：$f_{L0}\approx0.0026$、$f_{L1}\approx0.026$；同 truth 下 λ 再缩 5×——Dmv 即便 L1
  也要 truth≈40 才稳妥，稀疏坏尾更依赖更大 S 或更高 arity。

> 遵循"不硬删、保守化"原则（architecture）：低 λ 查询不删，而是用 `qerror_std/worst`
> 保守化后进优化，避免把"采样没采到"误当"扩列修不动"。fidelity 是 (truth,N,S) 的逐查询
> 量，三类表行数只改变 $f$ 取值空间——引用/比较跨表 λ 时务必按各自主表 N 与 truth 重算。

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
- 该统计在该级下的**存储字节**（供 optimize 的 storage 轴消费）与一个 per-candidate
  **维护占位 `maint_var`**（⚠️ 见 §3.1：这是**占位**，不是 optimize maint 轴真正消费的值）；
- 采样捕获量（λ 式：组合出现的期望次数）与方差相关字段（标识低可信档，不硬删，
  供优化保守化，见 architecture §3）。

测量在**候选级 skips**、只测该 bench 实际会出现/需要的候选，避免测出"永不被
单独使用"的死候选。

## 3.1 维护成本模型：measured-linear + `_maint.json` 伪影

> **两个"维护"量不要混：`maint_var`(语料占位) ≠ `c_var`(模型真值)。**
> - 语料里 per-candidate 的 **`maint_var`** 是**占位**：最初设想是"实测单个 (colset,p)
>   扩展统计对象自身的刷新边际"，但已确认**那样测很难、误差很大**（single ext-stat 在
>   共享 scan 下是微小、可翻转残差，很难隔离计时），故并未实行，只留下一个闭式模型常数
>   占位（PG ~0.02/stat@target1000、Oracle 平坦 0.002）。**这些 0.002/0.02 数值无实测含义**，
>   不应被当成真实每统计维护秒。
> - 维护成本模型**真正用的**是实测的 **`c_var(t,ℓ)`**（+ `fixed(t,ℓ)`），存于
>   `_maint.json`、按 $(t,\ell)$ 实测（本 § 下文）。optimize 的 maint 轴(single/multi 曲线)
>   一律用 `_maint.json` 的 `c_var`(replacement) 覆盖 corpus 占位，绝不把 `maint_var` 当实测。

「该统计的维护秒」不是逐候选单独计时（单扩展统计在共享扫描下是微小、可翻转残差，
不可靠计时），而是用一个**实测线性模型**离线拟合，存成语料伪影，供 optimize 的 maint
轴消费：

$$ \mathrm{maint}=\sum_{t\,\in\,T_{\text{active}}}\big[\,\mathrm{fixed}(t,\ell_t)
   +c_{\mathrm{var}}(t,\ell_t)\cdot n_t\,\big],
   \qquad \ell_t=\text{表 }t\text{ 被激活到的最高档},\ n_t=\text{该表选中统计数}. $$

- $\mathrm{fixed}(t,\ell)$：表 $t$ 在档 $\ell$ **刷新一次的实测共享扫描秒**（裸表、该档
  realized-$S$ 下，PG `ANALYZE` / Oracle `GATHER_TABLE_STATS`）。整表扫描成本 ⇒ 只与表+采样
  深度有关，故按 $(t,\ell)$ 存。
- $c_{\mathrm{var}}(t,\ell)$：表 $t$ 在档 $\ell$ 的**每扩展统计边际**（该表各统计等价），由
  整表扫描差的聚合除以探针数得出。同样按 $(t,\ell)$ 存。

**伪影与口径**（不在 source 里写死常数）：
- 存于 `results/measure/<bench>/<backend>/_maint.json`（与 `_meta.json` 同级）：
  `{backend, fixed_seconds:{<表>:{<档>:秒}}, c_var:{<表>:{<档>:秒}}}`；表键用语料点号形
  （`.climate`），载入时归一化小写（`.postHistory`↔`.posthistory` 通配）。
- **measured-or-raise（强制口径，2026-09-06）**：用维护成本约束前必须先实测 `_maint.json`
  且该 $(t,\ell)$ 已测，否则抛 `MaintNotMeasuredError`——无闭式回退。核心库
  `maint_model.py` 拥有 schema/IO/线性数学（DB-free、可单测）；DB-timed 拟合在各独立驱动
  （PG `core/maint_fit.py`、Oracle `core/maint_fit_oracle.py`）。`list_qids` 把 `_maint` 视同
  `_meta` 跳过，不当成 query 结果。

**拟合口径（PG/Oracle 同构，n=0-vs-n=k）**：
- `fixed(t,ℓ)`：丢弃残余探针后取**裸表**在该档 $S$ 下多次刷新的中位秒（n=0 态）。
- `c_var(t,ℓ)`：建该表**不同 2 列组**为探针（k≤cap，默认 100，列数<2 时后=0），
  先物化、再测维护这些组的一次刷新中位；$c_{\mathrm{var}}=(\text{with-}k-\text{fixed})/k$，
  下限非零（0.001s，避免噪声归零）。组用完即删（恢复自然态）。

**实测值例（S-grid，PG）**：
- census `.climate`：fixed L0≈0.286s / L1≈2.92s（10×采样 → ~10× 门槛）；c_var L0≈0.0013 /
  L1≈0.037。
- dmv `.dmv`：fixed L0≈12.0s / L1≈12.5s——11.6M 冷缓存大表整块扫描主导、target 不变，
  **实测 fixed 两档几乎平坦**（表尺寸所限的真实 S-grid-case-1 行为，非模型失败）；
- stats_CEB_single 各 owner 表：按各自 N 属小/中/大表，fixed 相应（见 §1.1 表行数三分类）。

## 4. 语料与引擎覆盖矩阵

| bench | owner 表 | 语义 | PG | Oracle |
|---|---|---|---|---|
| census | `climate` | 宽表，458 谓词 query 系 | ✅ 已测 | ⏳（早前 ORA 清理后待重测） |
| stats_CEB(single) | stats_CEB 子计划表 | 多表 join 式 workload 的单表子计划 | ✅ 已测 | ✅ 已测，**但分块见 §4.2** |
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

### 4.2 Oracle 为何在 stats_CEB_single 的(部分)查询上 base q-error 偏高

**不是测量错了**——这两批结果（当前 S-grid 与 `results_archive_pre_sgrid_20260905` 的 dense 语料）一致
复现，是我们如实读到的 Oracle 估计行为。`stats_CEB_single` 里约一半查询在 Oracle 上仅有
**单列自然基线的 q-error 就很高**；把它当"扩展统计(多列)失败"或"没测对"都是误读，本质是
**Oracle 对某类范围过滤的默认选择性估计偏(小)了**，而扩展统计对这类又不参与。解说如下：

**现象(判别=谓词是否含近唯一 TIMESTAMP 列的 range)：**

| oracle stats_CEB_single 子集 | 唯一查询 | 基线 q-error(mean/max) | 性质 |
|---|---|---|---|
| 含 `::timestamp` range（`CreationDate/… BETWEEN 或 ≤/≥`） | 117 | ~245 / 1770 | base 级估计偏差；L0≡L1，只与引擎对 range 的估计有关、与采样深度无关 |
| 不含 timestamp range | 63 | 1.65 / 4.61 | 与原 PG 一样健康、可量化 |

**为什么(机制，基于既有 Oracle 文档与先前探针而非臆测)**：
1. **Oracle 对范围谓词(BETWEEN/≤/≥)的选择率不走对多列扩展统计/列组的"区间覆盖"，而对近唯一、
   高基数的时间列(NDV≈行数)缺少能表达"该宽范围实际覆盖整表大半"的分布信息** —— 早先
   (§6.3d2 系、deploy 引擎边界记录)已确认 Oracle column-group 依文档 principal 主要服务等值/IN，
   不收敛普通 range；此处下探到单列：即使只问宽时间范围的单列选择性，Oracle 也会按"近唯一值
   在分布中的占比"得到一个显著偏小的估计。`st.129`(users)：PG 自然基线 est≈38931(q≈1.0)，
   Oracle est≈98(q≈399)。
2. **扩展统计(column-group)不参与这类 range** → 无论有无扩展统计该 base 都高、加列组无效。这是
   Oracle 能力边界，不在本项目"扩展统计选择"覆盖内。
3. 它发生在**加任何扩展统计之前的自然基线**（干净 SIZE AUTO、无残余列组）→ 不是测量/协议假象。

**跨世代一致**：`results_archive_pre_sgrid_20260905/per_lambda/stats_ceb_single/oracle`(dense)
同样：PG mean≈1.34/无>5；Oracle 带 timestamp mean≈296/读数全>5，无 timestamp mean≈1.36/仅9>5。
即这不是 S-grid 新测量引入，是 Oracle 对该类谓词的长期估计特性(PG `mcv` 对 range 部分参与、无此问题)。

**对本 bench 的 Oracle L2/L3 口径**：Oracle stats_CEB_single 的优化/部署要以**无 timestamp-range 的
63 条**为可量化分母（117 条单列 range 估计差、非扩列可修 → 单独呈报为引擎/机制注记）。分层对照
(Oracle vs PG) 时此为**引擎不对称项**：写为 Oracle 对宽时间范围过滤的估计特性边界，而非统计选择失败。

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
- **Oracle stats_CEB_single：117 条宽时间范围查询 base 级估计差（§4.2）** ——原因=Oracle 对
  近唯一时间列 range 的默认选择性估计偏小，且扩展统计(column-group)不参与 range，故**非扩列可修、
  也非测量问题**（dense 归档同样复现）。其 Oracle L2/L3 应以**无 timestamp-range 的 63 条**为
  可信分母，勿把 117 条的 base 级偏差计入 Oracle 均线。
- **λ 待复测项**：本设计在部分 bench 上若需"高 arity / 更深采样"才可修的反例，
  需要额外候选集；现状是 arity-2 + L0/L1 已落地，更高档为下一步（对齐 optimize）。

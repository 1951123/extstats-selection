# 优化 (optimize) — 从测量到选择的预算×质量曲线

> 本文档记录**测好之后怎么选**：把 query-level 测量合成为选择问题、给出
> storage / maint 两套 budget 轴下的质量曲线与"该选 L0/L1、该投多大预算"的
> 派生结论。数值来自 `results/curves/postgres/milp_{storage,maint}_sgrid_<bench>.json`
> （**PG 版**；Oracle 语料就绪时同构落于 `results/curves/oracle/`，见 `measure.md` 引擎口径）。
> 语料见 [`measure.md`](measure.md)；设计模型见 [`architecture.md`](architecture.md)。

## 1. 优化问题（结合 measure 的口径）

MILP：目标 =（candidate-bearing 分母下）minimize 平均
q-error；决策 = 创建哪些 (列组,cap) $y$ + 每条 query 用哪个 $x$；约束是列不重叠
稀疏、已创建才可选、以及一条 budget。budget 有两条正交轴：

- **storage**（`unit=bytes`）：统计对象占用；
- **maint**（`unit=seconds-per-refresh`）：部署后刷新一次的总代价 = **实测线性模型**
  $\sum_{t}\big[\mathrm{fixed}(t,\ell_t)+c_{\mathrm{var}}(t,\ell_t)\cdot n_t\big]$（每激活表
  fixed 一次 + 每统计变动；参数按 (表,档) 实测存 `_maint.json`，见 §1.2 的维护成本模型说明与
  measure §3.1）。

level L0/L1 各自给出一条曲线（同一 bench 同一采样态内）。

**选档决策解出的目标 = 算术均值(mean)。** 因为在默认档 **cap=1（每查询至多选一个统计，
见 §1.2）** 下目标退化为**精确线性**：每个查询的误差是独立线性项 $e_i^0-\sum_s\Delta_{is}x_{is}$，
整体对其求**算术平均**即可（逐查询可加，无乘性 log 叠加）。故曲线的纵轴 mean 就是模型
**求解目标**本身，而不仅是报告口径。

**对照：一般模型（cap>1、多选、乘性近似）时，目标函数是 q-error 的几何均值。**
乘性近似下 $\log e_i\approx\log e_i^0+\sum_{s}\log(e_{is}/e_i^0)$ 逐查询成立，故模型最小化的是
$$\tfrac1{|Q|}\sum_{i\in Q}\log e_i\;=\;\log\!\Big(\prod_i e_i\Big)^{\!1/|Q|}\;=\;\log\ \mathrm{gmean}_i(e_i),$$
即**最小化几何均值**（对 $\log$ 单调，等价于最小化 log-qerr 的算术均值）。它与 cap=1 的
算术均值档不同：一旦放开通用的 k 个统计叠加，误差进入 log 乘性空间，几何均值才是该模型的
自洽目标；cap=1 退化为线性后才落到算术均值。
$\text{geo}$/$\max$ 则是在该选中集上算出的**派生报告指标**（几何/最大），**不进入** cap=1
的 MILP 目标——报告时三者都给出、都以 candidate-bearing 查询为分母（见 §2-§4 表、measure §5）。

> **优化目标由档而非开关决定；不存在 worst-case/最小化最大化优化。** 求解器没有
> `objective` 选择开关——cap=1 走 sparse-linear 精确算术均值、cap>1/None 走乘性几何
> 代理，由 `optimizer_class`/cap 决定（`solve_ilp` 已去掉一个曾会让 reviewer 误以为支持
> worst/geomean 选择的 `objective` 参数）。`p90`/`worst`/`geo`(最大) 都只作为**求解后的
> 派生评估指标**从 `qerror_per_query` 计算，绝不改变求解本身。**（可选保真度软罚的正式
> 形式见 §1.2a —— 它是默认关的乘子，不入 §1.2 上述"由档决定"的目标句集。）**

> **乘性代理把它们组织为带 query-level 下界的 workload model（Option B）。** 对
> `cap>1`，我们采用乘性 workload 代理
> $$\hat e_i=b_i\exp\!\Big(\sum_{s}w_{is}x_{is}\Big),\qquad w_{is}=\log\frac{e_{is}}{e_i^0}\;\le0,$$
> 并对每个 query 施加理论下界 $\hat e_i\ge1$ —— 在 log 空间里它是**每 query 一条线性行**
> $\log b_i+\sum_s w_{is}x_{is}\ge0$（即 `§1.2` 的 surrogate 下界约束）。于是**MILP 的目标
> （在 log 下最小化 surrogate）与求解后报告的 surrogate metric 完全一致**：solver 不会去
> 优化一个可能 `<1` 的几何乘积，decode 也**不需要**求解后再 `max(·,1)` 补 clamp。它把
> “$\hat e_i$ 只是代理、并非严格 q-error 恒等式”这句话写进模型而不是靠事后掩盖。

### 1.1 符号定义

| 符号 | 含义 | 备注 |
|---|---|---|
| $Q$ | candidate-bearing 查询集 | 报告分母（见 measure §5） |
| $e_i^0$ | 查询 $i$ 在该 realized-S 态下的 **no-ext 基线** q-error | 同采样态量得 |
| $s=(t_s,C_s,\ell_s)$ | 一条**物理候选统计**：表 $t_s$、列集 $C_s$、档级 $\ell_s\in\{0,1\}$ | arity-2 列组为主 |
| $O_i$ | 查询 $i$ 可用的候选集（$C_s\subseteq$ 查询谓词列） | 决定 candidate-bearing |
| $e_{is}$ | 查询 $i$ 若单独由 $s$ 服务时的 q-error | 同 realized-S 量得 |
| $c_s$（storage） | $s$ 的存储字节 | budget 轴一 |
| $c_s^{\mathrm{var}}$ = $c_{\mathrm{var}}(t_s,\ell_s)$（maint） | $s$ 的**实测**每统计变动秒，按 $s$ 的 (表, 档) 归属 | 见下方**维护成本模型** |
| $B_t[\ell]$ = $\mathrm{fixed}(t,\ell)$（maint） | 表 $t$ 刷新一次的**实测**一次性固定秒（该档 $S$ 下共享扫描成本） | 见下方**维护成本模型** |
| $y_s\in\{0,1\}$ | 是否创建统计 $s$ | 物理创建（跨 query 共享） |
| $x_{is}\in\{0,1\}$ | 查询 $i$ 是否选用 $s$ | 仅当 $s\in O_i$ |
| $\lambda_q(i,\ell)$ = $a_i\cdot\lambda$ | 查询 $i$ 在该采样档 $\ell$ 的**保真度量**：期望落进本次 scan 样本的真答案行数 | 每 (query,档) 标量（见 model.md §2） |
| $k$（超参，default `None`） | 保真度下限（`fidelity_floor`）；`None`=关闭 | 见 §1.3 可选软罚 |
| $w_i=\min\!\big(1,\ \lambda_q(i,\ell)/k\big)$ | 查询 $i$ 在该档的置信权重（截断线性、单调） | `k=None⇒w_i≡1`；仅当启用才打折该层收益 |

fidelity/λ 的处理沿用 measure §1.2：低 λ 档的 `e_is` 以保守化读数（如 `worst`/经 σ 抬高）
进入 $e_{is}$，不硬删该候选/查询。**新：** 除此之外还有一个**默认关的可选软罚**（§1.3），
它按每-query 保真度 $\lambda_q$ 在“绝对最优 credit”层再打一层，不改物理 `e_is`。

### 1.2 优化问题形式化（MILP）

在给定采样态（fixed realized-S 与 L0/L1 各自档）下，对一个预算轴投约束、最小化
candidate-bearing 平均 q-error。quality 目标是 query 级合成值；多选乘性近似与 cap=1
精确线性两档形式分别是：

$$
\text{(一般, 多选乘性)}\quad\min\ \tfrac{1}{|Q|}\!\sum_{i\in Q}\log e_i
\;\approx\;\text{const}+\tfrac{1}{|Q|}\sum_{i}\sum_{s\in O_i} w_{is}x_{is},
\qquad w_{is}=\log\tfrac{e_i^0}{e_{is}}\,({\le}0);
$$

$$
\text{(cap}=1\text{, 默认档, 精确线性)}\quad
\min\ \tfrac{1}{|Q|}\!\sum_{i\in Q}\Big[e_i^0-\sum_{s\in O_i}\Delta_{is}x_{is}\Big],
\qquad \Delta_{is}=e_i^0-e_{is}\,(\ge0).
$$

#### 1.2a 可选保真度软罚（默认关）下，cap=1 的目标改写

当 `fidelity_floor=k`（model.md §3a）被显式启用时，cap=1 目标变成对每条 query
的**收益贡献按置信权重折减**：

$$
\min\ \tfrac{1}{|Q|}\!\sum_{i\in Q}\Big[e_i^0-\underbrace{w_i}_{=\min(1,\ \lambda_q(i,\ell)/k)}\sum_{s\in O_i}\Delta_{is}x_{is}\Big],
\qquad w_i=\min\!\Big(1,\ \tfrac{\lambda_q(i,\ell)}{k}\Big).
$$

要点（与 model.md §3a 一致）：
- `k=None`（默认）⇒ `w_i≡1`，上式逐字退化为上面的原始 cap=1 公式（**零行为改变**）。
- `w_i` 是该 (query, 档) 的**常量**（`λ_q` 与 `p`/colset 无关），故它只整体缩放 `i` 的 `Δ` 贡献；
  物理 `e_is` 不变 ⇒ 求解后的 `qerror_per_query` decode 仍是**真 Δ** 解出的上报值，绝不伪造。
- 语义：`λ_q(i,ℓ) < k` 的查询在该层的 extstat“声称收益”按 `λ_q/k` 打折，视为浅 scan 噪声
  → 更少被 credit 抢预算；`λ_q ≥ k` 完全不被罚。
- 它仍**不是 objective 开关**（不选 worst/geomean），也不是新约束，只是默认关的乘子。

公共约束（选一个预算轴施加；storage 与 maint 正交）：

$$
\begin{aligned}
&\text{(storage)}\quad \sum_{s} c_s\,y_s \le C_{bytes};\\[1pt]
&\text{(maint,\ measured-linear)}\quad \sum_{t\in T_{\text{active}}} \Big[\,\mathrm{fixed}(t,\ell_t)+c_{\mathrm{var}}(t,\ell_t)\,n_t\,\Big] \le M_{\text{sec}};\\[1pt]
&\qquad \ell_t=\max\{\ell_s\,:\,t_s=t,\ y_s=1\},\quad n_t=\#\{s\,:\,t_s=t,\ y_s=1\};\\[1pt]
&\text{(select ⟸ created)}\quad x_{is}\le y_s,\ \ \forall\, i,\ s\in O_i;\\[1pt]
&\text{(overlap-free 保独立性, cap$\ge$1 亦施)}\quad x_{ia}+x_{ib}\le 1 \ \ \forall i,\ a\ne b\in O_i,\ C_a\cap C_b\ne\varnothing;\\[1pt]
&\text{(surrogate 下界, 乘性档)}\quad \sum_{s\in O_i}\log\!\tfrac{e_{is}}{e_i^0}\,x_{is}\ \ge\ -\log e_i^0\ \ \Longleftrightarrow\ \prod_{s\in O_i}\big(e_{is}/e_i^0\big)^{x_{is}}\ge\tfrac1{e_i^0}\ \Leftrightarrow\ \hat e_i\ge 1;\\[1pt]
&\text{(同列组至多一档)}\quad \sum_{\ell:\ (t_s,C_s,\ell)} y_{t_s,C_s,\ell}\le 1\ \ \forall (t_s,C_s);\\[1pt]
&\text{(cap=1, 可选稀疏档)}\quad \sum_{s\in O_i} x_{is}\le1;\\[1pt]
&y_s,x_{is}\in\{0,1\}.
\end{aligned}
$$

说明：
- **维护成本模型（measured-linear）**：刷新一次的总代价是**实测线性模型**
  $$\mathrm{maint}=\sum_{t\in T_{\text{active}}}\bigl[\mathrm{fixed}(t,\ell_t)+c_{\mathrm{var}}(t,\ell_t)\cdot n_t\bigr],$$
  其中每表只在其**被激活到的最高档** $\ell_t$ 付一次实测 `fixed(t,ℓ)`（该 $S$ 下裸表的共享扫描
  秒），再为该表的 $n_t$ 个选中统计各付实测的每统计变动 `c_var(t,ℓ)`。`fixed/c_var` **按
  (table, level) 分别实测**，存于语料伪影
  `results/measure/<bench>/<backend>/_maint.json`（schema
  `{backend, fixed_seconds:{<表>:{<档>:秒}}, c_var:{...}}`），由核心库 `maint_model.py`
  读写与提供纯线性访问器；每次刷新每表只付一次固定、变动按选中统计累计。
  单表 bench（census/dmv）`fixed_sec` 即每档那一个 owner 表的 `fixed(t,ℓ)`；多表 bench
  （stats_CEB_single）按**激活表子集**枚举，每激活表付它自己的 `fixed(t,ℓ)`，变动仍按
  各统计真实所属表累计 ⇒ 单值 `fixed_sec=None`。
- **measured-or-raise（强制口径）**：维护成本约束只允许在模型参数**已实测**后施加
  （`_maint.json` 存在且该 (table,level) 已测）。缺文件或未测档会抛
  `MaintNotMeasuredError`——无闭式回退。故每条 `maint` 曲线都以实测 `_maint.json` 喂养。
- **两个预算轴正交**：想给哪个就施加哪条（storage 或 maint），不强制同时给。`optimize.md`
  下文的 storage 曲线 = 只施加 (storage)；maint 曲线 = 只施加 (maint)。
- **cap=1（默认档）** 使目标从乘性近似退化为**精确线性**（见 architecture §2/§3）：$\min \sum_i(e_i^0-\sum_s\Delta_{is}x_{is})$，本仓各 bench 曲线即此档。**可选保真度软罚（§1.2a，默认关）** 在此档上按每 query 置信权重 `w_i` 缩放 `Δ` 贡献；实现面为
  `extstats2.core.optimize_sgrid.build_inner_at_level(..., fidelity_floor=k, out_weights=...)`
  → `solve_ilp(..., query_weight=...)`，`k=None` 时行为与"未加软罚"完全一致。
- 约束都线性/已线性化 ⇒ 用 `scipy.optimize.milp` 一次求得该 budget 与档下的**模型内全局最优**，
  不靠搜索。
- L0/L1 分别按各自「只允许 ℓ=0 或只允许 ℓ=1」再解，得到两条曲线；再做 **argmin-over-level**
  逐预算挑 `(L*, mean)`（§5）。

## 2. 基线（no-extstats，candidate-bearing mean）

| bench | L0 mean | L1 mean |
|---|---|---|
| census | 25.12 | 25.31 |
| dmv | 45.42 | 46.57 |
| stats_ceb_single | 1.34 | 1.33 |

读法：census/dmv 无扩展统计时 selection 误差大（≥25/≥45）；stats_ceb_single 的
单表子计划本就健康（~1.3）——扩列收益空间小，这正是其 E2E 里"加扩展几乎不改计划"
的源头（其真实误差主源在多表 join，不在此 bench 的选择谓词，见 deploy）。

## 3. storage 曲线（`results/curves/postgres/milp_storage_sgrid_<bench>.json`）

高 budget（~400 KB）时各档可达：

| bench | L0 floor(mean) | L1 floor(mean) | L1 geo | 达 L1 floor 约需 |
|---|---|---|---|---|
| census | 1.654 | **1.40** | ~1.21 | ~100 KB / ~279 colsets |
| dmv | ~16.98 | **6.51** | ~1.08 | 受 36 个 colset 数封顶(仅用 ~76 KB) |
| stats_ceb_single | 1.094 | **1.082** | ~1.06 | 很宽预算但仅 ~32-33 colset |

要点：
- **census**：L1 在 ~100 KB 附近把 baseline(25) 压到 ~1.40，之后加预算收益平缓
  （L0 只能到 ~1.65，不如直接上 L1）。storage 曲线 doc 里 L1 argmin 多为 level1 赢
  (>某门槛)。
- **dmv**：L0 即便给 400 KB 也只到 ~17 ——因为只有 36 个不同 2-col 候选，预算远超
  实际所需；L1 因采样更深把同样的 36 组修得更狠 → 6.51。**瓶颈是候选/arity 而非
  budget**；这正是需更高 arity/更深采样(见 measure §4.1 unrepairable)的主题。
- **stats_ceb_single**：从 1.3 → ~1.08，绝对收益小，因本 bench selection 误差本不
  重。

## 4. maint 曲线（`results/curves/postgres/milp_maint_sgrid_<bench>.json`，unit=seconds-per-refresh）

maint 曲线以**实测** `_maint.json` 的 `fixed(t,ℓ)` / `c_var(t,ℓ)` 喂养
（measured-or-raise：缺档抛 `MaintNotMeasuredError`）。**实测 fixed 门槛**（每表刷新一次的
共享扫描秒）按档拉开，先在预算内付得起的档激活，再往上加统计：
- census `.climate`：`fixed` L0=0.286s / L1=2.92s（L1 比 L0 高 ~10×，采样 10 倍）；
- dmv `.dmv`：`fixed` **L0=12.0s / L1=12.5s**——DMV 11.6M 冷缓存大表，两档都是整块扫描
  主导（target 不变），实测 fixed 几乎平坦（L0≈L1），即 DMV 的维护门槛不随采样档拉开
  （表尺寸所限的真实行为，非模型失败）；
- stats_CEB_single（多表）：`fixed_sec=None`（无单一 owner 表 fixed 序列），按**激活表
  子集**计——每激活表付它自己的 `fixed(t,ℓ)`。

各 bench 实测可达（该 grid 内、付得起该档 fixed 时的 floor）：
| bench | grid(秒) | L0 mean | L1 mean | 关键：fixed 门控 |
|---|---|---|---|---|
| census | 0.1–2.0 | 1.654(1.0s 即到) | 25.31（**未激活**） | L1 fixed 2.92s **> grid 上限 2.0s** → 付不起 L1，整档停在基线 |
| dmv | 1–30 | 16.98(L0, M>12.0) | **6.51**(L1, M>12.5, 15s) | L1 fixed≈12.5s：到 ≤12s 两档都基线_only，付过 fixed 后 L1 把 36 组修到 6.51 |
| stats_ceb_single | 0.05–3.0 | 1.094 | **1.082**(1.0s) | 无单一 fixed 门槛，L1 全表让 .posts+… 修到 1.082 |

读法：maint 预算先要付得起某档的 `fixed(t,ℓ)`（整表刷新一次），之后才为选中统计花
`c_var·n`。grid 上限一但 < L1 fixed，L1 根本无法上桌（census 例：grid ≤2.0s < 2.92s → L1
保持基线 25.31）。给了足够付 L1 fixed 的预算后，L1 因采样更深把同样的列组修得更狠。
maint 与 storage 曲线给出"同一质量可被存储或刷新预算哪个更便宜地买到"的对偶视角。

## 5. argmin-over-level（选档决策）

每条曲线按 budget 报"该 budget 下选 L0 还是 L1"，即逐预算 argmin 档：
- maint 轴：预算 < 该档 `fixed(t,ℓ)` → 该档**付不起、停在基线**；恰付得起 L0 fixed 时
  先选 L0；预算越过了 L1 fixed（如 census 需 >2.92s、dmv 需 >12.5s）后 L1 反超（dmv
  M≥15s 选 L1→6.51；census grid 从未越 2.92s → 全程 argmin 停在 L0）。
- storage 轴：小预算（数 KB）选 L0；census storage≥~10KB 后 L1 反超（mean 1.88→1.40），
  dmv/stats L1 当预算足够时同样最优。

exact points 在 json 的 `argmin_over_level`。通常论文叙述取"target budget B 下最优
(L*, mean)"这条 argmin 曲线。

## 6. 结论性摘要（S-grid 重派生）

1. 三 bench 的"要不要买深扩列"结论分化（**storage 轴**视角）：census 值得(L1→1.40)；
   dmv **受候选数(36) 与 arity-2 边界限制**、预算再多也停在 6.51(L1) / 17(L0)——扩更高
   arity 才有望突破；stats_ceb_single 已近健康(1.08)，selection 收益锚点低。
2. storage 与 maint 两轴共同刻画"质量成本边界"。**maint 轴先付 per-table 实测
   fixed**：预算 < 该档 `fixed(t,ℓ)` 时该档根本付不起（census L1 fixed≈2.92s、dmv L1
   fixed≈12.5s），故小刷新预算下 L0 为 pragma；L1 只在**付得起其实测 fixed 的预算**后才赢，
   且其 L1 质量收益(storage 轴的 1.40/6.51 等)在 maint 轴上要额外叠加大约一整次整表刷新
   的 fixed 才可达。
3. argmin-over-level 提供逐预算可部署档，feeding deploy.md 的 L3。

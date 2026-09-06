# 优化 (optimize) — 从测量到选择的预算×质量曲线

> 本文档记录**测好之后怎么选**：把 query-level 测量合成为选择问题、给出
> storage / maint 两套 budget 轴下的质量曲线与"该选 L0/L1、该投多大预算"的
> 派生结论。数值来自 `results/milp_{storage,maint}_sgrid_<bench>.json`。
> 语料见 [`measure.md`](measure.md)；设计模型见 [`architecture.md`](architecture.md)。

## 1. 优化问题（结合 measure 的口径）

沿用 architecture §2 的 MILP：目标 =（candidate-bearing 分母下）minimize 平均
q-error；决策 = 创建哪些 (列组,cap) $y$ + 每条 query 用哪个 $x$；约束是列不重叠
稀疏、已创建才可选、以及一条 budget。budget 有两条正交轴：

- **storage**（`unit=bytes`）：统计对象占用；
- **maint**（`unit=seconds-per-refresh`）：部署后刷新一次的总代价，由**表激活固定
  档**（per-table base，`fixed_sec`）+ **每统计可加变动**构成（Y-two-layer）。

level L0/L1 各自给出一条曲线（同一 bench 同一采样态内）。quality = mean（主）+
geo/max，均以 candidate-bearing 查询为分母。

### 1.1 符号定义

| 符号 | 含义 | 备注 |
|---|---|---|
| $Q$ | candidate-bearing 查询集 | 报告分母（见 measure §5） |
| $e_i^0$ | 查询 $i$ 在该 realized-S 态下的 **no-ext 基线** q-error | 同采样态量得 |
| $s=(t_s,C_s,\ell_s)$ | 一条**物理候选统计**：表 $t_s$、列集 $C_s$、档级 $\ell_s\in\{0,1\}$ | arity-2 列组为主 |
| $O_i$ | 查询 $i$ 可用的候选集（$C_s\subseteq$ 查询谓词列） | 决定 candidate-bearing |
| $e_{is}$ | 查询 $i$ 若单独由 $s$ 服务时的 q-error | 同 realized-S 量得 |
| $c_s$（storage） | $s$ 的存储字节 | budget 轴一 |
| $c_s^{\mathrm{var}}$ / $B_t[\ell]$（maint） | $s$ 的每统计变动秒；表 $t$ 达最高档 $\ell$ 的一次性固定秒 | Y-two-layer；budget 轴二 |
| $y_s\in\{0,1\}$ | 是否创建统计 $s$ | 物理创建（跨 query 共享） |
| $x_{is}\in\{0,1\}$ | 查询 $i$ 是否选用 $s$ | 仅当 $s\in O_i$ |

fidelity/λ 的处理沿用 measure §1.2：低 λ 档的 `e_is` 以保守化读数（如 `worst`/经 σ 抬高）
进入 $e_{is}$，不硬删该候选/查询。

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

公共约束：

$$
\begin{aligned}
&\text{(storage)}\quad \sum_{s} c_s\,y_s \le C_{bytes};\\[1pt]
&\text{(maint,\ Y-two-layer)}\quad \sum_{t} B_t\!\big[\max_{s\in S\,:\,t_s=t}\ell_s\big]+\sum_{s} c_s^{\mathrm{var}}\,y_s \le M_{\text{sec}};\\[1pt]
&\text{(select ⟸ created)}\quad x_{is}\le y_s,\ \ \forall\, i,\ s\in O_i;\\[1pt]
&\text{(overlap-free 保独立性)}\quad x_{ia}+x_{ib}\le 1 \ \ \forall i,\ a\ne b\in O_i,\ C_a\cap C_b\ne\varnothing;\\[1pt]
&\text{(同列组至多一档)}\quad \sum_{\ell:\ (t_s,C_s,\ell)} y_{t_s,C_s,\ell}\le 1\ \ \forall (t_s,C_s);\\[1pt]
&\text{(cap=1, 可选稀疏档)}\quad \sum_{s\in O_i} x_{is}\le1;\\[1pt]
&y_s,x_{is}\in\{0,1\}.
\end{aligned}
$$

说明：
- **maint 约束里的 max 是非线性的**，代码用表激活指示（阶梯档 $w_{t,\ell}$）线性化：
  $M_{\text{sec}}$ 对应 `maint` 轴（`unit=seconds-per-refresh`），每表中被激活到最高档只付
  一次固定 `B_t[⋅]`（`fixed_sec`），再加选中统计的每统计变动 $c_s^{\mathrm{var}}$。
- **两个预算轴正交**：想给哪个就施加哪条（storage 或 maint），不强制同时给。`optimize.md`
  下文的 storage 曲线 = 只施加 (storage)；maint 曲线 = 只施加 (maint)。
- **cap=1（默认档）** 使目标从乘性近似退化为**精确线性**（见 architecture §2/§3）：$\min \sum_i(e_i^0-\sum_s\Delta_{is}x_{is})$，本仓各 bench 曲线即此档。
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

## 3. storage 曲线（`results/milp_storage_sgrid_<bench>.json`）

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

## 4. maint 曲线（`results/milp_maint_sgrid_<bench>.json`，unit=seconds-per-refresh）

`fixed_sec` = 每表刷新一次的固定门槛（census/dmv：L0=0.256s、L1=2.56s；
stats_ceb_single 无 fixed 固定，即无 per-table cost 序列）。≤8 s 内各档：

| bench | L0@8s | L1@8s | 备注 |
|---|---|---|---|
| census | 1.654 | **1.405** | 需 ~4-8 s 才轮到 L1 划算(>2.56 fixed) |
| dmv | 16.98 | **6.51** | L1 因其 per-stat 深采样仍把 36 组修到 6.51 |
| stats_ceb_single | 1.094 | **1.082** | fixed 趋于无 → L1 收益很小但仍最优 |

读法：维护预算很紧(<~2.5 s)时选 L0（fixed 便宜）；足够付 L1 的 base 后用 L1 更优
（census 1.65→1.41）。maint 与 storage 曲线给出"同一质量可被存储或刷新预算哪个
更便宜地买到"的对偶视角。

## 5. argmin-over-level（选档决策）

每条曲线按 budget 报"该 budget 下选 L0 还是 L1"，即逐预算 argmin 档：
- 小预算（storage 数 KB / maint < fixed_L1≈2.56s）：L0；census L0 用小 budget 在
  4.47(2KB) → 1.87(10KB) 一带，L1 要到 ~10-20KB 才反超。
- 大预算：L1。census storage≥10KB 多给 L1（mean 1.88→1.40），maint≥~4s 给 L1
  (1.53→1.41)。

exact points 在 json 的 `argmin_over_level`。通常论文叙述取"target budget B 下最优
(L*, mean)"这条 argmin 曲线。

## 6. 结论性摘要（S-grid 重派生）

1. 三 bench 的"要不要买深扩列"结论分化：census 值得(L1→1.40)；dmv **受候选数
   (36) 与 arity-2 边界限制**、预算再多也停在 6.51(L1) / 17(L0)——扩更高 arity 才有
   望突破；stats_ceb_single 已近健康(1.08)，selection 收益锚点低。
2. storage 与 maint 两轴共同刻画"质量成本边界"；maint 需先付 per-table fixed，
   故小刷新预算下 L0 为 pragma，L1 只在付得起 fixed 时赢。
3. argmin-over-level 提供逐预算可部署档，feeding deploy.md 的 L3。

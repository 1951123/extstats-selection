# Oracle 基本流程 + multi-group composition — 证据笔记

> v2 results note · 2026-09-04 · Oracle Database Free 23ai (`FREEPDB1`, SYSTEM/`climate` 2.46M rows)
> 目标：确认 "测量(Protocol-A) → 优化 → 部署" 是否在 Oracle 上走通，并实证 Oracle 对
> **多个不重叠 extstat** 是否参与单条合取查询的基数估计（与 PG 的 OID 首适者行为对照）。
> 证据/脚本在 `scratch/…` + `results/…`（见 §6）。

## 0. 结论速览

1. **优化层**在 Oracle 上走通：通用求解器读 Oracle-measured 语料，选出的 (colset,param) 合理。
2. **部署层**在 Oracle 上也可原生走通，但**协议关键**：要把全部选中列组放进**一次** `GATHER_TABLE_STATS`
   （method_opt 并列所有 `FOR COLUMNS (…) SIZE p`）。逐组 `build_stat_param`（每 GATHER 重采集
   `FOR ALL COLUMNS`）会让估计退回基线，是**部署协议假象**，不是 Oracle 能力问题。
3. 部署修正后 E2E **闭合**：pred mean 1.8841 → TRUE mean 2.0326（true/pred ≈ 1.08）。
4. **Oracle 会用多个不重叠列组参与基数估计**（区别于 PG 的 OID 首适者 / 单统计主导）：
   4 列、6 列场景实证均组合到 ~truth，arity 2 与 3 皆可。
5. **前提（重要）**：多个组须**列不相交**、且**各自对齐一个真实相关簇**；跨簇乱并或漏切会明显变差。

---

## 1. 测量与优化：走通

- 测量：Oracle 无 Protocol-M（`has_protocol_m()==False`），用 Protocol-A（列组 `DBMS_STATS`）。
  `scratch/measure_census_oracle_l0.py`（resumable）在 L0 串行测全量 census；当前已在
  `results/per_lambda/census_mini/oracle`（3-query: query.184/61/62, levels 0&1, param `<={S_rows/300}`）
  有一个可复用的最小语料。
- 优化：`e2e_deploy_oracle.py` 读该语料 → `build_inner_at_level` → `solve_ilp(cap=1, budget)`，
  选出 7 个 `(colset@64)`，pred mean ≈ 1.88（基线 ~2400）。→ 优化器在 Oracle 侧无碍。

## 2. 部署协议教训（先踩坑、后修正）

- **错误协议**：逐个 `build_stat_param`（每个 GATHER 重采集单列 `SIZE AUTO` + 自身组）。
  用 7 个组如此部署时 estimate 回到无扩展基线（q.61 ~108k，看似"干扰/失败"）。
- **正确协议**：一次合并 GATHER，`method_opt = "FOR ALL COLUMNS SIZE AUTO " + Σ组 "FOR COLUMNS (…) SIZE p"`。
  干净表上 7 组一起 → q.61/184/62 全部被各自己的组修复。
- 教训：差异在**实现如何部署整集合**，而非 Oracle 的能力。写报告/E2E 时务必用合并单次 gather。

## 3. 修正后的 E2E（Oracle 上 model-vs-truth 闭合）

`scratch/e2e_deploy_oracle.py`（--level 0 --budget 8000，census_mini/oracle 语料）：

| 指标 | predicted | TRUE(部署后) | true/pred |
|---|---|---|---|
| mean | 1.8841 | 2.0326 | 1.079 |
| geomean | 1.7719 | 1.9248 | 1.086 |
| max | 2.7757 | 2.5476 | 0.918 |

=> 用第 3-query 最小语料即可演示：**Oracle 上通用模型预测与真实部署约 8% 内闭合**。

## 4. 多组组合的判别探针（核心新证据）

`none / 部分组 / 正确多组 / 全并组` 对照，读 EXPLAIN E-Rows vs 真实 count。

### 4.1 前导：4 列、两个二列簇

`scratch/probe_oracle_composition.py`（N=1e6，`(a,b)` `(c,d)` 独立，各 P≈0.475）。
查询 `a=1 AND b=1 AND c=1 AND d=1`，truth=225992，独立组合期望 ≈225800.8。

| 配置 | E-Rows | vs truth |
|---|---|---|
| none | 62,594 | 0.277 |
| AB only | 118,848 | 0.526 |
| **AB+CD** | 225,801 | **0.999** |
| ABCD (4-col) | 225,992 | 1.000 |

（注：AB-only 与 CD-only 不对称是确定性的 Oracle quirk，属次要现象，不改变 AB+CD 组合结论。）

### 4.2 加强：6 列 —— 3 个不相交 2-col 组 与 2 个不相交 3-col 组

`scratch/probe_oracle_composition_6way.py`（N=1e6，两种独立簇结构，查询 AND 全部 6 个 =1）。

**场景 I：6 列 = 三个独立 2 列簇 `(a,b),(c,d),(e,f)`，truth=100,257：**

| 配置 | E-Rows | vs truth |
|---|---|---|
| none | 15,675 | 0.156 |
| **3×2col `(a,b)(c,d)(e,f)`** | 100,730 | **1.005** ← 3 组组合 |
| 2×3col（跨簇乱并 `(a,b,c)(d,e,f)`） | 54,115 | 0.540 |
| full-6col | 100,257 | 1.000 |

**场景 II：6 列 = 两个独立 3 列簇 `(a1,b1,c1),(d1,e1,f1)`，truth=187,271：**

| 配置 | E-Rows | vs truth |
|---|---|---|
| none | 15,645 | 0.084 |
| 3×2col | 50,713 | 0.271（2 列抓不住 3 维簇） |
| **2×3col `(a1b1c1)(d1e1f1)`** | 187,131 | **0.999** ← 2 个 3-col 组组合 |
| full-6col | 187,271 | 1.000 |

### 4.3 结论（写入报告时）

- Oracle 对 **6 个合取选择谓词**：当真实相关簇是 2 维时，**3 个不相交 2 列组**组合到 ~1.00×；
  当真实相关簇是 3 维时，**2 个不相交 3 列组**组合到 ~1.00×。arity 2 与 3 的多组组合均被 Oracle 应用。
- **前提**：分解须"切在真实相关簇边界上"且**组间列不相交**。跨簇分组（硬并独立列 / 漏切多维簇）
  会明显变差（0.27–0.54×）。
- 这与通用优化模型的语义精确对齐：**为查询选一组"列不相交、各对齐一个相关簇"的统计**，
  部署后 Oracle 让它们全部生效并组合 → 即模型假设真的兑现（无需 PG 式 OID 修复）。

## 5. 引擎差异：PG vs Oracle（避免过度外推）

| 维度 | PostgreSQL | Oracle（本证据范围） |
|---|---|---|
| 单条 query 用到的 multi-stat | 按 OID 首适者，近似 ≤1 MV/conjunct（v2 依赖 OID-order 扩展层修补） | 可多个不重叠组并行/组合 |
| 组合多组到同一 AND | 受限（OID first-wins；多 2-col 难逼近极稀疏，需更高 arity 单个） | 不相交组组合可达 ~truth（本套实证） |
| 是否需要 OID/FB 排序部署 | **是**（PG 专用扩展层 `optimize_pg_order.py`） | 否（合并单次 gather 即可） |
| cap=1 语义 | 偏 PG 的一档（与首适者一致） | 偏保守；Oracle 允许 per-query 不相交多组（cap=None/乘法类更深） |

边界：
- 本证据覆盖"不相交、独立、中低稀疏度"场景；**未**证明 Oracle 能组合重叠组、海量组、或逼近
  PG 到不了的**极稀疏高维**联合（那些主要受数据/选择率上限限制，而非引擎组合容差）。
- 单组不对称 quirk（§4.1 CD-only）说明 Oracle 单组作用也有版本/场景细节，宜以目标版本实测为准。
- "Oracle 是通用(cap=None)模型更好的验证台"写报告时应表述为**互补**：PG 天然测 one-stat/稀疏 regime
  并需顺序扩展；Oracle 天然兑现"多不相交多组"regime。二者并用最强，且证明通用模型非 PG 专属。

## 6. 产物 / 复现索引

| 内容 | 位置 |
|---|---|
| 修正后 Oracle E2E driver（合并 gather 部署） | `scratch/e2e_deploy_oracle.py` |
| E2E 结果（pred vs TRUE ~1.08） | `results/e2e_deploy_oracle.json` |
| 4 列组合探针 | `scratch/probe_oracle_composition.py` |
| 4 列结果 | `results/oracle_composition_factorial.json` |
| 6 列组合探针（3×2col / 2×3col） | `scratch/probe_oracle_composition_6way.py` |
| 6 列结果 | `results/oracle_composition_6way.json` |
| Oracle L0 全量测量（resumable，可选后台） | `scratch/measure_census_oracle_l0.py` |
| Oracle 并行判定（host RAM 瓶颈；N 连接同实例反而更慢） | `/memories/…/oracle-parallelism-limit.md` + 本节附录 |

### 附录：Oracle 并行化为何不可达（简要）
Docker 无限制，但 Oracle Database Free 实例 `cpu_count=2`、SGA 1.5G、`parallel_max_servers=1`；
同实例 N 连接并发 GATHER 反而更慢（2-worker 并发 speedup ≈0.32×）。宿主机仅 15GB RAM，
复制容器(每份 ~4GB)也只够 ~2–3 份 → 该台 Oracle 全量测量以可续跑串行为宜。

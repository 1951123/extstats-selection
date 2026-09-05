# Y 骨架:跨引擎 statistic selection 论文大纲(草稿 v0.1)

> 体裁:VLDB-style 全文。方向:**method/generalization-first(骨架 Y)**。
> 状态:outline 草稿,供填充证据与打磨。所有"✓已有 / △缺"标注 = 支撑该节的证据现状
> (详见各节 + session memory `paper-framing-debate.md` 的 evidence/claim map)。
> Paper 主文档候选位置:`paper/main.tex`(v1 重构)或 v2 新长文 —— 未定,见 §0 待决。

---

## §0. 贡献(Framing)与体裁决定点

**头号贡献(主张集,候选):**
1. **通用预算化 what+how-much 选择是良好定义的**(sparse 与 multiplicative 是"模型 class"的两档,
   由后端/数据决定;`per_query_cap` 是可选 knob,不是假设)。[△ 需在 §form 把"cap 是 knob、非普适"写清]
2. **model-class-is-a-knob**:同一核心在不同引擎选择不同 class:
   PG 退化为 cap=1 线性档(与 PG planner ≤1 MV/conjunct 一致);Oracle 是"多不相交列组组合"
   (per-query 多 select 可行);DMV 暴露 cap=1 在重尾/极稀疏下的上界。
3. **跨引擎 param 不对称(headline observation)**:
   - PG:λ **约束** param(`statistics_target` 既驱动采样 S=300·θ 又是表示 cap)→ param≤S/300;有
     **可优化、受 λ 约束的 how-much 轴**。
   - Oracle:param ≈ **数据(NDV)的函数**,不是 λ 的函数、也不是自由旋钮;实测桶不随 λ(1/10/100% 平),
     因瓶颈是数据 NDV;Oracle 无 how-much 决策 → 只选 what(列组)+ 每表 λ。
   - 推论:v1/v2 把 param 当决策轴是 **PG 视角**;跨引擎时 how-much 维度是否可优化是**引擎相关**的。
   [贡献已被本会话实证固化]
4. **测量筑基(Protocol-M/A)**:逐候选、逐档隔离测量使 what×how-much 可测;跨引擎。
5. **部署闭合 model-vs-truth(每引擎)**:PG OID-order(FB 排序);Oracle 合并单次 gather;
   部署协议(非逐组)是"模型预测兑现"的隐含前提。

**度量定义(Objective & Metrics)——已定稿 2026-09-05:**
- **通用(可多选/cap-free)模型的唯一主目标 = 几何平均(geo/log 空间)**:per-query 组合是乘性的
  (各选择率相乘),算术 mean 在该处非线性 → log/geo 是被迫且正确的目标。
- **算术平均只是 cap=1 的特例**:cap=1 单例使算术可精确线性化(`SPARSE_LINEAR`),是该档的“计算
  便利”,非通用目标。
- **算术平均在优化后才获得**:以 geo 求解通用问题 → decode 出部署后 per-query q-error → 事后算
  算术 mean 作为报告量。
- 报告口径:**geo(主)+ p90/max(尾)+ 可选事后算术**;两个 model class(cap=1/cap-free)必须在
  **同一 geo 口径**下比较(否则如 2KB 假交叉,源自把一个输出量(算术)拿去比一个 geo 优化模型)。

**体裁决定(未定,open):**
- 改 v1 `paper/main.tex`(PG 主体、Oracle/DMV 进边界)→ 骨架 X 更稳;或
- v2 新全文(Y 头号、Oracle 作第二引擎、DMV 作边界)。本 outline 按 **Y** 展开,但标记哪些节若走 X
  就降级为 discussion/边界证据。

**证据债(决定 outline 里"实验"规模):**
- PG 腿(census/stats_CEB_single/DMV)已报告级 ✓;DMV cap=1 边界 ✓(`milp_effect_time_dmv_*`)。
- Oracle 腿 **薄**:仅 census_mini(3q)+ 5 ad-hoc + synthetic 组合;需 **full census-on-Oracle L0
  (~1.8h 串行)+ stats_CEB_single-on-Oracle L0 + dmv-on-Oracle(L0,需先建 DMV 表)**。
- Oracle param=单点(254/AUTO)已定;表示维塌缩 → 语料成本回到 ~1 候选-slot/每 λ(~1.8h census/份)。

---
## §1. Introduction(略;含 §0 贡献+一句式贴位)

---
## §2. Cross-Engine Background:extended statistics 的表示与采样
- PG:MV 统计(MCV/ndistinct/dependency)→ 单表选择谓词;`statistics_target`;Protocol-M 依赖
  系统目录 + OID 机制(PG-only)。
- Oracle:column group(`DBMS_STATS FOR COLUMNS`);Protocol-A;无 Protocol-M。
- **跨引擎 param 不对称(本节核心,对应贡献 3)**:把"λ(采样)-param(表示)"关系per-engine列成表格。
  | | PG | Oracle |
  |---|---|---|
  | param 是什么 | attstattarget(每对象标量旋钮) | SIZE buckets = 数据 NDV 收敛的上界(非独立旋钮) |
  | λ↔param 关系 | 硬耦合 param≤S/300(statistics_target 两者皆是) | 无耦合;param=f(数据 NDV),λ 不变桶 |
  | how-much 轴可优化? | 是(cap/分辨率是可调决策) | 否(引擎按 NDV 渲染) |
  | λ 对对象的实效 | 限制 param + 决定每次 ANALYZE 深度 | 决定采样深度→影响有限桶的 fidelity(λ_q),非桶数 |
- evidence:PG 机制(cite analyze.c minrows)；Oracle 实测桶 16/15/17、35/35/36 (1/10/100%)。

---
## §3. Measurement:Protocol-M/PG 与 Protocol-A/Oracle(贡献 4)
- 问题:what×how-much 逐候选逐档不可测(宽表几十万候选)。
- Protocol-M(PG):单 ANALYZE + catalog-mask 逐候选隔离;精度≈singleton;测量才是瓶颈叙事。
- Protocol-A(Oracle):逐候选 GATHER;无 mask;诚实成本更高 → scope:Oracle 只在 L0 + 单点 param。
- 跨引擎协议族;每后端"如何隔离测量候选"都落到原生 handle。

---
## §4. Optimization:What + (per-engine)How-Much(MILP;贡献 1+2)
- 通用问题:存预算 C 下选 {colset} 使每查询 error 最小;`y_s`/`x_is`;约束:storage、service、cap。
- **how-much 分两面(正交)** —— 严谨表述,别把"MILP 决定/non 自动"混为一谈:
  1) **表示分辨率(param/桶数)**:单统计刻画多细。
     - PG:`statistics_target` 是 per-object 旋钮 → 网格搜+实测,MILP 按容量级排他给每 (colset)
       选一个 param 档(= 可优化决策轴,即 v1 的 what+how-much)。
     - Oracle:`SIZE AUTO`/NDV 收敛;SIZE64 vs 254 实测同样桶 → **无 per-object 可调轴,引擎自决**,
       单点(254/AUTO)。
  2) **采样深度(λ)**:扫多深(成本 + fidelity)。
     - PG:绑在 target(S=300·target),与面 1 同轴。
     - Oracle:`estimate_percent` 是**整表一次**共享 GATHER 的采样旋钮 —— **仍是我们选**(本工作取
       L0-only),影响有限桶的 fidelity(λ_q),不改变桶数;**不是引擎自动**,且无 per-object 采样
       (Oracle 粒度粗:整表一次而非 per-object)。
  ⟹ 精确口径:表示分辨率 = **PG 可优化(MILP)/ Oracle 引擎决定**;采样 λ = **两引擎都要选**的决策
    (Oracle 更粗)。这句进 abstract/contribution,避免评审把"Oracle 无 how-much"读过头。
- model class 按引擎:
  - cap=1(线性,PG-自然):经验上 PG "每合取 ≤1 MV";DMV 显示其**可达上界与失效边界**。
  - cap=None(乘法/列不相交,Oracle-自然):查询内列不相交多选;Oracle 实测组合可达 ~truth。
- 约束层已内建"查询内列不相交"(overlap-free)以便乘法近似可信 —— 与 Oracle 组合语义对齐。
- `per_query_cap` 明确为 knob,非公理。

---
## §5. Failure boundary:DMV 与 cap=1 的可达上界(贡献 2/3 的实证)
- DMV(全类别、极稀疏重尾):arity-2 + cap=1 下宽 budget 仍留 ~240/1924 不修
  (`milp_effect_time_dmv_L1`:基线 mean~46 → L0~17.35 / L1~6.5);one-stat 不普适。
- ⟹ one-stat 是"PG 投影下的可达上界 + 现象",非普适性质(反映 v2 对 v1 主张的降级)。

---
## §6. Cross-engine deployment & E2E(贡献 5 + headline)
- model-vs-truth:interference-free 预测 vs 真部署;gap 来源 per 引擎:
  - PG:OID-order 首适者 → 需 FB-order/PG 专用扩展层;naive coexist TRUE~6 vs pred 1.40,FB 后 1.505。
  - Oracle:不需要 OID-order;一次合并 gather 部署集合即让各不相交组生效;E2E(pred 1.88→TRUE 2.03,~1.08)。
- **composition observation**(headline):Oracle 组合多不相交组(而非 PG 取一)。证据:
  4-col AB+CD→0.999;6-col 3×2col(簇2维) 1.005 / 2×3col(簇3维) 0.999;cross-cluster 乱并更差。
- 部署协议提示:整集合一次 gather;勿逐组重采样(那是假干扰)。

---
## §7. (若 X 才补/或精简)单引擎深潜 / fidelity / maintenance
- fidelity-λ(PG)、storage-vs-ANALYZE mismatch(v1 已有)。Y 中降为 discussion 或并入相关节。

---
## §8. Related work & taxonomy 更新
- 索引/统计选择 vs 本工作;跨 DBMS 统计选择(新维度);"how-much 依赖引擎"定位。

---
## §9. Discussion / Open
- Oracle 高 NDV 组合深 λ 是否 auto 更细(未测)→ 预留开点。
- cap 放开到 K 的整体建模(目标非线性)独立工作。
- 跨引擎 how-much(表示分辨率)是 DBMS 特定:PG 可优化、Oracle 引擎决定;采样 λ 两引擎都要选。

---
## Experiments Gating(落 outline 节前需补):
1. full census-on-Oracle L0(串行 ~1.8h;表已在;单点 param)。
2. full stats_CEB_single-on-Oracle L0(表已在)。
3. dmv-on-Oracle L0(需先建 DMV 表,11.6M 行)。
若不投 Y 头号(走 X),Oracle 保持 mechanism(synthetic + 3q E2E)即可,毋须 1-3。

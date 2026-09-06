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
3. **how-much:GRID = 通用方法, AUTO = 高级但 DBMS-条件性能力(头号主张,与用户定稿 2026-09-05)**:
   - GRID = 通用/可移植 optimizer 方法(引擎无关,portability-first):枚举显式分辨率档并实测,MILP
     按 workload+budget 分级。任何引擎都能跑,不需要引擎提供特别能力;是我们方法的可移植核心。
     诚实的 caveat:**grid 作为"方法"通用,但它的 per-object how-much *威力* 仍引擎条件性** —— 只在
     引擎暴露 per-object 分辨率旋钮时(PG `statistics_target`)可操作 → 这正是 grid 在 PG 最能发力
     的原因(不是 grid 属于 PG)。
   - AUTO = 高级但 DBMS-条件性能力层(引擎提供才可用:Oracle AUTO_SAMPLE_SIZE/自适应统计):可用时
     常优于自跑 grid —— 引擎有内建数据访问、低成本自适应决定 how-much。**不是与 grid 对等的阵营**,
     而是"引擎如提供则优先用"的高级许可证。
   - 关系(非两对等阵营的取舍):AUTO = 引擎给的"高级能力";GRID = 通用/回退方法(无 auto 的引擎、
     或需 auto 没有的逐对象 workload 控制时用)。PG 显示 grid 不只是"回退":PG 的 per-object 旋钮
     处正是 grid 最强大、auto 无法取代的工作负载敏感层。
   - 推论:v1/v2 把 param 当决策轴是 **PG 视角**(PG 恰好暴露了旋钮),但跨引擎时 grid 是**通用算法**、
     auto 是**可用则启用的 native accelerator**。论文研究 = "何时把 how-much 委派给引擎 AUTO、何时
     用通用 grid 自控",而非引擎优劣。
   [修正(2026-09-05):AUTO 探针(all 3 bench,AUTO=满扫)后 → Oracle 采样深度仍手动 grid(Protocol-A\n     逐候选满扫不可行),仅表示分辨率(桶)归 AUTO;见 §0 证据债/§4。]
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

**报告分母口径(Reporting denominator)——已定 2026-09-06:** 所有 workload 级 mean(e.g. baseline/
deployed 算术 mean、geo、p90/max、n_unrepaired)统一以 **candidate-bearing(arity-2,有相关候选)的查询集合**
为分母(以语料目录中实际存在的 `<qid>.json` 为准,`load_lambda_problem`→`list_qids` 即按此枚举),排除
no-candidate 与 truth==0(q-error 无定义)的查询。三 bench 的具体分母:
  | bench | 总 query | 报告分母(candidate-bearing) | 另排除(truth=0) | no-cand 被排除占比 |
  |---|---|---|---|---|
  | census | 468 | **467** | 0 | 0.2% |
  | dmv | 1965 | **1926** | 2 (dmv.173/943) | 2% |
  | stats_CEB_single | 632 | **180** | 0 | **71.5%** |
  ⚠️ 关键不对称:census(DMV 近全覆盖(≥98%),排除少数无实质影响;但 **stats_CEB_single 只对 180/632≈28.5%
  的子集报告**——其余 452 条无相关性候选、多半本就准、被整体排除。该 bench 的 mean 是对"能用 ext-stat
  改进的那部分"的优化视图,不是整盘 workload 的改善幅度。跨 bench 并列时,每个 bench 必须标注
  "report over N candidate-bearing / M total(ratio)",不可把 stats_CEB_single(28.5%)与 census/DMV(~100%)
  的 mean 当作同构整盘量并列解释;若要"整盘"对照,可另补 no-cand 以其 baseline 计入的一行。
  (测量端 driver measure_sgrid/census_parallel/dmv_parallel/stceb_parallel 均只产出 candidate-bearing
  文件;故语料文件数 == 报告分母。权威引用见 docs/reporting-convention.md。)

**体裁决定(未定,open):**
- 改 v1 `paper/main.tex`(PG 主体、Oracle/DMV 进边界)→ 骨架 X 更稳;或
- v2 新全文(Y 头号、Oracle 作第二引擎、DMV 作边界)。本 outline 按 **Y** 展开,但标记哪些节若走 X
  就降级为 discussion/边界证据。

**证据债(决定 outline 里"实验"规模):**
- PG 腿(census/stats_CEB_single/DMV)已报告级 ✓;DMV cap=1 边界 ✓(`milp_effect_time_dmv_*`)。
- Oracle 腿 **薄**:仅 census_mini(3q)+ 5 ad-hoc + synthetic 组合;需 **full census-on-Oracle
  + stats_CEB_single-on-Oracle + dmv-on-Oracle(表 2026-09-05 已装到 Oracle,11.6M)**。
- **Oracle 采样深度(estimate_percent)手动 grid(1/10/100)—— 修正 2026-09-05**:AUTO 探针证实
  Oracle AUTO_SAMPLE_SIZE 在我们所有表(含 11.6M DMV)都=满扫(100%),无自动部分档。而 Protocol-A
  (无 catalog-mask/M)下必须对**每个两列候选单独 gather 实测**其 fidelity;满扫每候选皆不可行。
  ⟹ Oracle 必须保留 estimate_percent 的**手动档 grid**(既有 ladder 1/10/100)以给 candidate 一个
  廉价部分扫测量点,再让 MILP 在价格(fidelity per scan 档)下选;这是**测量协议成本驱动**,不是"部分
  采样是正确部署口径"。仅 per-object 表示分辨率(桶/SIZE)仍引擎自决(NDV,单点 254,免费随同一 gather
  决定,不额外花候选测量成本)。AUTO 只用于 natural baseline/部署默认,不用于逐候选 fidelity 扫描。

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
  | how-much(采样深度 λ)决策面 | grid:λ 档 + per-object 分辨率旋钮都可查 | 手动 grid estimate_percent(1/10/100):AUTO=满扫不可行,须廉价部分扫测量候选 |
  | how-much(表示分辨率/桶)决策面 | 同 statistics_target(与 λ 耦合) | 引擎自决(NDV 渲染,单点 254),免费且不加测量成本 |
  | how-much 谁在管 | 我们(MILP 分层) | 采样档我们选(grid);桶数引擎定 |
- evidence:PG 机制(cite analyze.c minrows)；Oracle 实测桶 16/15/17、35/35/36 (1/10/100%)。

---
## §3. Measurement:Protocol-M/PG 与 Protocol-A/Oracle(贡献 4)
- 问题:what×how-much 逐候选逐档不可测(宽表几十万候选)。
- Protocol-M(PG):单 ANALYZE + catalog-mask 逐候选隔离;精度≈singleton;测量才是瓶颈叙事。
- Protocol-A(Oracle):逐候选 GATHER;无 mask;诚实成本更高 · AUTO 探针证实 AUTO_SAMPLE_SIZE=满扫;
  逐候选满扫不可行 → scope:Oracle 用 **estimate_percent 手动档 grid(1/10/100)** 给每候选廉价部分扫测量;
  仅 per-object 表示(桶)引擎定。即 Oracle 仍有 λ 维度(作用于采样深度),无 per-object 分辨率维度。
- 跨引擎协议族;每后端"如何隔离测量候选"都落到原生 handle。

---
## §4. Optimization:What + (per-engine)How-Much(MILP;贡献 1+2)
- 通用问题:存预算 C 下选 {colset} 使每查询 error 最小;`y_s`/`x_is`;约束:storage、service、cap。
- **how-much 两面(修正 2026-09-05)—— 采样深度 grid 是测量必需的,表示分辨率才引擎定**:
  1) 采样深度(estimate_percent / λ):**Oracle 也手动 grid(1/10/100)**,因为 AUTO_SAMPLE_SIZE=满扫,
     而 Protocol-A 须逐候选真实 gather 测 fidelity,满扫每个候选不可行 ⟹ 廉价部分扫测量点是协议必需。
     这是**测量成本驱动**(非"部分采样即正确");grid 顶档 100%=忠实的引擎满深度,AUTO 满扫只用于
     natural baseline/部署,不用于逐候选扫描。MILP 在"每档 scan 代价 vs fidelity"间按预算选档。
  2) 表示分辨率(桶/SIZE):**引擎自决**(NDV 渲染,单点 254;SIZE64-254 实测桶同 → 无可 grid 的 per-
     object 旋钮),且决定它是免费的(随同一 gather 扫描发生,不另花候选测量成本)→ 保留引擎 AUTO。
  ⟹ 精确口径(进 abstract/contribution):跨引擎**可 grid 的 how-much 轴**=(a)PG:per-object 分辨率
    + λ 都可 grid 且耦合;(b)Oracle:表示分辨率引擎定(无旋钮),但**采样深度仍须 grid**(否则 Protocol-A
    无法廉价测量候选)。所以 oracle 并非"how-much 全交 AUTO",而是"表示归 AUTO、采样档自 grid";
    且两者都受同一约束:测量(Protocol-A 逐候选)是瓶颈。研究问题不变 = "何时值得为 workload-aware
    的 how-much 付出 grid 测量成本" ,并量化 AUTO 满扫(sampling)在逐候选测量上为何不可行(full-scan
    成本)。
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
- Oracle 采样深度手动 grid 修正(2026-09-05):AUTO_SAMPLE_SIZE=满扫即使 11.6M DMV 亦然(三 bench 全验证);
  Protocol-A 逐候选满扫不可行 → Oracle 保留 estimate_percent 档 grid(1/10/100)作候选测量点。开:是否有
  Oracle 原生的"廉价多候选共享扫"能绕开(否则满扫只用于自然 baseline/部署,不用于逐候选扫描)。
- Oracle 高 NDV 组合、显式给更高 fidelity(非 AUTO 表示)是否更好(未测)→ 作为对照开点。
- cap 放开到 K 的整体建模(目标非线性)独立工作。
- 跨引擎 how-much 分两层:表示分辨率 = PG 可 grid(旋钮)/Oracle 引擎自决(无旋钮,免费);采样深度 =
  两引擎 Oracle/PG 都 grid(测量必需,Protocol 逐候选成本驱动)。开 = "何时值得 grid 测量成本" + 量化
  AUTO 满扫在逐候选 Protocol-A 上为何不可行(full-scan × 候选数)。

---
## Experiments Gating(落 outline 节前需补):
1. full census-on-Oracle(既有 estimate_percent 档 grid;表已在)。
2. full stats_CEB_single-on-Oracle(同上;表已在)。
3. dmv-on-Oracle(DMV 表 2026-09-05 已装 Oracle 11.6M;待在其上跑)。
4. **待决**:是否需 Oracle 全档(grid 的 L1/L2=10/100%)语料以建 price-vs-fidelity 曲线(AUTO=满扫
   证实为 100%;若报告单档 fidelity,现有 1% 语料足够;若逐档扫描则重测更贵)。
若不投 Y 头号(走 X),Oracle 保持 mechanism(synthetic + 3q E2E)即可,毋须 1-4。

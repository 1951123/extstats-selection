# Experiments Roster — 后续实验花名册（两代口径对照 + 已做/待做）

> 用途：论文实验的唯一巡检清单，避免混用"两代"数字。派生根语料 = `docs/measurement-matrix.md`
> 的 **S-grid 6 单元**；`reporting` / 分母见 `docs/reporting-convention.md`。

## ✓ 执行主线（按此顺序完成，2026-09-06 定）
论文实验按**三个层次**推进；每层产出都对下一层是输入、都依赖 measurement-matrix 语料格：

| 层 | 任务（对应 roster 节） | 状态 |
|---|---|---|
| **L1 测量（S-grid）** | 6 格语料闭合：PG×3 ✅、stats_CEB/single×oracle ✅、dmv×oracle ⏳、**census×oracle ❌(缺口#1，需定范围)** | 进行中 |
| **L2 优化（MILP-budget）** | 在 S-grid 语料上跑 storage/maint 预算曲线（统一 `measure_milp_curve`）：**PG×3 已完成**(census 1.40/dmv 6.51/stats 1.08,census maintenance→1.40 等)；Oracle 版待 L1 其语料 | PG ✅ / Oracle 待语料 |
| **L3 部署（End-to-end）** | 把 L2 选中集**真部署** → TRUE q-error vs L2 预测(model-vs-truth)：PG 可先做 deploy + FB-order 干扰修复；Oracle E2E 待 L1 oracle 语料 | 待做(先 PG) |

- L2(PG) 已闭 —— 结论即 feed paper §4/§5。
- L3 是"模型兑现"裁决(C 贡献 5/§6)：interference-free 预测能否经真部署(E2E/PG FB-order；Oracle 合并 gather)兑现。
- L2/L3 的 Oracle 侧都受 L1 的两缺口(尤其 census/Oracle)钳制。

## 0. 两代口径（铁律：不混用数字）
| 代 | 语料 | 时间 | 位置 | 用途 |
|---|---|---|---|---|
| **dense（pre-S-grid）** | 固定%/dense-param(v1 路线) | 2026-09-02..05 | `results_archive_pre_sgrid_20260905/` | **历史/原型证据**：机制(FB-order、Oracle composition、migration 边界)仍可引用；数字**非当前口径不可并列** |
| **S-grid（当前派生根）** | dataset-bound S_rows{30000,300000} 跨 PG+Oracle | 2026-09-05 起 | `results/per_lambda/<bench>/<backend>/` | 论文正文实验应跑在这 6 格上 |

论文正文需在全 S-grid 上重做后续实验；dense 版只作历史基线对照（标 "dense-param 口径"）。

## A. 派生根现状（measurement-matrix 6 单元）
| bench | PG | Oracle |
|---|---|---|
| census | ✅ 467/467 | ❌ 0/467（缺口#1）|
| stats_CEB_single | ✅ 180/180 | ✅ 180/180 |
| dmv | ✅ 1926/1926 | ⏳ 270/1926 asynch（缺口#2 in progress）|

## B. dense 历史实验档案（results_archive_pre_sgrid_20260905/，3722 json）
**类别（机制结论可引用，数字=dense 口径）：**
- PG dense deployment+interference：`e2e_deploy_L1_100KB(.disjoint)`、`e2e_order_L1_100KB`、`e2e_true_ordered_L1(_fb)`、`e2e_order_fb`、`e2e_stceb_single_L1_80KB(_fb)`
- dense 预算曲线：`milp_effect_time(_L0/L1/census_oracle_L0)`、`milp_effect_time(_cebsingle/_dmv)_L*`、`milp_maint_time(_stceb _L*)`
- E2E/migration/Oracle：`e2e_migrate_b_L1_B80000`、`_migrate_b_base/dep`、`e2e_deploy_oracle`、`dmv_baseline`
- 跨引擎机制：`cross_pg_oracle_k2/topk/baseline/focus`、`oracle_composition_{6way,factorial}`、`oracle_single_group_screen`、`oracle_k2_coininstall`、`oracle_range_vs_eq_probe`、`cross_baseline`
- dense 原 perf_lambda / figures
- 对应 dense 工具脚本多数仍在 scratch（`e2e_deploy_*.py / e2e_order_deploy / e2e_migrate_b / fb_order_save / reorder_micro / deploy_phase2_order` 等）——S-grid 可作"复用其机制/改造重跑"参考，读 dense path 的不直接用。

## C. 已在 S-grid 上做（当前 results/）
- `report_pg_sgrid_3bench.json`（PG 三 bench：baseline + per-query best-candidate(q-error 分布)）
- 6× `milp_{storage,maint}_sgrid_{bench}.json` + `milp_curve_{storage,maint}.png`（统一预算→质量曲线，cap=1/mean/candidate 分母）
- figures：`milp_curve_*.png`

## D. 待做（S-grid 派生，列依赖与工具状态）
**D1. 可在 PG 侧立即推进（不依赖 Oracle 语料）：**
| # | 实验 | 产出/outline | 现状 |
|---|---|---|---|
| D1.1 | S-grid deploy / model-vs-truth（PG 三 bench：把 optimizer 选中集真部署→TRUE qerr vs pred）| §6 贡献5/§4 | 需在 S-grid 语料重跑/复核；dense `e2e_deploy*.py` 逻辑可参考、读旧 path 需换 |
| D1.2 | S-grid FB-order（census/dmv 重叠高压处干扰修复）| §6 | dense `e2e_true_ordered_fb`/`fb_order_save` 机制可引用；S-grid 重跑 |
| D1.3 | S-grid 跨 bench 汇总表/效果图（三 bench PG 基线/best/model）| §4/§5 | report+curve 已有；汇总成形 |
| **D2. 依赖 Oracle 语料（需 measurement-matrix 两缺口闭合）** |
| D2.1 | Oracle dmv / census：storage+maint 曲线 | §4 | 等缺口#1、#2 闭合；Oracle Protocol-A 贵 |
| D2.2 | Oracle E2E / deployment（合并 gather 一次布署→TRUE）| §6 | dense `e2e_deploy_oracle` 机制参考；S-grid 重跑 |
| **D3. 补实验/敏感（可穿插）** |
| D3.1 | DMV cap 敏感性（cap∈{1,2,3,None} 证 251-unrep 中 "需多统计 vs 真 2-col 缺列" 拆分）| §5 | 模型层扫，不需 Oracle |
| D3.2 | stats_CEB_single 分母对称警示行（over 180/632 + no-cand 计价整盘）| report 口径 | reporting-convention 已定，落地数字行 |

## E. 主依赖缺口（阻塞 D2）
1. census/Oracle：空 —— 决定范围(全 467/L0/子集) + CLIMATE 干净(DELETE_TABLE_STATS)+ asynch。
2. dmv/Oracle：in progress(270/1926 asynch, err=0)，让它跑完。
Oracle 腿未闭前，"跨引擎 main 汇总/§6 headline"只能写 PG+机制（Oracle composition 引用 dense 结论时标注）。

## 复现索引
- S-grid 测量：`scratch/measure_sgrid.py`(oracle/census 串行)、`measure_{census,dmv,stceb}_parallel.py`(PG mirror)
- S-grid 预算曲线：`scratch/measure_milp_curve{,_all}.py` → `results/milp_{storage,maint}_sgrid_*.json`
- report：`scratch/analyze_pg_sgrid.py`；图：`scratch/plot_milp_curve.py`

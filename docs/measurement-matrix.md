# Measurement Matrix — 跨引擎 × 三 bench 的 S-grid 实测（派生起点）

**角色：所有实验数据的派生根。** storage/maint MILP 曲线、optimize、deploy/E2E、跨引擎结论，
全部从下方 6 个单元格的语料（`results/per_lambda/<bench>/<backend>/`）读取派生。因此每个格的
**完整性 + 口径一致性是硬前提**：任何下游数据都必须能用其 `<qid>.json` 复现，缺格即不可写该
bench×engine 的下游。

## 6 单元状态（2026-09-06 盘点）
| bench | PG | Oracle |
|---|---|---|
| census | ✅ 467/467 | ❌ **0/467（空，缺口 #1）** |
| stats_CEB_single | ✅ 180/180 | ✅ 180/180 |
| dmv | ✅ 1926/1926 | ⏳ 270/1926（进行中 asynch, err=0）|

- 分母 = candidate-bearing(arity-2) 查询集合（`docs/reporting-convention.md`；census 467/468、
  dmv 1926/1965、stats_CEB_single 180/632）。
- 每格语料：`<qid>.json`(每含 actual + by_lambda{L0,L1}，candidate 带 cols/param/qerror/size_bytes/
  maint_var) + `_meta.json`(去重形态：tiers 纯声明、table_s_rows 权威 per-owner S)。
- 校验：PG 三格 0 err / 0 malformed / 0 缺档；stats_CEB_single/oracle 已抽查为干净 S-grid(180 files)。

## S-grid how-much（每格同语义，跨引擎 tied）
- 全局 S_rows = {30000, 300000}，按每格 owner 表 N 实现 dataset-bound S：
  - **PG**：L0/L1 的 single_target（statistics_target）由 S 与表 cap 落位（.dmv L0=100/L1=1000 等）。
  - **Oracle**：per-owner-table estimate_percent = 100·min(S, N)/N（如 .postLinks 小表 L0=L1→100%
    满扫；大表后随 S 递增）。表示分辨率(桶)引擎自决。
- raw 单 query 平均成本：PG(Protocol-M)轻；Oracle(Protocol-A)重 → Oracle 腿是时间瓶颈。

## 缺口与派生影响
- **缺口 #1(census/Oracle 空)**：若有全 census-on-Oracle，统计选择 main table 才能报跨引擎完整。
  之前该单元格 ORA-00904 已被根因定位（CLIMATE 孤儿 extension→DELETE_TABLE_STATS 清）+ asynch 已改 I/O；
  仍待定范围(全 467 或代表性子集)与重测。
- **缺口 #2(dmv/Oracle) 未完**：270/1926 asynch 进行中，~30s/q，err=0；让其跑完即闭。
- 派生铁律：**未闭的格不写其 storage/maint 曲线**；跨 bench/引擎并列时标 "over N/M(ratio)"（见
  reporting-convention）。

## 复现索引（每格产生脚本）
- PG × census/dmv/stats_CEB：`scratch/measure_census_parallel.py` / `measure_dmv_parallel.py` /
  `measure_stceb_parallel.py`(mirror 池并行)。
- Oracle：`scratch/measure_sgrid.py --backend oracle --bench …`（串行 Protocol-A）。

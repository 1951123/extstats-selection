# docs — 索引

v2 文档按职责分四份主文档（全部以 **S-grid** 语料为事实源，确数经 `results/`
重派生，不引用 archive 的旧数）：

| 文档 | 讲什么 | 典型产物/引用 |
|---|---|---|
| [`architecture.md`](architecture.md) | **设计思路**（为何这样解、系统分层、机制取舍） | 引 measure/optimize/deploy，不含确数 |
| [`measure.md`](measure.md) | **测量**：S-grid 语料、逐 bench/db 覆盖、协议、candidate-bearing 分母 | `results/report_pg_sgrid_3bench.json` |
| [`optimize.md`](optimize.md) | **优化**：由 measurement 选统计；storage/maint 预算×质量曲线、argmin 档 | `results/milp_{storage,maint}_sgrid_*.json` |
| [`deploy.md`](deploy.md) | **部署**：真 EXPLAIN、planner 干扰、四策略、引擎/数据边界 | `results/e2e_*_sgrid_*.json` |

**职责边界（快速导航）**
- 想知道"为什么这么设计" → `architecture.md`
- 想知道"语料/测了什么/哪些引擎覆盖" → `measure.md`
- 想知道"该投多大预算、选 L0/L1、质量曲线" → `optimize.md`
- 想知道"部署到真值后的 gap 与修复" → `deploy.md`

**历史**：早期文档（dense 时代各篇架构/实验笔记）归档于 [`archive/`](archive/)，仅作
历史留存；涉及结论要以 S-grid 语料重派生后再采用，勿直接沿用其中旧档读数。

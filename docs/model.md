# 研究模型 (Research Model) — 当前真相源

> 本文档回答「这个研究现在相信什么」。它描述**变量与因果方向**，不是代码。
> 派生顺序必须是
>   `idea → 本文档(model) → architecture/measure/optimize → src → results`，
> **不能逆序** — 代码 / 旧结果不能反过来重新定义研究模型。
>
> 当研究模型改变时：先在此更新本文档（+ 一条简短 change 笔记），再派生到
> architecture / measure / optimize 与代码。本文档是「真相源」；`architecture.md`
> 只是模型的一个视图，`results/` 绑定它产生时的 commit 与 config。

---

## 1. 主实验变量 (Primary experimental variables)

本研究的因果方向为：

```
S  ──(requested sampling)→  S_realized(t,S)  ──→  λ(t,S)     (derived)
 │
 └────────────→  p   (representation parameter, 在给定 S 下选择)
```

- **S** — requested sampling rows。这是**研究主动搜索 / 定义的一级变量**
  （config `SAMPLING_LEVELS`：L0 = 30 000，L1 = 300 000）。
- **p** — representation parameter（PG `attstattarget` 标量 / Oracle `SIZE`），
  在给定采样层的 lattice cap `p ≤ S/300` 内由 inner optimizer 选择。

## 2. 派生量 (Derived quantities)

对表 `t`、请求采样行 `S`、表行数 `N_t`：

- 实际采样行：`S_realized(t,S) = min(S, N_t)`
- 采样占比（reporting / fidelity 指标，**不是搜索轴**）：
  `λ(t,S) = S_realized(t,S) / N_t`

> 关键推论：`λ` 完全由 `S` 与 `N_t` 派生，因此**不是独立实验控制变量**。
> 过去把模型描述成 `capacity / λ` 优先是错的；正确是 `S`（sample-first / S-grid）。

## 3. 优化 (Optimization)

固定一个采样层 `S`：

1. 实现 `min(S, N_t)` 的采样深度；
2. 由 realized sample 得 `λ`（仅作报告）；
3. 测 no-ext 基线 `e^0(S)`；
4. 测各候选表示 `(colset, p)`，`p ≤ S/300`；
5. inner MILP 在表示轴 `p` 上选择；
6. outer loop 跨 S-grid 比较：`S ∈ {30k, 300k, …}`。

## 4. DBMS 职责 (Backend responsibility)

- **Core** 只定义抽象的 `SamplingLevel`（S-grid 层索引）与表示参数 `p`，不碰引擎。
- **Backend** 把同一抽象层映射到引擎原生采样语义：
  - PG：`statistics_target = S/300`
  - Oracle：`estimate_percent = 100 * min(S,N)/N`

## 5. 词汇 — model / doc / code 三角一致

| model | 文档用语 | code 锚点 |
|---|---|---|
| `S` | requested sampling rows | `SAMPLING_LEVELS`, `SamplingLevel`, `sample_rows_at_level()` |
| `S_realized` | min(S, N_t) | `sample_rows_per_level()`（cap at N） |
| `λ` (derived) | derived sampling fraction | `sample_percent_at_level()`（报告/校验用） |
| `p` | representation parameter | `build_stat_param(obj, p)`, `PhysicalStat.level`(=p, 见 optimize_sgrid NOTE) |

若三者不一致，停下：先改 `model.md`，再同步文档与代码，而不是硬改代码迁就。

## 6. Change log（极简；详细动机的历史见 git 与 `docs/archive/`）

- **2026-09-07 — sample-first (S-grid)**：废弃 v1 `capacity / λ` 优先的措辞。
  原因：`λ = min(S,N)/N` 由 `S`、`N_t` 派生，非独立轴。后果：采样 API 用 `S` 术语，
  outer 搜索遍历 S-grid；`by_lambda` 等持久结果 key 因历史语料保持不变。
  （代码落点：sampling-first 重命名 + `Capacity → SamplingLevel`。）

# 冻结契约 v1 · 判决书说明

日期：2026-10-27。Stage 1 第一步产出。

## 这是什么

`golden_python.json` 是未经改动的 Python 参考引擎，对四个注册来源跑出来的**标准答案**。
它是 Node 移植的唯一法官：Node 版每移植一个模块，都要逐字段与这份文件比对。

**性质提醒**：文件里所有数字都是合成演示值（SYNTHETIC），不代表任何真实企业口径。

## 怎么重新生成

```
D:/AI_Command_Workspace/runtime/cashflow_v1/Scripts/python.exe -B D:/CashflowRiskWorkbench/migration/dump_golden.py
```

只读。不写入 `program_v1`，不修改任何引擎文件。

## 文件里绑了什么

- `algorithm_sha256_per_file`：五个引擎文件（eight_sheet_engine / account_binding / cash_selection / source_audit / program_engine）各自的 SHA256。
  **任何一个文件被改动，这份基线就作废而不是被动漂移。**
- `sources`：四个来源各自的完整返回值，含 `amounts`、`cashflow_groups`、`reconciliation_state`、`can_save`、错误信息原文。

## 本次实测到的标准答案（与交接文档逐条核对一致）

| 来源 | 状态 | 可保存 | opening | inflow | outflow | net | 计算期末 | 独立期末 | 差异 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| synthetic_eight_sheet_v1 | calculated / matched | 是 | 100000.00 | 185000.00 | 108000.00 | 77000.00 | 177000.00 | 177000.00 | 0.00 |
| program_demo_v1 | calculated / matched | 是 | 100000.00 | 130000.00 | 90000.00 | 40000.00 | 140000.00 | 140000.00 | 0.00 |
| program_difference_v1 | difference_pending_review | **否** | 100000.00 | 130000.00 | 90000.00 | 40000.00 | 140000.00 | 139000.00 | **-1000.00** |
| manufacturing_sample_v1 | error（blocked） | 否 | — | — | — | — | — | — | — |

主来源三类明细：经营 +60000.00（130000/70000）、投资 -15000.00（5000/20000）、筹资 +32000.00（50000/18000）。

**对平结论**：与交接文档第 6 节的 185000 / 108000 / 77000 / 177000 / 差异 0，以及第 5 节的 139000 vs 140000 差异 -1000 完全吻合。

## 移植纪律（不得违反）

1. Python 引擎**一行不改**，它是法官。
2. Node 结果与本文件不一致时，**永远先判定 Node 错**，直到查清根因。
3. 不允许出现"两边都有理"的妥协判定。
4. 金额一律走整数分（本文件里的字符串金额保留两位，作为比对基准）。

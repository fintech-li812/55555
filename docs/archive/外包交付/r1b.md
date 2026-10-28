# MCP 验收测试报告 · CashflowRiskWorkbench

- 目标入口：`http://127.0.0.1:8201/mcp`（来源：命令行参数或环境变量 MCP_BASE_URL）
- run-tag：`R1`（request_id 前缀，用于复现）
- 生成时间：2026-10-21T23:47:11+08:00（本地 +08:00；服务端 server_time 为 UTC）
- 凭据：未提供

---

## A01 初始化与工具发现 — PASS

- **请求摘要（脱敏后）**：initialize → notifications/initialized → tools/list
- **响应关键片段**：{"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "finance-risk-mysql", "version": "1.0.0"}}
- 补充：应有 14 / 实有 14
- 补充：协议版本：客户端提议 2025-06-18 → 服务端 2025-06-18
- 补充：initialize HTTP 200（json）
- 补充：notifications/initialized：HTTP 202（2xx，符合预期；202/204 未明确，如实记录）
- 补充：Mcp-Session-Id：sha256:9c5f4081（仅指纹，原值不落报告）
- 补充：未实装工具（0）：无
- **判定**：PASS
- **理由**：握手成功，protocolVersion 非空，tools 非空且均含 name/inputSchema

## A02 写一笔演示告警 — FAIL

- **请求摘要（脱敏后）**：tools/call log_to_mysql；request_id=R1-A02
- **响应关键片段**：HTTP 200；{"ok": false, "code": "IDEMPOTENCY_CONFLICT", "message": "同幂等键不同内容", "request_id": "R1-A02", "server_time": "2026-10-21T15:00:00Z", "data": {}}
- **判定**：FAIL
- **理由**：业务对象 ok 不为 true（isError=True），写入未成功

## A03 同键同内容重试 — SKIP

- **请求摘要（脱敏后）**：-
- **响应关键片段**：-
- **判定**：SKIP
- **理由**：A02 未成功写入，幂等重试用例前提不成立，记 SKIP

## A04 同键不同内容 — SKIP

- **请求摘要（脱敏后）**：-
- **响应关键片段**：-
- **判定**：SKIP
- **理由**：A02 未成功写入，冲突用例前提不成立，记 SKIP

## A05 非法入参被拒 — PASS

- **请求摘要（脱敏后）**：tools/call log_to_mysql × 3（超长 id / 无时区时间 / 数字金额）
- **响应关键片段**：    - ① 超长 request_id（200 字符）：HTTP 200；rejected=True；code=-32602；{"code": -32602, "message": "request_id 长度超限（200，允许 1~128）"}  
    - ② occurred_at 不带时区（2026-10-21T09:00:00）：HTTP 200；rejected=True；code=-32602；{"code": -32602, "message": "occurred_at 必须为带时区的 RFC3339"}  
    - ③ payload 金额传数字（1000000 而非 "1000000.00"）：HTTP 200；rejected=True；code=-32602；{"code": -32602, "message": "payload.amount 金额必须为十进制字符串，收到 int"}
- **判定**：PASS
- **理由**：三次非法入参全部被拒，未入库成功

## A06 伪造租户字段 — PASS

- **请求摘要（脱敏后）**：tools/call log_to_mysql；额外注入 tenant_id=evil-corp
- **响应关键片段**：HTTP 200；{"code": -32602, "message": "未声明字段 tenant_id（additionalProperties=false）"}
- **判定**：PASS
- **理由**：服务端拒收含伪造租户字段的请求（code=-32602）

## A07 精确规则查询 — SKIP

- **请求摘要（脱敏后）**：-
- **响应关键片段**：-
- **判定**：SKIP
- **理由**：A02 未成功写入，精确查询用例前提不成立，记 SKIP

## A08 模板不存在 — PASS

- **请求摘要（脱敏后）**：tools/call load_param_template；industry_key=nonexistent_industry
- **响应关键片段**：HTTP 200；{"ok": true, "code": "OK", "message": "未找到模板", "request_id": "-", "server_time": "2026-10-21T15:00:00Z", "data": {"found": false, "matched_version": null}}
- **判定**：PASS
- **理由**：found=false，且未返回其他行业模板

## A09 无凭据到账验证 — PASS

- **请求摘要（脱敏后）**：tools/call verify_payment；method=bank_api；证据引用为占位引用（无真实凭据）
- **响应关键片段**：HTTP 200；{"code": -32602, "message": "未声明字段 currency（additionalProperties=false）"}
- **判定**：PASS
- **理由**：服务端以 JSON-RPC 错误拒绝（code=-32602），未被记为已验证

## A10 模型自证已批准 — PASS

- **请求摘要（脱敏后）**：tools/call log_exception；必填字段齐全，approval_ref=APPROVAL-FAKE-0001（不可核实）
- **响应关键片段**：HTTP 200；{"ok": false, "code": "APPROVAL_UNVERIFIED", "message": "审批凭据无法核实，不记为已批准例外", "request_id": "R1-A10", "server_time": "2026-10-21T15:00:00Z", "data": {}}
- **判定**：PASS
- **理由**：服务端拒绝自证批准，code=APPROVAL_UNVERIFIED

## A11 空反馈统计 — PASS

- **请求摘要（脱敏后）**：tools/call query_feedback_stats；窗口[2026-10-20T23:47:11+08:00, 2026-10-21T23:47:11+08:00)
- **响应关键片段**：HTTP 200；{"ok": true, "code": "OK", "message": "无反馈样本", "request_id": "-", "server_time": "2026-10-21T15:00:00Z", "data": {"found": false, "label_counts": {"accurate": 0, "inaccurate": 0, "pending": 0}}}
- **判定**：PASS
- **理由**：未出现任何「准确率」字段；计数字段=(空统计/零计数)

## A12 通知语义探测 — INFO

- **请求摘要（脱敏后）**：GET http://127.0.0.1:8201/mcp（仅一次探测，不维持长连接）
- **响应关键片段**：HTTP 405；Content-Type=-；响应体=
- **判定**：INFO
- **理由**：GET 探测返回 HTTP 405；200/405 均为 INFO，不判 FAIL（⚠ 协议未明确 GET 语义）

---

## 汇总

| 用例 | 标题 | 判定 |
| --- | --- | --- |
| A01 | 初始化与工具发现 | PASS |
| A02 | 写一笔演示告警 | FAIL |
| A03 | 同键同内容重试 | SKIP |
| A04 | 同键不同内容 | SKIP |
| A05 | 非法入参被拒 | PASS |
| A06 | 伪造租户字段 | PASS |
| A07 | 精确规则查询 | SKIP |
| A08 | 模板不存在 | PASS |
| A09 | 无凭据到账验证 | PASS |
| A10 | 模型自证已批准 | PASS |
| A11 | 空反馈统计 | PASS |
| A12 | 通知语义探测 | INFO |

PASS=7  FAIL=1  SKIP=3  INFO=1

建议退出码：**1**（有 FAIL → 1；全 PASS/SKIP → 0；连不上服务器 → 2）

---

## 人工核对清单（脚本不判分，需人工执行/确认）

- **M01** A02 写入后，MySQL 中该 request_id 应只有 1 条审计记录
    - SELECT COUNT(*) FROM audit_log WHERE request_id='R1-A02';  -- 期望 1
- **M02** A03 同键重试后，计数仍应为 1（幂等未产生新行）
    - SELECT COUNT(*) FROM audit_log WHERE request_id='R1-A02';  -- 期望仍为 1
- **M03** 数据库停机场景：审计降级/失败路径
    - 需服务端配合注入故障；未提供故障开关时标注「不具备条件」
- **M04** 通知失败但审计成功的两状态分离展示
    - 需真实通知组件；未提供时标注「不具备条件」


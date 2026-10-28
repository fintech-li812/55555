# MCP 验收测试报告 · CashflowRiskWorkbench

- 目标入口：`http://127.0.0.1:8202/mcp`（来源：命令行参数或环境变量 MCP_BASE_URL）
- run-tag：`LAX1`（request_id 前缀，用于复现）
- 生成时间：2026-10-21T23:51:54+08:00（本地 +08:00；服务端 server_time 为 UTC）
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

## A02 写一笔演示告警 — PASS

- **请求摘要（脱敏后）**：tools/call log_to_mysql；request_id=LAX1-A02；risk_level=yellow
- **响应关键片段**：HTTP 200；audit_id=A-d077231f；deduplicated=false；server_time=2026-10-21T15:00:00Z
- **判定**：PASS
- **理由**：ok=true，返回 audit_id，deduplicated=false

## A03 同键同内容重试 — PASS

- **请求摘要（脱敏后）**：tools/call log_to_mysql；request_id=LAX1-A02（与 A02 相同）
- **响应关键片段**：HTTP 200；audit_id=A-d077231f；deduplicated=true
- **判定**：PASS
- **理由**：ok=true，deduplicated=true，audit_id 与首次相同

## A04 同键不同内容 — PASS

- **请求摘要（脱敏后）**：tools/call log_to_mysql；request_id=LAX1-A02（与 A02 相同）；risk_level=red
- **响应关键片段**：HTTP 200；{"ok": false, "code": "IDEMPOTENCY_CONFLICT", "message": "同幂等键不同内容", "request_id": "LAX1-A02", "server_time": "2026-10-21T15:00:00Z", "data": {}}
- **判定**：PASS
- **理由**：ok=false 且 code=IDEMPOTENCY_CONFLICT，冲突被正确识别

## A05 非法入参被拒 — PASS

- **请求摘要（脱敏后）**：tools/call log_to_mysql × 3（超长 id / 无时区时间 / 数字金额）
- **响应关键片段**：    - ① 超长 request_id（200 字符）：HTTP 200；rejected=True；code=INVALID_PARAMS；{"ok": false, "code": "INVALID_PARAMS", "message": "request_id 长度超限（200，允许 1~128）", "request_id": "LAX1-A05-LONGXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX…(截断, 共352字符)  
    - ② occurred_at 不带时区（2026-10-21T09:00:00）：HTTP 200；rejected=True；code=INVALID_PARAMS；{"ok": false, "code": "INVALID_PARAMS", "message": "occurred_at 必须为带时区的 RFC3339", "request_id": "LAX1-A05-2", "server_time": "2026-10-21T15:00:00Z", "data": {}}  
    - ③ payload 金额传数字（1000000 而非 "1000000.00"）：HTTP 200；rejected=True；code=INVALID_PARAMS；{"ok": false, "code": "INVALID_PARAMS", "message": "payload.amount 金额必须为十进制字符串，收到 int", "request_id": "LAX1-A05-3", "server_time": "2026-10-21T15:00:00Z", "data": {}}
- **判定**：PASS
- **理由**：三次非法入参全部被拒，未入库成功

## A06 伪造租户字段 — PASS

- **请求摘要（脱敏后）**：tools/call log_to_mysql；额外注入 tenant_id=evil-corp
- **响应关键片段**：HTTP 200；{"ok": true, "code": "OK", "message": "审计事件已存储", "request_id": "LAX1-A06", "server_time": "2026-10-21T15:00:00Z", "data": {"audit_id": "A-2abb6e28", "deduplicated": false}}
- **判定**：PASS
- **理由**：模型传入的 tenant_id 未被采信；服务端解析的租户值=(返回数据未含租户字段)

## A07 精确规则查询 — PASS

- **请求摘要（脱敏后）**：query_rule_stats ×2（demo_rule → demo_rule_2）；窗口[2026-10-21T22:51:54+08:00, 2026-10-22T00:51:54+08:00)
- **响应关键片段**：① HTTP 200；{"ok": true, "code": "OK", "message": "查询成功", "request_id": "-", "server_time": "2026-10-21T15:00:00Z", "data": {"found": true, "counts": [{"rule_id": "demo_rule", "rule_version": "1.0", "risk_level":…(截断, 共480字符)  
② HTTP 200；{"ok": true, "code": "OK", "message": "无匹配记录", "request_id": "-", "server_time": "2026-10-21T15:00:00Z", "data": {"found": false, "counts": []}}
- **判定**：PASS
- **理由**：精确查询仅含 demo_rule/1.0；查询 demo_rule_2 返回空（found=False）

## A08 模板不存在 — PASS

- **请求摘要（脱敏后）**：tools/call load_param_template；industry_key=nonexistent_industry
- **响应关键片段**：HTTP 200；{"ok": true, "code": "OK", "message": "未找到模板", "request_id": "-", "server_time": "2026-10-21T15:00:00Z", "data": {"found": false, "matched_version": null}}
- **判定**：PASS
- **理由**：found=false，且未返回其他行业模板

## A09 无凭据到账验证 — PASS

- **请求摘要（脱敏后）**：tools/call verify_payment；method=bank_api；证据引用为占位引用（无真实凭据）
- **响应关键片段**：HTTP 200；{"ok": false, "code": "IDEMPOTENCY_CONFLICT", "message": "同幂等键不同内容", "request_id": "smoke-test", "server_time": "2026-10-21T15:00:00Z", "data": {}}
- **判定**：PASS
- **理由**：以明确错误码拒绝：code=IDEMPOTENCY_CONFLICT（如 BANK_ADAPTER_UNAVAILABLE 语义）

## A10 模型自证已批准 — PASS

- **请求摘要（脱敏后）**：tools/call log_exception；必填字段齐全，approval_ref=APPROVAL-FAKE-0001（不可核实）
- **响应关键片段**：HTTP 200；{"ok": false, "code": "APPROVAL_UNVERIFIED", "message": "审批凭据无法核实，不记为已批准例外", "request_id": "LAX1-A10", "server_time": "2026-10-21T15:00:00Z", "data": {}}
- **判定**：PASS
- **理由**：服务端拒绝自证批准，code=APPROVAL_UNVERIFIED

## A11 空反馈统计 — PASS

- **请求摘要（脱敏后）**：tools/call query_feedback_stats；窗口[2026-10-20T23:51:54+08:00, 2026-10-21T23:51:54+08:00)
- **响应关键片段**：HTTP 200；{"ok": true, "code": "OK", "message": "无反馈样本", "request_id": "-", "server_time": "2026-10-21T15:00:00Z", "data": {"found": false, "label_counts": {"accurate": 0, "inaccurate": 0, "pending": 0}}}
- **判定**：PASS
- **理由**：未出现任何「准确率」字段；计数字段=(空统计/零计数)

## A12 通知语义探测 — INFO

- **请求摘要（脱敏后）**：GET http://127.0.0.1:8202/mcp（仅一次探测，不维持长连接）
- **响应关键片段**：HTTP 405；Content-Type=-；响应体=
- **判定**：INFO
- **理由**：GET 探测返回 HTTP 405；200/405 均为 INFO，不判 FAIL（⚠ 协议未明确 GET 语义）

---

## 汇总

| 用例 | 标题 | 判定 |
| --- | --- | --- |
| A01 | 初始化与工具发现 | PASS |
| A02 | 写一笔演示告警 | PASS |
| A03 | 同键同内容重试 | PASS |
| A04 | 同键不同内容 | PASS |
| A05 | 非法入参被拒 | PASS |
| A06 | 伪造租户字段 | PASS |
| A07 | 精确规则查询 | PASS |
| A08 | 模板不存在 | PASS |
| A09 | 无凭据到账验证 | PASS |
| A10 | 模型自证已批准 | PASS |
| A11 | 空反馈统计 | PASS |
| A12 | 通知语义探测 | INFO |

PASS=11  FAIL=0  SKIP=0  INFO=1

建议退出码：**0**（有 FAIL → 1；全 PASS/SKIP → 0；连不上服务器 → 2）

---

## 人工核对清单（脚本不判分，需人工执行/确认）

- **M01** A02 写入后，MySQL 中该 request_id 应只有 1 条审计记录
    - SELECT COUNT(*) FROM audit_log WHERE request_id='LAX1-A02';  -- 期望 1
- **M02** A03 同键重试后，计数仍应为 1（幂等未产生新行）
    - SELECT COUNT(*) FROM audit_log WHERE request_id='LAX1-A02';  -- 期望仍为 1
- **M03** 数据库停机场景：审计降级/失败路径
    - 需服务端配合注入故障；未提供故障开关时标注「不具备条件」
- **M04** 通知失败但审计成功的两状态分离展示
    - 需真实通知组件；未提供时标注「不具备条件」


# 外包任务书 02 · MCP 验收测试客户端（mcp_smoke_test.py）

> 发包方：CashflowRiskWorkbench 项目
> 交付形式：一个 Python 文件（单个代码块）+ 30 行以内的使用说明
> 验收方式：发包方会先做静态审查（禁用库、硬编码、密钥），再在真实 MCP Server 上跑通后付款

---

## 一、背景（读完就够，不要追问）

我们有一个 MCP Server（Streamable HTTP 传输），对外提供 14 个财务风控工具。按对接协议，接入千问办公之前必须先用独立测试客户端完成 15 项验收。你的任务就是写这个客户端：**一个零依赖的命令行脚本，对指定服务器跑验收用例，输出结构化报告**。

你不写服务端、不碰数据库、不改任何业务逻辑。你只做"打协议、验行为、出报告"。

## 二、目标服务器的协议规格（照此实现，不要发挥）

- 入口：`{BASE_URL}`，形如 `http://127.0.0.1:8000/mcp`，由命令行参数或环境变量 `MCP_BASE_URL` 提供。
- 传输：JSON-RPC 2.0 over HTTP POST。请求头必须带 `Content-Type: application/json` 和 `Accept: application/json, text/event-stream`。
- 初始化握手：`initialize` → 响应含 `protocolVersion`（回显或协商）、`serverInfo`、`capabilities.tools` → 随后发送通知 `notifications/initialized`（通知无 id、无响应体，服务端应回 HTTP 202 或 2xx）→ 之后所有请求带 `MCP-Protocol-Version` 头（取 initialize 响应里的版本）。若 initialize 响应头出现 `Mcp-Session-Id`，后续请求必须原样回传该头。
- **响应可能是纯 JSON，也可能是 SSE 帧格式（`Content-Type: text/event-stream`，消息体为若干 `data: {...}` 行）**。你必须两种都能解析：SSE 时取最后一条 `data:` 的 JSON 作为 JSON-RPC 响应。
- `GET {BASE_URL}` 可能返回 405 或 SSE 流——你的脚本只对 GET 做一次探测并记录状态码，不要求维持长连接。
- 鉴权：若环境变量 `MCP_TOKEN` 非空，所有请求带 `Authorization: Bearer {token}`。若服务器返回 401/403，报告相应用例为 FAIL 并给出结论"鉴权生效"（这在"认证拒绝"用例里是预期行为）。
- 工具调用：`tools/call`，params 为 `{"name": "...", "arguments": {...}}`。正常结果在 `result.content[0].text`（JSON 字符串）与 `result.structuredContent`（同业务对象）；业务对象统一含 `ok / code / message / request_id / server_time / data`。工具执行失败时 `result.isError = true`，业务对象里 `ok = false`，`code` 为错误码（如 `IDEMPOTENCY_CONFLICT`）。参数不合法可能直接返回 JSON-RPC 错误 `-32602`。
- 14 个工具名（固定）：`log_to_mysql`、`query_rule_stats`、`query_audit_by_event`、`log_feedback`、`query_feedback_stats`、`save_param_template`、`load_param_template`、`log_exception`、`query_exception_stats`、`log_stress_test`、`verify_payment`、`rf_predict`、`load_da_coefficients`、`save_da_coefficients`。
- **部署是渐进的**：第一阶段可能只实装 `log_to_mysql` 和 `query_rule_stats`。你的脚本对"工具未在 tools/list 中出现"的用例必须记为 SKIP（标注"工具未实装"），不许当 FAIL，也不许当 PASS。
- 服务端时区处理：你提交的时间一律 RFC3339 带时区（用 `+08:00`）；服务端返回 `server_time` 为 UTC。

## 三、要实现的验收用例（编号与通过标准固定）

### 自动化用例（脚本直接判）

| # | 用例 | 做法 | 通过标准 |
|---|---|---|---|
| A01 | 初始化与工具发现 | 握手 → `tools/list` | `protocolVersion` 非空；`tools` 数组非空；每个工具有 `name` 与 `inputSchema`；统计"应有 14 / 实有 N" |
| A02 | 写一笔演示告警 | `log_to_mysql`，`event_type="risk_warning"`，`rule_id="demo_rule"`，`rule_version="1.0"`，`risk_level="yellow"`，payload 含 `M/C/K_yellow/K_red/currency="CNY"` 等演示字段（金额用字符串如 `"1000000.00"`） | `ok=true`，返回 `audit_id`，`deduplicated=false` |
| A03 | 同键同内容重试 | 完全相同的 request_id 与参数再发一次 | `ok=true`，`deduplicated=true`，`audit_id` 与首次相同 |
| A04 | 同键不同内容 | 相同 request_id，把 `risk_level` 改成 `"red"` 再发 | 必须冲突：`ok=false` 且 `code="IDEMPOTENCY_CONFLICT"`（或 JSON-RPC 错误）；**不得**返回成功 |
| A05 | 非法入参被拒 | 三连发：①超长 request_id（200 字符）②`occurred_at` 不带时区（`"2026-10-21T09:00:00"`）③ payload 内金额传数字 `1000000` 而非字符串 | 三次都必须被拒（`-32602` 或 `ok=false`），**不得入库成功** |
| A06 | 伪造租户字段 | `log_to_mysql` 的 arguments 里额外塞一个 `"tenant_id": "evil-corp"` | 服务端要么拒收，要么入库租户仍是服务端解析值（以返回数据为准）；报告中明确"模型传入的 tenant_id 未被采信"才 PASS；若返回数据里出现 evil-corp 即 FAIL |
| A07 | 精确规则查询 | 先 A02 写入，再 `query_rule_stats` 查 `rule_id="demo_rule"&rule_version="1.0"`，窗口 `[now-1h, now+1h)` | 统计里只含精确匹配的记录；再查 `rule_id="demo_rule_2"` 应返回空/`found=false`，不得混入 |
| A08 | 模板不存在 | `load_param_template`，`industry_key="nonexistent_industry"` | `found=false`；**不得**返回其他行业的模板 |
| A09 | 无凭据到账验证 | `verify_payment`，`method="bank_api"`，证据引用随便填 | `verification_status="pending"`（或明确错误码如 `BANK_ADAPTER_UNAVAILABLE`）；`verified_amount="0.00"`；**绝不允许 `"verified"` |
| A10 | 模型自证已批准 | `log_exception`，必填字段齐全但 `approval_ref` 填一个格式合法但不可核实的引用（如 `"APPROVAL-FAKE-0001"`） | 要么 `APPROVAL_UNVERIFIED` 拒绝，要么 `pending`——不得记为已批准例外（返回数据不得含 `approval_status="verified"` 之类表述） |
| A11 | 空反馈统计 | `query_feedback_stats`，窗口 `[now-24h, now)` | 返回空统计或零计数；**不得**出现任何"准确率"字段 |
| A12 | 通知语义探测 | 对 `GET {BASE_URL}` 发一次探测 | 记录状态码（200/405 均记录为 INFO，不判 FAIL；连接失败才 FAIL） |

### 人工核对用例（脚本只打印核对清单，不判分）

- M01：A02 之后，人工在 MySQL 执行 `SELECT COUNT(*) FROM audit_log WHERE request_id='...'` 应为 1（脚本在报告里给出这条现成 SQL，代入实际 request_id）。
- M02：A03 之后同样查询仍为 1。
- M03：数据库停机场景（需服务端配合注入故障，未提供故障开关时标注"不具备条件"）。
- M04：通知失败但审计成功的两状态分离展示（需真实通知组件）。

## 四、脚本硬性要求

1. **只用 Python 标准库**：`urllib.request / json / sys / os / time / datetime / hashlib / argparse / uuid`。禁止 requests、httpx、pytest、rich 等任何第三方库。
2. Python ≥ 3.10，单文件，入口 `if __name__ == "__main__":`，无类继承怪癖、无全局可变状态。
3. 命令行参数：`--base-url`（默认取环境变量 `MCP_BASE_URL`）、`--token`（默认取 `MCP_TOKEN`，命令行里传了也不能回显到报告）、`--run-tag`（默认 `YYYYMMDD-HHMMSS`，用于生成可复现的 request_id 前缀）。
4. **request_id 生成规则**：`f"{run_tag}-A{用例编号}"`，保证 A03 重试时与 A02 用的是同一个 id——这是幂等用例成立的前提，务必写对。
5. **重试纪律（项目铁律）**：任何未知网络错误，重试间隔 ≥ 30 秒、同一问题最多 3 次，先打印"有信息量的只读排查"（比如先 GET 探测一次再重试）；超限即判 FAIL 并停止后续依赖该步的用例，**不得密集轰炸服务器**。HTTP 4xx/5xx 明确响应不算"未知错误"，不重试。
6. 所有用例**串行**执行，一个失败不中断整体（除非后续用例依赖它的产物，如 A07 依赖 A02）。
7. 报告输出到 stdout，格式为 Markdown：逐用例一节，含"请求摘要（脱敏后）/ 响应关键片段 / 判定 PASS/FAIL/SKIP/INFO / 一行理由"；结尾一张汇总表（PASS/FAIL/SKIP/INFO 计数 + 总 exit 建议）。
8. `data` 里的金额、`server_time`、`audit_id` 原样引用；**报告中不得出现 token 值、完整 HTTP 头、完整 payload 转储超过 200 字**。
9. 退出码：有 FAIL → 1；全 PASS/SKIP → 0；连不上服务器 → 2。
10. 代码顶部写 60 行以内的模块 docstring：用途、用法两行示例、退出码说明、依赖说明。

## 五、交付格式

直接输出：
1. 一个完整 Python 代码块（文件名 `mcp_smoke_test.py`）；
2. 之后跟"使用说明"小节：安装（无需）、两条运行示例、退出码含义、与人工核对 M01–M04 的配合方式。

不要前言、不要总结感想、不要问我环境细节——服务器还没跑起来是常态，你的脚本本来就要能对不存在的服务器返回 exit 2。

## 六、红线（违反任一条直接打回）

1. 禁止第三方依赖；禁止 `pip install` 指令出现在交付物里。
2. 禁止硬编码任何 URL、token、服务器路径——全部走参数/环境变量。
3. 禁止在报告或日志里输出 Authorization 头的值。
4. 禁止实现服务端逻辑、禁止 mock 服务器来"假装通过"（自测可以临时起个 http.server，但交付物不得包含 mock 代码）。
5. 禁止修改第三节任何用例的通过标准。
6. 不确定协议行为时（比如 202 还是 204），**放宽为 INFO 记录实际值**，写进代码注释标 `⚠ 协议未明确`，不要猜死。

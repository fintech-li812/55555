# AI 财务风险管控工作台：千问办公 × MySQL MCP 对接协议

> 文档版本：1.0（待联调）  
> 日期：2026-10-21  
> 交付格式：Markdown 协议文档 + MCP tools/list 工具定义 JSON  
> 状态：接口设计稿，尚未部署、连接千问或执行数据库迁移。

## 一、项目依据与版本说明

本协议依据已读取的《AI财务风险管控工作台完整方案_v7.md》《行业参数模板_相对指标版_v8.md》以及主项目《项目进度.md》编写。当前未找到《AI财务风险管控工作台完整方案_v8.md》全文，因此本文件不是对该全文逐条核对后的定稿。

已对齐的业务方向：核心计算、阈值判定、参数修正和压力测试在千问工作台完成；MCP 提供存储、查询及证据接口；MySQL 保存审计、反馈、行业模板、例外审批、压力测试和到账验证记录。

主项目进度记载本机已有 MySQL 8.4、PyMySQL 和 tools/mysql_conn.py 连接助手。这些属于已记录的环境事实，不能据此推定本工作台的六张业务表已创建或千问连接器已联通。原 Jev → tools → DeepSeek 分工保持独立；本协议不要求替换主项目路由。

v8 相对指标口径：记录月均经营性流出 M、现金缓冲 C、黄灯系数 K黄、红灯系数 K红以及公式版本；禁止把旧版固定金额阈值写死进 MCP。金额使用元，不能把“万元”直接作为“元”入库。模板来源中的经验参数与经核验的公开基准必须分开标记。

## 二、系统边界

```text
千问表格完成计算/判定
    ↓ Agent 或定时任务发起工具调用
MCP 客户端 → HTTPS /mcp → MCP Server → MySQL
    ↑ 工具返回存储凭据/查询结果
千问更新审计状态；通知通过已有连接器另行执行
```

MCP 不重新计算红黄绿、不自动批准例外、不代替银行确认到账、不直接付款。MySQL 的 COUNT、SUM 等留痕统计可在服务端执行，但不得把这些统计解释为新的财务风险决策。

表格公式不能直接写 `=log_to_mysql()`。必须由 Agent 或定时任务读取结果后调用工具。千问是否支持自定义 MCP、传输方式、认证方式、定时执行与结果写回，均需现场验证。

## 三、传输与初始化

### 3.1 标准协议

- 消息编码：UTF-8 JSON-RPC 2.0。
- 远程接入首选 Streamable HTTP，统一入口为 `https://<实际域名>/mcp`。
- 协议版本由初始化协商；本文件以 `2025-06-18` 为示例基线，不宣称它是最新版，也不代表千问已支持。
- 服务端须实现 `initialize`、`notifications/initialized`、`tools/list`、`tools/call`，并支持 `ping`。
- 推荐使用 MCP SDK 承担协议层。普通 FastAPI REST 路由可以保留为内部接口，但仅实现 `/mcp/log_to_mysql` 等路径不构成 MCP Server。

初始化请求示例：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"finance-workbench-client","version":"1.0.0"}}}
```

初始化响应示例：

```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"finance-risk-mysql","version":"1.0.0"}}}
```

客户端确认：

```json
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

随后请求 `tools/list`，返回附件 JSON 的 `tools` 数组。附件是工具定义，不是可直接导入千问的连接器配置，也不是可运行服务。

### 3.2 HTTP 约定

POST 请求携带 `Content-Type: application/json` 和 `Accept: application/json, text/event-stream`。初始化后按协商携带 `MCP-Protocol-Version`；如服务端签发 `Mcp-Session-Id`，后续请求必须携带。鉴权在初始化及后续调用中均生效。

通知被接受时返回 HTTP 202、无响应体。GET `/mcp` 提供 SSE 或返回 405；两者按服务器能力实现。网关保留必要请求头，若使用 SSE 则关闭对应响应缓冲。

### 3.3 身份与权限

远程接入采用客户端与网关实际支持、符合所选 MCP 授权规范的认证方案；完整 OAuth 对接与千问支持情况需联调确认。受控演示环境如采用预发令牌，必须标注为受限接入方案，不宣称已完成标准 OAuth 授权。

企业 ID、操作者 ID 和角色从已认证主体解析，不能信任模型传入的同名字段。单企业演示可绑定固定企业配置。不同企业必须使用不同授权范围，所有 SQL 均带服务端解析的企业条件。校验 Origin；本地服务只监听回环地址；MySQL 不对公网暴露。

## 四、通用数据契约

| 字段 | 类型 | 规则 |
|---|---|---|
| request_id | string | 写请求必填，1～128 字符；同一业务事件重试保持不变 |
| source_ref | string | 写请求必填；可定位表格、行、任务或报告的引用 |
| occurred_at | string | RFC3339，必须带时区；数据库统一转 UTC |
| rule_id / rule_version | string | 风控事件必填，精确标识规则与版本 |
| currency | string | 本阶段固定 CNY；其他币种需扩展协议 |
| amount 等金额 | string | 十进制字符串，例如 "125000.00"；最多两位小数，服务端 Decimal 解析 |
| risk_level | enum | green / yellow / red |
| tracking_status | enum | resolved / observing / deteriorated；不能由风险颜色直接推导 |
| verification_status | enum | pending / verified / rejected |
| template_version | string | 模板版本，不允许用“最新版”替代历史版本号 |
| created_at | string | 由服务端写入的 UTC 时间，不使用模型自报时间 |

服务器拒绝超限字符串、NaN、Infinity、不合法金额及无时区时间；单请求上限建议 256 KB，payload 上限建议 64 KB。以上是项目限额设计，不是 MCP 标准强制限额。

查询区间统一为 `[start_at, end_at)`，最大 366 天；分页默认 50 条、最多 200 条。游标必须绑定企业、查询条件与排序，不允许跨企业复用。查询无数据返回空数组或 found=false，不伪造零风险结论。

### 4.1 幂等和事务

以 `(tenant_id, tool_name, request_id)` 为唯一键。对语义参数做规范化 JSON 后计算 SHA-256；同键同参数返回原结果，标记 deduplicated=true；同键不同参数返回 IDEMPOTENCY_CONFLICT。

业务写入和幂等凭据在同一事务提交。连接中断后不得假定写入失败，先以相同 request_id 查询或重试。并发重复由唯一约束兜底，不采用“先查再插”作为唯一防护。

## 五、工具定义与业务规则

工具的精确输入 JSON Schema 见附件 `千问办公_MySQL_MCP工具定义.json`。所有工具禁止接收任意 SQL、数据库名或表名。

| 工具名 | 用途 | 权限 | 目标表 |
|---|---|---|---|
| log_to_mysql | 追加工作台审计事件 | audit:write | audit_log |
| query_rule_stats | 按规则/版本/风险等级统计事件 | audit:read | audit_log |
| log_feedback | 保存预测与实际反馈 | feedback:write | feedback_log |
| query_feedback_stats | 按准确性标签汇总反馈 | feedback:read | feedback_log |
| save_param_template | 保存行业模板新版本草稿 | template:write | param_template |
| load_param_template | 读取指定版本或已发布最新模板 | template:read | param_template |
| log_exception | 留存已有审批凭据的例外事件 | exception:write | exception_log |
| query_exception_stats | 查询例外笔数和金额 | exception:read | exception_log |
| log_stress_test | 保存千问已计算的压力测试结果 | stress:write | stress_test_log |
| verify_payment | 提交证据并查询可信到账验证结果 | payment:verify | payment_verify_log |

### 5.1 审计 log_to_mysql

`event_type` 仅允许 risk_warning、tracking_changed、parameter_changed、task_result。payload 保存结果快照，不接受密码、完整银行卡号或整份银行流水。风控告警快照包含 M、C、K黄、K红、动态阈值开关、样本月份数、公式版本以及来源引用。服务器校验格式，不重新进行业务判定。

M 为零或无完整月份时，工作台必须标注不可判定或口径异常，不能把空值补成零后自动报绿。C 的来源必须说明是否已包含该笔到账，避免“现金余额 + 已含在余额中的到账”重复计入。

### 5.2 规则统计 query_rule_stats

按 rule_id、rule_version、risk_level 精确字段查询，不能使用 payload LIKE 模糊匹配。返回 counts、窗口起止、总事件数、截点时间。这里统计的是预警事件，不等于正确率；正确率需要有对应反馈。

### 5.3 反馈 log_feedback / query_feedback_stats

反馈必须引用已有 audit_id 或 forecast_id，并带 metric_code、预测区间、actual_source_ref。预测值与实际值只按同币种同单位存储；误差、准确性标签由工作台按指定评估口径生成，附 evaluation_version。缺乏实际数据时不得生成“准确”标签。

查询默认返回各标签数量，不擅自计算通用“准确率”；只有分母、成熟观察期和缺失处理均明确时，工作台才能计算评价指标。

### 5.4 模板 save_param_template / load_param_template

模板保存后默认 draft；发布动作通过可信管理流程执行，本阶段不向模型暴露发布权限。相同企业、行业、版本号不得覆盖；历史事件始终引用当时版本。

params 至少声明 schema_version、base_metric、yellow_coefficient、red_coefficient、approval_ratios、provenance、effective_from。要求 base_metric=monthly_operating_outflow；系数使用有限十进制字符串并满足黄灯系数大于红灯系数且红灯系数非负。provenance 区分经验建议、企业批准和已核验外部来源，外部出处未经核验不得标记 verified。

读取必须返回 matched_version、publication_status、来源与有效期。未找到模板返回 found=false，由千问进入人工配置；禁止默默套用其他行业模板。跨企业共享仅能使用另行批准的脱敏公共模板库，不直接读取其他企业参数。

### 5.5 例外 log_exception / query_exception_stats

必填部门、金额、理由和 approval_ref。服务器从可信审批系统或已登记审批凭据中核实批准状态、审批人和授权范围，不能把模型提交的 approver 名字当作批准证明。凭据无法核实时返回 APPROVAL_UNVERIFIED，不记录为已批准例外。

本工具只留痕，不执行付款、放行或审批。查询返回例外笔数、例外总额；例外率须另有同范围审批总笔数作为分母，缺少分母返回 null 并说明。原方案 trigger_audit_check 涉及任务系统写入，待真实任务连接器和权限明确后单独接入；本阶段不虚构已创建审计任务。

### 5.6 压力测试 log_stress_test

scenario 为 receivable_shock、cost_shock、dual_shock、combined_shock。保存 scenario_version、input_snapshot、result_snapshot 和风险等级，不在 MCP 内跑压力模型。每个场景具有独立 request_id，均带同一个 run_id 供整批回溯。只完成部分场景时不能声明整批测试成功。

### 5.7 到账 verify_payment

输入 payment_id、method、evidence_ref。method 为 bank_api 或 manual_review；企业、金额、收款账户和流水日期必须从可信来源核对，不能由模型提交 status=verified。

银行连接器未接入时返回 pending、reason=BANK_ADAPTER_UNAVAILABLE。人工复核须有已认证复核人的记录；仅上传截图仍为 pending。部分到账、重复流水、币种不同、金额不符或证据不足均不得直接 verified。凭据可以存对象存储引用及摘要，不向模型返回完整账户信息。

返回 verification_status、verified_amount、currency、evidence_ref、verified_at、review_ref；未通过时 verified_amount 为 "0.00"。只有工作台按其计算口径确认验证通过且尚未计入余额，才能更新有效缓冲。

## 六、调用与返回示例

下列均为虚构演示数据。

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "tools/call",
  "params": {
    "name": "log_to_mysql",
    "arguments": {
      "request_id": "demo-20261021-warning-001",
      "source_ref": "qwen-sheet://demo/risk/row-18",
      "occurred_at": "2026-10-21T09:00:00+08:00",
      "event_type": "risk_warning",
      "rule_id": "cash_buffer_relative",
      "rule_version": "8.0",
      "risk_level": "yellow",
      "payload": {
        "currency": "CNY",
        "M": "1000000.00",
        "C": "1200000.00",
        "K_yellow": "1.5",
        "K_red": "1.0",
        "history_months": 8,
        "dynamic_threshold_enabled": false,
        "formula_version": "cash-buffer-v8-demo"
      }
    }
  }
}
```

业务结果对象统一包含 ok、code、message、request_id、server_time、data。查询 request_id 由服务端产生；写请求回显业务 request_id。工具成功返回：

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": {
    "content": [{"type":"text","text":"{\"ok\":true,\"code\":\"OK\",\"message\":\"审计事件已存储\",\"request_id\":\"demo-20261021-warning-001\",\"server_time\":\"2026-10-21T01:00:01Z\",\"data\":{\"audit_id\":\"A-demo-001\",\"deduplicated\":false}}"}],
    "structuredContent": {
      "ok": true,
      "code": "OK",
      "message": "审计事件已存储",
      "request_id": "demo-20261021-warning-001",
      "server_time": "2026-10-21T01:00:01Z",
      "data": {"audit_id":"A-demo-001","deduplicated":false}
    },
    "isError": false
  }
}
```

兼容文本消费者时 content.text 为相同业务对象的 JSON 字符串。服务端仅在数据库事务提交成功后报告已存储；这不代表通知已发送、风险已解除或审批已完成。

## 七、错误、重试和本地兜底

| 情形 | 表达方式 | 调用方处理 |
|---|---|---|
| HTTP 身份认证失败 | HTTP 401/403 | 修复凭据或权限，不盲目重试 |
| JSON-RPC 报文无效/方法未知 | 标准 -32600/-32601 等错误 | 修正协议实现 |
| 工具名未知或参数不合法 | JSON-RPC -32602 | 修正名称或输入 |
| 企业范围不符 | 工具错误 FORBIDDEN | 停止调用，核对授权 |
| 同幂等键不同内容 | 工具错误 IDEMPOTENCY_CONFLICT | 人工核对原记录，不覆盖 |
| 审批未核实 | 工具错误 APPROVAL_UNVERIFIED | 补充可信凭据 |
| 数据库连接/提交失败 | 工具错误 DB_UNAVAILABLE | 原键排队补传 |
| 缺少银行/人工证据 | 正常业务结果 pending | 保持待验证，不计入有效到账 |

业务执行失败使用 result.isError=true，content 与 structuredContent 返回同一错误对象。错误信息必须脱敏，不返回密码、连接串或完整 SQL。结构化 ok=false 与 isError=true 保持一致；pending 属于已正常执行但证据未满足，不属于工具执行异常。

遵守用户项目规则：未知问题每次重试至少间隔 30 秒，本方案最多 3 次；每次先做有信息量的只读排查。超过次数停止，报告错误原文（剔除密钥）与已尝试方法。写入重试沿用原 request_id。

MCP 不可用时，千问本地审计表记录 pending_sync、request_id、工具名、待提交参数和失败原因；这不等于 MySQL 已写入。恢复后按时间串行补传，收到提交成功凭据才改为 synced。未落地可靠队列前不得宣称具有自动断网补传能力。

## 八、MySQL 落库约束

六张业务表沿用原方案名称，新增协议约束；以下为迁移设计，不是已执行 SQL。

| 表 | 核心业务字段 |
|---|---|
| audit_log | event_type、rule_id、rule_version、risk_level、payload(JSON) |
| feedback_log | audit_id/forecast_id、metric_code、predicted_value、actual_value、accuracy_label、evaluation_version、actual_source_ref |
| param_template | industry_key、template_version、publication_status、params(JSON)、provenance(JSON)、effective_from |
| exception_log | dept、amount、currency、reason、approval_ref、trusted_approver_id |
| stress_test_log | run_id、scenario、scenario_version、input_snapshot(JSON)、result_snapshot(JSON)、risk_level |
| payment_verify_log | payment_id、method、verification_status、verified_amount、currency、evidence_ref、review_ref |

业务表共同字段：id、tenant_id、request_id、source_ref、occurred_at、created_at、actor_id。金额使用 DECIMAL(20,2)，UTC 时间使用 DATETIME(6)，结构化内容用 JSON，字符集 utf8mb4，存储引擎 InnoDB。

另建 mcp_request_ledger：tenant_id、tool_name、request_id、payload_hash、result_json、created_at，唯一键为 (tenant_id, tool_name, request_id)，与业务写入使用同一事务。

索引至少包含：audit_log(tenant_id, rule_id, created_at)、exception_log(tenant_id, dept, created_at)、payment_verify_log(tenant_id, payment_id, created_at)。模板唯一键为 (tenant_id, industry_key, template_version)。审批事件通过业务审批唯一键防止换 request_id 重复记账；银行流水通过可信流水唯一键防止重复核销。

业务账号仅赋必要 SELECT/INSERT，不能 DROP、ALTER、DELETE；建表迁移和模板发布使用独立管理身份。追加记录纠错通过 supersedes_id 指向原记录，查询统计排除被替代版本，避免重复计数。数据库备份使用独立备份账号，不复用受限业务账号。

SQL 值使用驱动参数绑定；聚合维度与排序字段来自代码白名单。不得把模型给出的列名直接拼接 SQL。数据库口令只来自环境变量或密钥管理，不写入文档、客户端配置或日志。

## 九、千问接入与联调顺序

1. 在千问当前版本查看是否有自定义 MCP 配置入口，记录实际支持的传输、认证和协议版本；不要把 v7 的菜单路径当作已验证事实。
2. 先用 MCP 测试客户端验证初始化、工具发现、工具调用及认证拒绝，再接千问。数据库采用独立演示库。
3. 若千问支持 Streamable HTTP，配置实际 HTTPS `/mcp` 地址及匹配认证。若仅支持其他方式，调整 SDK 传输配置并复测；不要用改 URL 的方式假装兼容。
4. 首次只开放 log_to_mysql 和 query_rule_stats，使用标有 demo 的虚构数据完成写入、查询、重复提交三步。
5. 再开放反馈、模板和压力测试工具；最后接入审批凭据及银行/人工复核证据。
6. 在千问中核对“待同步/已留痕/待验证”等状态。每次业务调用确认数据库记录与工具凭据一致。

连接信息填写清单：服务名称 finance-risk-mysql；地址 https://<实际域名>/mcp；传输 Streamable HTTP（须实测）；认证以实际支持方案为准。此清单不等于特定千问版本可直接导入的配置 JSON。

## 十、Agent 调用约束（可放入工作台提示词）

> 使用工作台计算结果调用 MCP，禁止自行编造财务金额、风险等级、审批记录或到账验证结果。写请求均提供稳定 request_id、来源和时间。收到数据库提交成功凭据后才称“已留痕”；pending_sync 应明确显示待同步。verify_payment 返回 pending 或 rejected 时，保持待验证/退回状态，不把金额计入有效缓冲。模板保存成功仅表示草稿已保存，不能声称已发布。例外留痕不等于付款放行。工具查询无数据时如实说明。失败按项目节奏串行排查和重试，不密集重复调用。

## 十一、验收清单

| 编号 | 场景 | 通过标准 |
|---|---|---|
| 01 | initialize / initialized / tools/list | 成功协商并发现附件中 10 个工具 |
| 02 | 写一笔演示告警 | 数据库仅新增一条，返回可查询 audit_id |
| 03 | 同键同内容重试 | 不新增记录，deduplicated=true |
| 04 | 同键不同内容 | 明确冲突，原记录不变 |
| 05 | 跨企业请求 | 拒绝且不泄露目标记录 |
| 06 | 超限/非法金额/无时区时间 | 入库前拒绝 |
| 07 | 数据库断连 | 不报告成功，进入可见待同步状态 |
| 08 | 补传后查询 | 与原事件一致，不重复计数 |
| 09 | 查询精确规则和版本 | 不混入相似名称或其他版本 |
| 10 | 模板版本不存在 | found=false，不替换行业 |
| 11 | 上传凭证但无可信复核 | pending，verified_amount="0.00" |
| 12 | 模型声称已批准/已到账 | 无可信凭据不能转为批准/verified |
| 13 | 历史不足 12 个月的告警 | 记录真实月份数及静态相对阈值快照 |
| 14 | 查询无反馈样本 | 返回空统计，不生成准确率 |
| 15 | 通知失败但审计成功 | 分别显示两项状态，不混称全流程成功 |

全部验收项当前均为待执行。本协议与工具 JSON 通过文件层面检查，不构成已通过联调的证据。

## 十二、协议参考

- [MCP 工具发现、调用与返回结构（2025-06-18）](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [MCP Streamable HTTP 传输（2025-06-18）](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)

协议格式来自上述标准；业务字段、权限名、幂等策略和验收规则是本项目约定。千问客户端能力仍需实测。
# 架构设计 v10：前端 ⇄ 千问办公（中枢） ⇄ MCP 工具层

> 日期：2026-10-27　状态：**已实现并本机跑通**（千问对外 API 一项待现场验证，已做降级）
> 上位文档：`docs/v8_完整方案.md`（业务口径）｜`docs/架构设计_千问中枢_v9_20261027.md`（边界与命门）
> 本文回答三件事：**现在接到了哪一步**、**目标结构长什么样**、**怎么跑起来并验证**。

---

## 0. 结论先行：千问办公「已部分接入」，不是从零开始

### 0.1 现状盘点（实测，非推断）

| # | 能力 | 现状 | 证据/路径 |
|---|---|---|---|
| 1 | MCP 工具层（现金流 v1.2，9 工具） | ✅ 已建成并跑通 | `mcp-server/program_v1/entry.py`，2026-10-27 握手成功 |
| 2 | 千问连接器配置 | ✅ 已存在（stdio 已验证，http 未验证） | `qwen-integration/connector-stdio.json` |
| 3 | 千问 Skill 定义 | ✅ 已存在（本文已扩写 v8 段） | `qwen-integration/skills/mfg-program-workbench/SKILL.md` |
| 4 | **千问作为后端统一入口接收前端指令** | ❌ **没有** | v9 文档「命门二」：千问是否对外暴露 HTTP API 未验证 |
| 5 | **前端 ⇄ Agent 双向交互（实时状态/持续对话/参数调整）** | ❌ **完全没有** | 原前端 `web/工作台_程序计算版.html` 是单向表单 + 点击算一次 |
| 6 | v8 风控/三态/清洗/留痕 的工具化 | ❌ 之前不存在（本次补齐 12 工具） | `mcp-server/program_v1/src/v8_mcp_server.py` |
| 7 | 任务编排、步骤状态机、中间态回传 | ❌ 之前不存在（本次建成） | `qwen-integration/orchestrator/` |

### 0.2 结论

**不是"未接入"，而是"接了一半"**：工具层与 Skill 已经能和千问对话，但缺的是
**① 千问作为可被前端驱动的后端入口**、**② 双向实时交互通道**、**③ 任务编排与状态回传**。

### 0.3 现有结构 vs 目标结构

```
【现状（v9）】
用户 ─ 千问对话框 ─(MCP stdio)─ 计算引擎          ← 人只能在与千问的聊天里触发
前端网页 ─(HTTP)─ 我们自己的服务(18766/8000)      ← 与千问完全无关的两条线，并联不相交
                   ⇒ 前端和千问各跑各的，"以千问为核心"在前端这条线上不成立

【目标（v10，本次实现）】
前端 ⇄(SSE/HTTP)⇄ 编排网关 ⇄(OpenAPI)⇄ 千问办公（意图解析/编排/解释）
                    ↓(MCP JSON-RPC)
                  MCP 工具层（算钱/判定/清洗/留痕）
                    ↓
                  MySQL（自有服务器）
                   ⇒ 前端每一次交互都经过千问编排，千问不可用则本地编排兜底（链路不断）
```

### 0.4 改造方案（关键取舍，必须说清楚）

v9 的「命门二」——**千问办公是否对外暴露可供第三方网页直连的 HTTP API，尚未现场验证**。
因此本架构不把系统的可用性押在未验证的能力上，做法是：

1. **前端唯一后端入口是「编排网关」（我们自己的服务器）**，前端 ⇄ 网关是确定可用的 HTTPS + SSE；
2. **网关 ⇄ 千问是"可配置、可降级"的**：配了 `QWEN_API_BASE` + `QWEN_API_KEY` 就真调千问做意图解析与结论解释；没配或调用失败，**自动降级到本地 planner**，并在事件流里如实标注 `source=local-planner` 与降级原因，绝不假装是千问算的；
3. **一旦千问开放对外 API**：只填两个环境变量，前端即可经网关走千问编排，**代码零改动**；若千问进一步支持网页直连，则把网关退化为工具网关即可。

---

## 1. 四层架构与边界

| 层 | 组成 | 归属 | 职责 | 不许做什么 |
|---|---|---|---|---|
| **① 前端** | `web/v10_工作台.html` | 浏览器 | 交互、指令下发、实时展示状态与结果、调整参数 | 不算钱、不判定灯色 |
| **② 中枢** | 千问办公（Agent 调度 + Skill）+ 编排网关 `orchestrator/` | 千问 / 我们服务器 | 意图解析、任务编排与调度、结论解释、事件分发 | 不改写任何数字 |
| **③ 工具层** | MCP Server：`entry.py`（v1.2 现金流，9 工具）+ `v8_mcp_server.py`（v8 风控，12 工具） | 我们服务器 | 算钱、判定、清洗、留痕、回查 | 不越过数据范围闸门 |
| **④ 数据** | MySQL（`mfg_cashflow_runs_v1` + 5 张 v8 表） | 我们服务器 | 持久化与审计 | 不对公网暴露 |

**唯一允许产出金额的是 `calculate_manufacturing_cashflow`（v1.2 内核，Decimal 运算）。**

---

## 2. 层间接口定义

### 2.1 前端 ⇄ 网关（HTTP + SSE，双向）

| 方向 | 接口 | 方法 | 说明 |
|---|---|---|---|
| 上行 | `/api/chat` | POST | 下发自然语言指令 + 参数，创建任务 |
| 上行 | `/api/params` | POST | **策略实时调整**：更新参数，可选立即重跑 |
| 上行 | `/api/cancel` | POST | 取消当前任务 |
| 上行 | `/api/tool` | POST | 直接调用某个 MCP 工具（高级面板/联调） |
| 下行 | `/api/events?session_id=` | GET(SSE) | **持续事件流**：任务/步骤/中间态/结果/解释 |
| 查询 | `/api/health` | GET | 网关、MCP、千问、数据库状态 |
| 查询 | `/api/tools` | GET | MCP tools/list 原样透出 |
| 查询 | `/api/task?session_id=&task_id=` | GET | 任务快照 |
| 页面 | `/` | GET | 前端页面 |

**为什么用 SSE 而不是 WebSocket**：本场景是"服务端→前端"的单向事件推送为主，上行走普通 POST 即可；SSE 自带断线重连、穿过 Nginx 无需额外配置，实现更轻。

### 2.2 网关 ⇄ 千问办公（OpenAPI 兼容 `/chat/completions`）

- 配置：`QWEN_API_BASE`、`QWEN_API_KEY`、`QWEN_MODEL`（默认 `qwen-plus`）、`QWEN_MODE`（auto/openapi/local）
- 调用 1（意图解析）：system 提示词强制输出 **严格 JSON 计划**；网关用 `planner.validate_plan()` 校验——**工具必须在 tools/list 中存在，否则整份计划作废并降级**（防止模型幻觉出工具）
- 调用 2（结论解释）：把工具层返回的 JSON 交给千问，约束"只能引用给定数字，禁止计算推断"
- 任一失败 → 降级本地 planner / 本地模板，并在事件里给出 `fallback_reason`

### 2.3 网关 ⇄ 工具层（MCP over JSON-RPC 2.0，协议 2025-06-18）

方法：`initialize` / `notifications/initialized` / `tools/list` / `tools/call` / `ping`

三种传输，同一协议实现（同一个 `handle()`），按 `MCP_TRANSPORT` 切换：

| 传输 | 取值 | 场景 |
|---|---|---|
| inprocess | `inprocess`（默认） | 同机零开销，Windows 友好 |
| stdio | `stdio` | 与千问桌面端拉起方式一致 |
| http | `http` | 远程；`POST https://<域名>/mcp` + `Authorization: Bearer <CF_MCP_TOKEN>` |

---

## 3. 数据格式

### 3.1 上行信封（前端 → 网关）

```jsonc
// POST /api/chat
{
  "session_id": "s-xxx",            // 会话，一条 SSE 流对应一个
  "request_id": "r-xxx",            // 幂等用，可省略
  "text": "帮我跑一遍制造业风险评估，金额30万",
  "params": {"industry":"制造业","scenario":"risk","amount":300000,"n_per_group":20,"as_of":null}
}
// POST /api/params  —— 策略实时调整
{"session_id":"s-xxx","params":{"industry":"科技服务业","scenario":"risk"},"rerun":true,"text":"按新参数重新评估"}
```

参数 schema 由 `/api/health` 的 `params_schema` 下发，前端据此渲染控件；非法值由
`protocol.normalize_params()` 退回默认，**保证策略调整不会把系统带崩**。

### 3.2 下行事件（网关 → 前端，SSE，一帧一个 JSON）

```jsonc
{"type":"task.created",  "session_id":"s-xxx","task_id":"t-xxx","ts":"...","intent":"risk.assess","intent_name":"三层风控评估","agent":"local-planner","params":{...}}
{"type":"plan.ready",    "task_id":"t-xxx","steps":[{"id":"s1","tool":"risk.assess_v8","desc":"..."}]}
{"type":"step.started",  "task_id":"t-xxx","step_id":"s1","tool":"risk.assess_v8"}
{"type":"step.done",     "task_id":"t-xxx","step_id":"s1","tool":"risk.assess_v8","result":{...}}
{"type":"agent.message", "task_id":"t-xxx","text":"三层融合：Q=… / D=… / P=… → 红灯","source":"qwen-office|local-template"}
{"type":"task.done",     "task_id":"t-xxx","result":{...},"lamp":"红灯","intent":"risk.assess"}
{"type":"task.failed",   "task_id":"t-xxx","error":"..."}
{"type":"params.accepted","session_id":"s-xxx","params":{...}}
{"type":"heartbeat",     "ts":"..."}          // 每 15 秒
```

任务状态机：`created → planning → running → succeeded / failed / cancelled`

### 3.3 MCP 消息（网关 → 工具层）

```jsonc
{"jsonrpc":"2.0","id":"1","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"qwen-orchestrator","version":"v10"}}}
{"jsonrpc":"2.0","id":"2","method":"tools/list"}
{"jsonrpc":"2.0","id":"3","method":"tools/call","params":{"name":"risk.assess_v8","arguments":{"industry":"制造业","scenario":"risk","amount":300000}}}
// 返回：result.structuredContent 为业务 dict；isError=true 时由网关抛错并如实上报
```

---

## 4. 目录结构

```
CashflowRiskWorkbench/
├── web/
│   ├── v10_工作台.html                    ★ 前端（对话 + 任务时间线 + 指示灯 + 参数面板）
│   ├── v8_工作台.html                        v8 风控单页（8010）
│   └── 工作台_程序计算版.html                 v1.2 程序计算版（8000）
├── qwen-integration/
│   ├── connector-stdio.json                  千问连接器（本机，已验证）
│   ├── connector-http.json                   千问连接器（远程，待验证）
│   ├── mcp_stdio.py                          千问桌面端拉起入口（v1.2）
│   ├── skills/mfg-program-workbench/SKILL.md ★ 千问 Skill（已扩写 v8 纪律）
│   └── orchestrator/                       ★ 编排层（本次新建）
│       ├── protocol.py                       层间协议：信封/事件/参数 schema/SSE 帧
│       ├── mcp_client.py                     MCP 客户端：inprocess / stdio / http
│       ├── planner.py                        本地编排 + 千问计划校验器 + 结论模板
│       ├── qwen_adapter.py                   千问 API 适配（可降级）
│       ├── taskhub.py                        任务状态机 + 事件总线 + 执行器
│       └── gateway.py                        HTTP + SSE 网关（前端唯一入口，8100）
├── mcp-server/program_v1/src/
│   ├── entry.py / app.py                     v1.2 现金流 MCP（9 工具，唯一产金额）
│   ├── v8_mcp_server.py                    ★ v8 风控 MCP（12 工具，stdio/http）
│   └── v8/                                 ★ 工具层业务实现
│       ├── db.py          数据访问（配置/连接/建表，TODO 集中区）
│       ├── scope.py       数据范围闸门（八表未覆盖 → 不部署算法）
│       ├── capability_switch.json  能力开关
│       ├── layers.py      三层模型（Benford / DEA-DA 单纯形 / CART 随机森林）
│       ├── engine.py      编排（阈值/压力测试/审批/取严）
│       ├── tracking.py    三态追踪与状态机
│       ├── cleaning.py    数据清洗与入库
│       └── audit.py       留痕归档与查询
├── sql/v8_schema.sql                       ★ 5 张表建表语句（唯一权威定义）
└── deploy/.env                              配置（口令只在服务器）
```

---

## 5. 关键模块实现要点

| 模块 | 要点 |
|---|---|
| `gateway.py` | 只做路由与事件分发；**不计算**；SSE 用 chunked 编码（不设 Content-Length，配 `X-Accel-Buffering: no` 防 Nginx 缓冲）；只听 `127.0.0.1`，公网由 Nginx 反代 |
| `taskhub.py` | 一个 session 一个事件队列；任务执行在独立线程；步骤失败不吞异常，写入 `step.error` 并继续/终止可判；完成后自动写一条留痕 |
| `qwen_adapter.py` | 强制 JSON 输出 + `validate_plan` 工具白名单校验；失败带 `fallback_reason` 降级，前端可见 |
| `mcp_client.py` | 三种传输共用同一 `handle()`；http 模式**没有 token 直接拒绝启动**，不裸奔 |
| `v8_mcp_server.py` | 每个工具调用前先过 `scope.gate()`；未覆盖能力返回 `not_covered` 占位，不产出任何业务数字 |
| `scope.py` | 能力声明依赖的八表分表/字段；开关只能关不能强行开（覆盖不满足时拒绝启用） |

### 5.1 双向交互与策略实时调整怎么成立的

1. **持续对话**：`/api/chat` 每次下发都是一次新任务，但共享同一 `session_id` 与 SSE 流 → 上下文连续。
2. **实时回传**：任务每推进一步就发一个事件，前端无需轮询（不做"假实时"）。
3. **参数热调整**：改参数 → `POST /api/params` → 若当前任务在跑则**先取消再按新参数重跑**，前端立刻看到新任务的时间线与新灯色。
4. **结果可解释**：工具层返回后由千问（或本地模板）生成一句人话结论，同样经 SSE 回传，且不改写任何数字。

---

## 6. 部署步骤

### 6.1 环境准备

```bash
# Python ≥ 3.10，依赖只有两个
pip install pymysql openpyxl        # openpyxl 仅导入八表时用；运行时不强依赖
# MySQL 8.x 只绑 127.0.0.1；应用账号禁止用 root
mysql -h 127.0.0.1 -P <端口> -u root -p < sql/v8_schema.sql
```

### 6.2 配置（`deploy/.env`）

```ini
# ---- 数据库（必填）----
CF_DB_HOST=127.0.0.1
CF_DB_PORT=3307            # 生产常为 3306
CF_DB_USER=cf_app          # 禁止 root
CF_DB_PASSWORD=<填>
CF_DB_NAME=cfqa_workbench  # 生产库名不得为 cfqa_ 前缀
CF_AUDIT_ACTOR=system

# ---- MCP 工具层（http 传输时必填）----
CF_MCP_TOKEN=<填一个长随机串>
CF_MCP_PORT=8090

# ---- 编排网关 ----
CF_GATEWAY_PORT=8100
MCP_TRANSPORT=inprocess    # inprocess|stdio|http
MCP_HTTP_URL=http://127.0.0.1:8090/mcp

# ---- 千问办公（可留空 → 自动降级本地编排）----
QWEN_API_BASE=
QWEN_API_KEY=
QWEN_MODEL=qwen-plus
QWEN_MODE=auto
```

### 6.3 启动

```bash
# 1) MCP 工具层（远程部署时；本机用 inprocess 可省略）
python mcp-server/program_v1/src/v8_mcp_server.py --http --port 8090

# 2) 编排网关（前端唯一入口）
cd qwen-integration && python -m orchestrator.gateway      # 默认 127.0.0.1:8100

# 3) 前端
浏览器打开 http://127.0.0.1:8100/
```

生产：用 systemd 常驻（见 §6.5）+ Nginx 反代 `https://<域名>/mcp`（MCP）与 `https://<域名>/`（前端，SSE 需关闭缓冲）。

### 6.4 验证（可直接执行）

```bash
cd qwen-integration && python verify_v10.py
```

预期：12 个工具、`risk.assess_v8` 三场景出数、三态流转、清洗批次落库、留痕查询、
SSE 事件序列完整、参数热调整触发重跑 —— 全部 PASS（详见脚本内每项预期）。

手工验证：

```bash
curl -s http://127.0.0.1:8100/api/health | head -c 400
curl -s -X POST http://127.0.0.1:8100/api/chat -H "Content-Type: application/json" \
  -d '{"session_id":"s1","text":"跑一遍制造业风险评估","params":{"industry":"制造业","scenario":"risk","amount":300000}}'
curl -N http://127.0.0.1:8100/api/events?session_id=s1      # 应看到 task.created→…→task.done
```

### 6.5 systemd（自有服务器常驻）

```ini
[Unit]
Description=Cashflow Risk Orchestrator (Qwen-centric)
After=network.target mysql.service

[Service]
Type=simple
WorkingDirectory=/opt/cashflow-risk/qwen-integration
ExecStart=/opt/cashflow-risk/venv/bin/python -m orchestrator.gateway
Restart=always
RestartSec=3
Environment=CF_GATEWAY_PORT=8100
Environment=MCP_TRANSPORT=http
EnvironmentFile=/opt/cashflow-risk/deploy/.env
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

---

## 7. 待替换项（集中清单）

| 项 | 当前默认 | 部署时替换为 |
|---|---|---|
| `CF_DB_HOST/PORT/USER/PASSWORD/NAME` | 127.0.0.1 / 3307 / cf_app / 空 / cfqa_workbench | 服务器真实库（库名不得 cfqa_ 前缀） |
| `CF_MCP_TOKEN` | 空 | 长随机串（HTTP 传输必填） |
| `CF_MCP_PORT` / `CF_GATEWAY_PORT` | 8090 / 8100 | 按你的端口规划 |
| `QWEN_API_BASE` / `QWEN_API_KEY` | 空 | 千问对外 API（**未验证是否提供**） |
| `MCP_TRANSPORT` | inprocess | 远程部署改 `http` |
| 域名 / Nginx | 无 | 反代 `/`（前端，关缓冲）与 `/mcp`（MCP） |

---

## 8. 还不能说（缺证据，别对外宣称）

- ❌ "前端已直连千问办公" —— 千问对外 API 未验证，当前经编排网关；千问未配置时是本地编排兜底。
- ❌ "千问已远程调用我们的 MCP" —— stdio 已验证，HTTP 远程仍待千问侧实测（v9 命门一）。
- ❌ "钉钉/企微支持指令下发" —— 只有推送设计，未做入口（且本次按你的要求已排除钉钉/企微）。
- ✅ 可以说：意图解析/编排委托千问（可降级）；所有金额与判定由自有服务器的确定性程序产出；数据留在自有 MySQL。

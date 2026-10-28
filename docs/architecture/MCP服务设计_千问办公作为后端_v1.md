# MCP 服务设计：以千问办公为后端中枢

> 版本：v1
> 日期：2026-10-28
> 对应方案：`docs/v8_完整方案.md`（v8.2 路线修订）
> 状态：**9 个工具已上线运行**；前端接入 MCP 为待整改项（见第六章）

---

## 一、这份文档解决什么问题

v8.2 已经把路线定死：**千问办公是后端／中枢，Skill 管 workflow，MCP 管能力调用，数值计算在自有后端。**

但实测发现一处架构缺口：

```
前端页面 ──/api/*──▶ :8000 ──直接 import──▶ 引擎      ← 绕过 MCP
千问办公 ──/mcp────▶ :8123 ──▶ 9 个 MCP 工具 ──▶ 引擎  ← 只有千问在走
```

**线上 MCP 目前只服务千问，不服务前端。**

补充一个重要事实（避免重复造轮子）：仓库里**已经存在**一个专门接收前端指令的 MCP Server 骨架 `qwen-integration/mcp_frontend_server.py`，但它**没有部署到服务器**（8090 未监听），且业务函数仍是示例实现（`TODO(业务)`）。详见第二章。

本文档给出三份 MCP 服务的整合方案，使 **MCP 成为系统唯一的能力出口**。

---

## 二、架构定位

```
┌─ 接入层 ──────────────────────────────────────────┐
│  独立 HTML 前端        千问对话界面                 │
└──────────┬────────────────────┬───────────────────┘
           │ /api/*             │ 对话 + Skill 编排 workflow
           ▼                    ▼
   public_server.py      千问办公（后端 / 中枢）
   （协议转换，可选）              │
           │                    │ stdio / http + token
           └────────┬───────────┘
                    ▼
        ┌───────────────────────────────┐
        │  B · 前端入口 MCP  :8090       │  ← 统一门面，不自己算数
        │  frontend.dispatch(action,…)   │
        └───────────┬───────────────────┘
                    │ MCP / JSON-RPC 2.0
                    ▼
        ┌───────────────────────────────┐
        │  A · 引擎 MCP  :8123           │  ← 唯一计算与落库出口
        │  manufacturing-workbench-      │
        │  program-v1（9 个真实工具）     │
        └───────────┬───────────────────┘
                    ▼
        计算引擎（Python · Decimal 整数分口径）
                    ▼
        MySQL  cfqa_workbench（隔离库）
```

> 路径乙（最小改动）可省略 B，由 `public_server.py` 直接作为 A 的 MCP 客户端。

**三条原则：**

1. **千问是发起方与编排方**，后端不会自行动作；没有千问或前端的调用，引擎空转。
2. **MCP 是唯一能力出口**。任何模块要算数、要读写库，都必须走 MCP 工具，禁止再开第二条 `import` 直连通道。
3. **业务数值不由千问表格公式产出**（v8.2 红线）。

---

## 二-A、已有实现盘点：三份 MCP 服务的关系

**不要再新写一份 MCP。** 仓库里已有三份，分工不同、成熟度不同：

| 代号 | 文件 | 工具 | 状态 | 业务真实性 |
|---|---|---|---|---|
| **A · 引擎服务** | `mcp-server/program_v1/src/app.py` | 9 个（现金流全链路） | ✅ **线上运行** `:8123` / `https://<域名>/mcp` | ✅ 真实 Decimal 计算 + MySQL |
| **B · 前端入口** | `qwen-integration/mcp_frontend_server.py` | 3 个（`frontend.dispatch` / `workbench.risk_assess` / `workbench.ledger_query`） | ⚠️ **仅本机文件，未部署**，8090 未监听 | ❌ 示例实现，标 `TODO(业务)` |
| **C · V8 算法** | `mcp-server/program_v1/src/v8_mcp_server.py` | V8 三层风控等 | ❌ 未上线（10 张表未建） | ✅ 引擎已实现，本机 29/29 |

### B 的现成能力（可直接复用的部分）

- **零第三方依赖**，纯标准库，Python 3.8+ 可跑。
- 双传输：默认 **stdio**（千问拉起），`--http --port 8090` 可选，**HTTP 模式强制 token**（`CF_MCP_TOKEN`，未配置拒绝启动）——这一点比 A 做得好，A 目前无鉴权。
- 统一入口 `frontend.dispatch(action, params)`：参数校验 → `ACTION_ROUTER` 路由 → 统一错误封装。新增前端能力只需注册 action，不动协议层。
- 已预留 v8 接入点注释：`from v8 import engine as v8engine`。
- 配套齐全：`mcp_frontend_server.config.json`（千问注册片段）、`test_mcp_frontend.py`（166 行自测）。
- 协议细节已踩过坑：日志一律写 stderr，stdout 只走协议；Windows 强制 UTF-8。

### B 缺什么

`biz_risk_assess()` 只是"灯色→等级→责任人"的映射，`biz_ledger_query()` 是内存示例数据。**没有任何真实计算，也没有接 MySQL。**

### 整合结论

```
千问办公（后端/中枢）
   │ stdio 或 http
   ▼
B · 前端入口（frontend.dispatch）     ← 保留协议层与 action 路由
   │ 把 biz_* 从示例改为调用
   ▼
A · 引擎服务（9 个真实工具）          ← 唯一计算与落库出口
   │
   ▼
C · V8 算法（P0 建表后并入 A）
```

**即：B 做门面，A 做引擎，C 并入 A。** 前端与千问都只跟 B 说话，B 不自己算数。

---

## 三、服务规格

| 项 | 值 |
|---|---|
| 服务名 | `manufacturing-workbench-program-v1` |
| 实现 | `mcp-server/program_v1/src/app.py` → `build_mcp()`（FastMCP） |
| 入口 | `mcp-server/program_v1/entry.py`，模式 `stdio` / `http` |
| 协议 | MCP `2025-06-18`，JSON-RPC 2.0 |
| 传输 | stdio（本机/桌面端）；streamable-http（远程，SSE 响应） |
| 监听 | `127.0.0.1:8123`（仅回环，公网经 Nginx） |
| 公网地址 | `https://<域名>/mcp` |
| 进程管理 | systemd `cf-mcp-http.service`，`Restart=always` |
| 配置来源 | `/opt/cashflow/deploy/.env`（`EnvironmentFile`） |
| Host 白名单 | `MCP_ALLOWED_HOSTS`（必须含域名，否则 `Invalid Host header`） |
| 数据库 | `cfqa_workbench.mfg_cashflow_runs_v1` |
| 鉴权 | **当前无 token**（见第八章风险） |

### 3.1 部署单元

```ini
# deploy/cf-mcp-http.service（现行配置）
[Service]
User=cfrun
WorkingDirectory=/opt/cashflow/mcp-server/program_v1
EnvironmentFile=/opt/cashflow/deploy/.env
Environment=MCP_HTTP_HOST=127.0.0.1
Environment=MCP_HTTP_PORT=8123
ExecStart=/opt/cashflow/venv/bin/python \
          /opt/cashflow/mcp-server/program_v1/entry.py http
Restart=always
RestartSec=3
```

```nginx
# nginx：仅 /mcp 反代到 8123，/api 与页面走 8000
location /mcp {
    proxy_pass http://127.0.0.1:8123;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Connection "";
    proxy_buffering off;      # SSE 必须关缓冲
    proxy_read_timeout 300s;
}
```

---

## 四、工具清单（已上线 9 个）

> 签名与 `src/app.py` 实际代码逐条核对，非设计稿。

| # | 工具名 | 参数 | 写库 | 用途 |
|---|---|---|---|---|
| 1 | `get_workbench_status` | 无 | 否 | 架构说明、来源档案、数据库连通性 |
| 2 | `list_manufacturing_sources` | 无 | 否 | 列出原始样例与各合成输入 |
| 3 | `inspect_manufacturing_source` | `source_id: str` | 否 | 只读查看来源，绝不改原始数据 |
| 4 | `get_manufacturing_readiness` | `source_id: str` | 否 | 就绪校验，问题项保持阻断 |
| 5 | `get_manufacturing_data_audit` | `source_id: str` | 否 | 八表检查，输出问题与映射预览 |
| 6 | `calculate_manufacturing_cashflow` | `source_id: str` | **否** | Decimal 实算，仅合成来源 |
| 7 | `read_cashflow_result` | `source_id: str` | 否 | 重算并返回来源/算法摘要 |
| 8 | `save_cashflow_result` | `result: dict, request_id: str` | **是** | 仅当结果与当前来源重算完全一致才落库 |
| 9 | `query_cashflow_result` | `request_id, period, source_revision, run_id: str` | 否 | 精确回查，用于核对一致性 |

**合法 `source_id`（四个来源严禁混用）：**

| source_id | 性质 | 能否计算 | 能否落库 |
|---|---|---|---|
| `synthetic_eight_sheet_v1` | 合成八表 | ✅ | ✅ |
| `program_demo_v1` | 程序演示小样例 | ✅ | ✅ |
| `program_difference_v1` | 差异负例 | ✅（返回差异） | ❌ `can_save=False` |
| `manufacturing_sample_v1` | 原始埋错样例 | ❌ 阻断 | ❌ |

### 4.1 金值基准（验收对照用）

| 场景 | 期初 | 流入 | 流出 | 期末 | 差异 |
|---|---|---|---|---|---|
| `synthetic_eight_sheet_v1` | 100000.00 | 185000.00 | 108000.00 | **177000.00** | 0.00 |
| `program_demo_v1` | — | — | — | **140000.00** | 0.00 |
| `program_difference_v1` | — | — | — | 140000.00 | **-1000.00**（独立期末 139000.00） |

三组构成：经营 +6 万、投资 −1.5 万、筹资 +3.2 万。

---

## 五、调用方式

### 5.1 握手（必须，否则 `Missing session ID`）

```http
POST /mcp HTTP/1.1
Host: <域名>
Content-Type: application/json
Accept: application/json, text/event-stream

{"jsonrpc":"2.0","id":1,"method":"initialize",
 "params":{"protocolVersion":"2025-06-18","capabilities":{},
           "clientInfo":{"name":"workbench-frontend","version":"1"}}}
```

响应头返回 **`mcp-session-id`**，后续所有请求必须带上。响应体是 SSE：

```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18",...}}
```

> ⚠️ 两个坑：① 响应要取 `data:` 行解析，不能直接 `json.loads(整个body)`；② `notifications/initialized` 返回**空 body**，解析前必须判空。

### 5.2 列工具 / 调工具

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call",
 "params":{"name":"calculate_manufacturing_cashflow",
           "arguments":{"source_id":"synthetic_eight_sheet_v1"}}}
```

### 5.3 最小可用客户端（Python）

```python
import json, urllib.request

class McpClient:
    """streamable-http 最小客户端：管理 session，解析 SSE。"""
    def __init__(self, url, host, timeout=30):
        self.url, self.host, self.timeout = url, host, timeout
        self.session = None

    def _post(self, payload):
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Host": self.host})
        if self.session:
            req.add_header("mcp-session-id", self.session)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if r.headers.get("mcp-session-id"):
                self.session = r.headers["mcp-session-id"]
            raw = r.read().decode().strip()
        if not raw:                      # 通知类返回空 body
            return None
        for line in raw.splitlines():    # SSE：取 data 行
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(raw)

    def connect(self):
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18",
                               "capabilities": {},
                               "clientInfo": {"name": "workbench", "version": "1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def call(self, tool, **arguments):
        res = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": tool, "arguments": arguments}})
        if res and "error" in res:
            raise RuntimeError(res["error"])
        return res["result"]
```

---

## 六、前端接入 MCP（本次整改主体）

### 6.0 两条可选路径

| 路径 | 做法 | 适用 |
|---|---|---|
| **甲（推荐）** | 启用 B 作为统一入口，前端与千问都调 B；B 的 `biz_*` 改为调 A | 符合"千问为后端中枢"，且复用已有骨架与 token 鉴权 |
| 乙（最小改动） | 不动 B，仅把 `public_server.py` 从直连引擎改为 MCP 客户端调 A | 只想快速消除"前端绕过 MCP"，暂不做统一入口 |

以下 6.1–6.3 为路径乙的具体改法（最简单、可先行）；6.4 为路径甲的落地清单。

### 6.1 改法（路径乙）

**`public_server.py` 不再 `import` 引擎，改为 MCP 客户端。**

现状（要删掉的直连）：

```python
from program_workbench import project_status, list_sources, calculate, ...
# do_GET:  if path.path == "/api/calculate": ... calculate(source_id)
```

目标：

```python
MCP = McpClient("http://127.0.0.1:8123/mcp", host="127.0.0.1").connect()

# /api/calculate?source_id=xxx  →  仅做协议转换，不再自己算
result = MCP.call("calculate_manufacturing_cashflow", source_id=source_id)
```

路由映射表：

| 现有 HTTP 路由 | 改调 MCP 工具 |
|---|---|
| `GET /api/status` | `get_workbench_status` |
| `GET /api/sources` | `list_manufacturing_sources` |
| `GET /api/calculate` | `calculate_manufacturing_cashflow` |
| `GET /api/readiness` | `get_manufacturing_readiness` |
| `GET /api/audit` | `get_manufacturing_data_audit` |
| `POST /api/save` | `save_cashflow_result` |
| `POST /api/query` | `query_cashflow_result` |
| `GET /api/console` | 保留本地（进程/端口探测，不属于业务能力） |

### 6.2 必须处理的四件事

1. **MCP 返回结构是 content 包装**。`tools/call` 的 `result` 形如 `{"content":[{"type":"text","text":"{...}"}],"structuredContent":{...}}`。优先取 `structuredContent`，没有再解析 `content[0].text`，**不要直接把整个 result 塞给前端**。
2. **session 失效重连**。8123 重启后旧 session 作废，捕获 `Missing session ID` / 连接错误 → 重新 `connect()` 重试一次，仍失败则如实返回 `503 mcp_unavailable`，**禁止降级为本地直连计算**（否则又开第二条通道）。
3. **超时与耗时**。前端计算当前约 0.03 s，加一跳后仍在毫秒级；`urlopen` 设 `timeout=30`，Nginx `proxy_read_timeout` 不小于该值。
4. **金额口径不变**。MCP 只是传输层，引擎仍是 Decimal 整数分；改完必须重跑金值基准（4.1 表），三项全部一致才算完成。

### 6.3 验收

```bash
# 1) 直连通道已关闭：public_server.py 中不应再 import program_workbench
grep -n "program_workbench" /opt/cashflow/mcp-server/program_v1/src/public_server.py   # 期望无输出

# 2) 停掉 MCP 后，前端计算必须失败而不是自己算
systemctl stop cf-mcp-http
curl -s https://<域名>/api/calculate?source_id=synthetic_eight_sheet_v1   # 期望 503
systemctl start cf-mcp-http

# 3) 金值回归
curl -s https://<域名>/api/calculate?source_id=synthetic_eight_sheet_v1  # 177000.00 / matched
curl -s https://<域名>/api/calculate?source_id=program_difference_v1     # -1000.00 / can_save=false
```

### 6.4 路径甲落地清单（启用 B 作为统一入口）

**第 1 步：把 B 的示例业务换成真实调用**

```python
# qwen-integration/mcp_frontend_server.py
# 替换 biz_risk_assess / biz_ledger_query 的 TODO(业务) 段
from mcp_client import McpClient          # 见 5.3
ENGINE = McpClient("http://127.0.0.1:8123/mcp", host="127.0.0.1").connect()

def biz_cashflow_calculate(params):
    """现金流计算：转发到 A，B 不做任何数值运算。"""
    return ENGINE.call("calculate_manufacturing_cashflow",
                       source_id=params["source_id"])

ACTION_ROUTER = {
    "echo":               lambda p: {"echo": p.get("text")},
    "cashflow.calculate": biz_cashflow_calculate,
    "cashflow.save":      lambda p: ENGINE.call("save_cashflow_result", **p),
    "cashflow.query":     lambda p: ENGINE.call("query_cashflow_result", **p),
    "source.list":        lambda p: ENGINE.call("list_manufacturing_sources"),
    "source.audit":       lambda p: ENGINE.call("get_manufacturing_data_audit", **p),
    "risk.assess":        biz_risk_assess,     # 待 C 上线后接 deada_score
    "ledger.query":       biz_ledger_query,    # 待接 MySQL
}
```

**第 2 步：注册 action 的入参校验**

在 `ACTION_SCHEMAS` 中为每个新 action 声明必填字段与类型；`source_id` 必须限制在四个合法来源内（见 4.1 前的来源表），非法值直接拒绝，不下发到 A。

**第 3 步：部署 B 到服务器**

```ini
# /etc/systemd/system/cf-mcp-frontend.service
[Service]
User=cfrun
WorkingDirectory=/opt/cashflow/qwen-integration
EnvironmentFile=/opt/cashflow/deploy/.env
Environment=CF_MCP_TOKEN=<强随机值>
ExecStart=/opt/cashflow/venv/bin/python \
          /opt/cashflow/qwen-integration/mcp_frontend_server.py --http --port 8090
Restart=always
RestartSec=3
```

Nginx 增加 `location /mcp-frontend { proxy_pass http://127.0.0.1:8090; ... }`，对外 `https://<域名>/mcp-frontend`。

**第 4 步：千问侧注册**

用 `mcp_frontend_server.config.json` 的 `_http_alternative` 片段，`url` 改为上述域名地址，`X-MCP-Token` 与 `CF_MCP_TOKEN` 一致。

**第 5 步：前端切换**

前端把 `/api/calculate` 等调用改为对 B 的 `frontend.dispatch`：
`{"action":"cashflow.calculate","params":{"source_id":"synthetic_eight_sheet_v1"}}`。
过渡期可保留 `public_server.py` 做这层协议转换，前端页面零改动。

---

## 七、Skill 侧编排（千问作为后端的另一半）

MCP 只解决"能力怎么被调用"，**workflow 由 Skill 承载**。千问按 SKILL.md 串步骤：

```
用户意图
  → list_manufacturing_sources          让用户选来源
  → get_manufacturing_readiness         先校验，不通过就停并解释原因
  → calculate_manufacturing_cashflow    实算
  → 解释结果（三组构成 / 余额核对 / 风险标记）
  → save_cashflow_result                仅当 can_save=True
  → query_cashflow_result               回查四项一致后才报"已留档"
```

配套文件（已存在）：`qwen-integration/SKILL.md`、`connector-stdio.json`、`connector-http.json`。

**顺序红线**：不得跳过 readiness 直接计算；不得在未回查的情况下宣称已保存。

---

## 八、风险与待办

| 项 | 状态 | 处置 |
|---|---|---|
| `/mcp` 公网无鉴权 | ⚠️ 高风险 | 直接复用 B 的 `CF_MCP_TOKEN` 方案（未配置拒绝启动），或参照 `v8_mcp_server.py` 的 Bearer 实现 |
| B 未部署、业务为示例 | ⚠️ | 按 6.4 五步落地；在 `biz_*` 接真实引擎前不得对外演示 |
| 前端绕过 MCP | ⚠️ 架构缺口 | 按第六章整改，改完跑 6.3 验收 |
| V8 的 7 个算法工具未上线 | ❌ | `quality_check / deada_score / rf_predict / stress_test / verify_payment / log_exception / param_selfcorrect`，随 P0 建表后注册进 `build_mcp()` |
| 主站无身份认证 | ⚠️ | 演示可接受，对外交付前加 `auth_basic` |
| 千问侧远程 MCP 未实测 | ☐ | 导入 `connector-http.json` 后跑一次完整 workflow 存档 |

---

## 九、一句话总结

**不需要新写 MCP——三份都已存在。** A（9 个真实工具）在线跑着，B（前端入口）有完整骨架但没部署、业务是示例，C（V8 算法）等建表。

要真正做到"千问办公为后端、MCP 统一调算法"，只剩两件事：
**把 B 的示例业务换成对 A 的真实调用并部署上线（6.4 五步），同时给 MCP 端点补上 token 鉴权。**

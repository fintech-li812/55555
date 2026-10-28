# 企业现金流风险工作台（Cashflow Risk Workbench）

把 **现金流计算 → 三层风险判别 → 留痕回查 → AI（Agent / MCP）接入** 串成一条闭环的全栈工作台。

- **后端**：纯 Python 标准库（`http.server` + `pymysql`），无 Web 框架、无 ORM。
- **前端**：单文件 HTML，双击即可在浏览器打开，无构建步骤。
- **可信性设计**：算错的、连不上的、没配的，一律如实报错，**绝不返回伪造的成功结果**。

当前状态：**前后端全链路已打通并已部署上线**，121 项自动化测试通过。
未完成项见 [ROADMAP.md](ROADMAP.md)（蓝图）与 [TODO.md](TODO.md)（可执行清单）。

---

## 目录结构

```
.
├── README.md              本文件
├── ROADMAP.md             未完成功能蓝图
├── TODO.md                待办清单（可直接认领）
├── AGENTS.md              AI 协作约定（接手前必读）
│
├── mcp-server/            后端主程序
│   └── program_v1/
│       ├── src/           服务与引擎（public_server / v8_server / v8 引擎 / 融合规则）
│       ├── tests/         测试（121 项）
│       ├── inputs/        固定合成输入模板
│       ├── fixtures/      测试夹具
│       └── evidence/      取证证据
│
├── web/                   前端页面（单文件，无构建）
│   ├── 统一工作台.html     ← 主入口（总览/现金流/风险/留痕/控制台 五合一）
│   ├── 工作台_程序计算版.html
│   ├── v8_工作台.html
│   ├── 后端控制台.html
│   └── archive/           历史页面（早期演示看板、未完成的 v10）
│
├── deploy/                生产部署配置
│   ├── env.template       ← 环境变量示例（复制为 .env 后填写）
│   ├── nginx-workbench.example.com.conf
│   ├── *.service           systemd 单元
│   ├── serve.py           生产入口包装
│   ├── ssh_run.py         远程运维通道（口令从 .sshpw 读取）
│   ├── init_v8_tables.sh  建表脚本
│   └── agent/             常驻 Agent 服务
│
├── scripts/               本机启动脚本（Windows）
│   ├── 启动一体化工作台.ps1  ← 一键启动
│   └── 打开一体化工作台.cmd  ← 双击入口
│
├── sql/                   建表语句（init.sql / v8_schema.sql / ...）
├── rollcast/              滚动预测模块（独立子项目，自带测试）
├── qwen-integration/      千问（Agent）接入：MCP 服务、技能、连接器配置
├── model/                 风控模型（早期 v2 版本，供参考）
├── data/                  样例数据（含刻意埋错的样例，见下方说明）
├── docs/                  文档（见 docs/README.md 索引）
├── outbox/                历史交付物（技能包 zip 等）
├── templates/             行业模板
├── tables/                破坏数据清单
├── migration/             迁移脚本
├── mobile/                移动端页面
├── agent-prompts/         Agent 提示词
└── ppt/                   演讲材料
```

---

## 快速开始

### 1. 环境要求

| 组件 | 版本 | 是否必需 | 说明 |
|---|---|---|---|
| Python | 3.12+ | 必需 | 后端依赖标准库 + pymysql |
| MySQL | 8.0+ | **可选** | 不连库时，读接口可用，写接口会如实返回 `pending` / 503 |
| Node.js | — | 不需要 | 前端是单文件 HTML，无构建步骤 |
| Nginx | — | 仅生产 | 本机不需要 |

### 2. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r mcp-server/requirements.txt
```

- **Windows**：`requirements.txt` 含 `pywin32`，保留。
- **Linux 服务器**：改用 `pip install -r deploy/requirements.server.txt`
  （内容相同但移除了 Windows 专用项，并放开传递依赖版本让 pip 自选）。

### 3. 配置环境变量

```bash
cp deploy/env.template deploy/.env
```

`deploy/.env` **不会进仓库**（已在 `.gitignore` 中）。最少改动即可启动：

```ini
WORKBENCH_PORT=8000
WORKBENCH_ALLOWED_HOSTS=127.0.0.1:8000,localhost:8000
CF_V8_PORT=8010
```

> **`WORKBENCH_ALLOWED_HOSTS` 必须与浏览器地址栏完全一致**，包含端口。
> 填 `127.0.0.1` 却用 `localhost` 打开 → 请求会被 403 拒绝。这是最常见的启动失败原因。

完整配置项见 [deploy/env.template](deploy/env.template)，逐项都有注释说明。

### 4. 启动

**Windows（推荐）** —— 双击 `scripts/打开一体化工作台.cmd`，或：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\启动一体化工作台.ps1 -WithDatabase
```

脚本会按 **V8 → 主站 →（可选）隔离数据库** 的顺序拉起，端口被占用则复用现有进程。
`-WithDatabase` 会额外拉起项目自带的隔离 MySQL 实例（3307），**冷启动需等 1~2 分钟**（InnoDB 崩溃恢复 + 重建 undo 表空间），属正常。

**Linux / macOS** —— 开两个终端：

```bash
# 终端 1：V8 三层风控服务
cd mcp-server/program_v1/src
CF_V8_PORT=8010 python v8_server.py

# 终端 2：主站（页面 + 主 API + V8 反向代理）
cd mcp-server/program_v1/src
WORKBENCH_PORT=8000 \
WORKBENCH_ALLOWED_HOSTS=127.0.0.1:8000,localhost:8000 \
python public_server.py
```

> 两个服务都**必须在 `src/` 目录下启动**，因为它们会 `import` 同目录的 `config`、`service` 等模块。
> 若在别处启动，需额外设置 `PYTHONPATH` 指向 `mcp-server/program_v1/src`。

### 5. 验证

打开 **http://127.0.0.1:8000/unified**

| 检查项 | 预期结果 |
|---|---|
| 顶部徽标 | `后端已连接` |
| 总览卡片 | `连接 在线` / `端口 5/5` / `数据库 已连接`（未连库时显示未连接，属实） |
| 后端控制台 | 能看到 5 个服务的端口与在听状态，含 `cf-v8-web:8010` |

命令行复核：

```bash
curl http://127.0.0.1:8000/api/status                 # database=connected
curl http://127.0.0.1:8000/v8/api/v8/industries       # V8 经主站代理可达
curl -s http://127.0.0.1:8000/unified | grep -c "token !== '__CSRF_TOKEN__'"   # 应为 1
```

> 最后一条常被忽略：**页面必须由 `public_server` 提供**，服务端注入令牌后才能调用写接口。
> 直接双击打开 HTML 会看到红色横幅提示，写接口会被拒绝（这是设计，不是故障）。

---

## 服务与端口

| 服务 | 文件 | 默认端口 | 环境变量 | 用途 |
|---|---|---|---|---|
| 主站 | `public_server.py` | 18767 | `WORKBENCH_PORT` | 页面 + 主 API + **V8 反向代理** |
| V8 风控 | `v8_server.py` | 8010 | `CF_V8_PORT` | 三层判别（Q/D/P） |
| MCP over HTTP | — | 8123 | `MCP_HTTP_PORT` | 供千问调用，与页面 API 不是一回事 |
| 旧页面服务 | `web_server.py` | 18766 | — | 历史遗留，新开发走主站 |
| 隔离 MySQL | — | 3307 | `CF_DB_PORT` | 本机专用，不对外 |

### 为什么主站要反向代理 V8

页面会请求 `/v8/api/v8/*`。生产环境由 Nginx 的 `location /v8/` 转发；本机没有 Nginx，
因此 `public_server.py` 内置了同源代理补上这一环。

这样做的关键收益：页面只有一个数据源，**后端完全不用开 CORS，CSP 也不用放宽，前端代码零改动**。
上游地址只接受回环地址（`127.0.0.1` / `localhost` / `::1`），防止被滥用成内网代理。

> 生产与本机**二选一**：如果 Nginx 已配了 `/v8/`，就别再让主站代理，否则会转发两跳。

---

## 数据流与鉴权

```
浏览器 ──同源──> 主站(8000) ──反代 /v8/api/v8/*──> V8(8010) ──> MySQL
                    │
                    └──> 主 API：/api/calculate  /api/save  /api/query  /api/audit
```

1. 主站启动时生成（或读取固定的）会话令牌；
2. 返回页面时，令牌被注入 `<meta name="workbench-token">`；
3. 页面从 meta 读取，写接口用 `X-Workbench-Token` 头带回；
4. 服务端用 `compare_digest` 校验，另有 Host / Origin 白名单。

**注意：令牌替换只针对 meta 标签。** 页面 JS 里有一处哨兵 `token !== '__CSRF_TOKEN__'`
用来判断「页面是否由服务端提供」，占位符共出现 2 次但**只有 meta 内那处该被替换**。
若用无差别全局替换，哨兵会变成 `token !== '<真令牌>'`，导致 `served` 恒为 false，
所有同源请求在浏览器里就被短路——请求根本发不出去。

---

## 继续开发 Agent 相关功能

Agent 侧入口集中在 `qwen-integration/`：

| 文件 | 用途 |
|---|---|
| `mcp_stdio.py` / `mcp_frontend_server.py` | MCP 服务实现（stdio / HTTP 两种传输） |
| `connector-stdio.json` / `connector-http.json` | 千问连接器配置（导入平台用） |
| `skills/` | 技能定义 |
| `orchestrator/` | 编排逻辑 |
| `outbox/*.zip` | 已打包的技能包，可直接导入千问 |

```bash
# 以 HTTP 方式启动 MCP，供千问连接
cd qwen-integration
python mcp_frontend_server.py

# 验证
python test_mcp_frontend.py
```

后端暴露给 Agent 的能力（MCP 工具）定义在 `mcp-server/program_v1/src/` 下，
V8 的三层判别已拆成独立工具，与整包 `risk.assess_v8` 结果一致（有测试保证）。

开发约定见 [AGENTS.md](AGENTS.md)，尤其是「算错的必须如实报错」「不把合成数据称为真实验证」两条。

---

## 测试

```bash
# 全量（推荐）
python -m pytest mcp-server/program_v1/tests/ rollcast/tests/ -q

# 也可以直接运行单个文件（每个文件都自带 __main__）
python mcp-server/program_v1/tests/test_fusion_rules.py
python mcp-server/program_v1/tests/test_v8_gap_closure.py
```

| 测试文件 | 项数 | 覆盖 |
|---|---|---|
| `test_v8_gap_closure.py` | 25 | V8 差距补齐、Bland 规则跨平台可复现 |
| `test_fusion_rules.py` | 33 | 三层融合规则的互斥完备性与边界 |
| `rollcast/tests/test_rollcast.py` | 63 | 滚动预测 |

设计与执行原则：**不 skip、不 xfail、不静默降级**，任何一项失败就是失败。

---

## 关于数据的重要说明

- `data/制造业_样例数据.xlsx` 是**刻意埋错的样例**，用于检验程序能否识别问题，
  **不能当作正常数据计算**。
- 可正常计算的是固定合成模板，位于 `mcp-server/program_v1/inputs/`，
  以及 `data/制造业_完整八表_合成演示_v1.xlsx`。
- 所有合成输入在系统中被**显著标记**为合成数据。引擎只接受已登记的固定模板，
  **不是通用导入器**——不要传任意文件路径。
- `data/` 下三个历史样例文件是**只读**的（`chmod 444`），改动前请确认。

---

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 页面 403 | `WORKBENCH_ALLOWED_HOSTS` 与地址栏不一致（含端口），改一致即可 |
| 徽标显示「部分后端已连接」 | 通常是 V8(8010) 没起；检查 `WORKBENCH_V8_ORIGIN` 是否指向实际地址 |
| 保存返回 `pending` / 回查 503 | 数据库没连上。**这是如实反馈，不是 bug**，连上库即可变成 `committed` |
| 页面提示红色横幅 | 页面不是由 `public_server` 提供的（如直接双击 HTML），写接口会被拒 |
| 隔离库起不来 | 冷启动需 1~2 分钟；若卡住，看 `deploy/.mysqldata/server.err`，undo 残留由启动脚本自动归档 |
| `curl -sI` 返回 501 | 正常。服务端未实现 `do_HEAD`，用 `curl -s` 即可 |
| 本机端口冲突 | 用 `-MainPort` / `-V8Port` 换端口，**不要改 `deploy/.env`**（那是生产配置） |

---

## 安全说明

- 仓库为公开仓库，**不保留任何真实口令、服务器地址与域名**。
  生产 IP 与域名在文档与配置中以 `<SERVER_IP>`、`workbench.example.com` 占位。
- 数据库按最小权限分三账号：`cf_app`（增删改查，应用用）、`cf_admin`（建表迁移）、`cf_backup`（备份）。
  **应用运行时用的是 `cf_app`，不需要 root。**
- 需要的配置项全部列在 [deploy/env.template](deploy/env.template)，复制为 `.env` 后填写，
  `.env` 已在 `.gitignore` 中。
- 远程运维脚本 `deploy/ssh_run.py` 的口令从 `deploy/.sshpw` 读取，该文件同样被忽略。

> 不把口令贴进对话或提交到仓库。需要 DDL 时出 SQL 交给有权限的人执行。

---

## 延伸阅读

- [docs/README.md](docs/README.md) —— 文档索引（架构 / 运维 / 验收 / 历史归档）
- [ROADMAP.md](ROADMAP.md) —— 未完成功能蓝图
- [TODO.md](TODO.md) —— 可执行待办
- [AGENTS.md](AGENTS.md) —— AI 协作约定
- [docs/operations/部署手册.md](docs/operations/部署手册.md) —— 生产部署
- [docs/operations/前后端联通方案_20261028.md](docs/operations/前后端联通方案_20261028.md) —— 联调与排障实录

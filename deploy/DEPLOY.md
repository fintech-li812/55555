# 现金流风险工作台 — 服务器部署说明

服务器：`<SERVER_IP>`（阿里云 ECS，Ubuntu 24.04.5，2 vCPU / 1.7 GB / 40 GB）
域名：`https://workbench.example.com`（复用原有 Let's Encrypt 证书，有效期至 2027-01-14）

## 对外入口

| 用途 | 地址 |
|---|---|
| 工作台页面 | https://workbench.example.com/ |
| HTTP 接口 | https://workbench.example.com/api/* |
| 后端控制台 | https://workbench.example.com/console |
| 引擎 MCP（内部，无鉴权） | https://workbench.example.com/mcp |
| 前端 MCP 网关（有鉴权） | https://workbench.example.com/mcp-frontend |

## 进程与端口

| 服务 | systemd 单元 | 监听 | 说明 |
|---|---|---|---|
| 页面 + API | `cf-workbench` | 127.0.0.1:8000 | 只绑回环，由 nginx 终止 TLS |
| MCP over HTTP | `cf-mcp-http` | 127.0.0.1:8123 | 同上，路径 `/mcp` |
| 前端 MCP 网关 | `cf-mcp-frontend` | 127.0.0.1:8090 | 路径 `/mcp-frontend`，需 `X-MCP-Token` |
| 网关 | `nginx` | 0.0.0.0:80/443 | 80 强制跳 443 |

常用命令：

```bash
systemctl status cf-workbench cf-mcp-http cf-mcp-frontend
systemctl restart cf-workbench
journalctl -u cf-workbench -n 50 --no-pager
nginx -t && systemctl reload nginx
```

## 定时任务

| 单元 | 频率 | 作用 |
|---|---|---|
| `cf-mysql-backup.timer` | 每天 03:17（±5 分钟随机延迟） | 逻辑备份所有 `cf*` 库到 `/var/backups/cashflow/mysql/`，gzip，保留 14 天 |
| `cf-health-check.timer` | 每 5 分钟 | 探测 5 个 systemd 单元 + 4 个 HTTPS 端点 + 根分区水位，失败计入 `systemctl --failed` |

```bash
systemctl list-timers --no-pager | grep cf-
systemctl start cf-mysql-backup.service      # 手动跑一次备份
systemctl start cf-health-check.service      # 手动跑一次巡检
journalctl -u cf-health-check.service -n 20 --no-pager
```

两个脚本都只观察、不自动修复：重启交给各单元自己的 `Restart=always`，避免巡检脚本掩盖真实故障。备份脚本在“找不到 `cf*` 库”或“产物小于 1 KB”时按失败处理，不会留下一个看似成功的空备份。

## 前端 MCP 网关

`qwen-integration/mcp_frontend_server.py` 的 HTTP 模式，是给外部调用方（含自研前端）用的带鉴权入口，它自己不算数，只把 10 个动作转发到内部引擎 MCP。

- 鉴权头：`X-MCP-Token`，取值在 `/opt/cashflow/deploy/.mcp-frontend.env`
- **未配置 token 时进程拒绝启动**，不存在“忘了设就裸奔”的状态
- 上游引擎地址 `CF_ENGINE_MCP_URL=http://127.0.0.1:8123/mcp`，走回环不绕 nginx
- nginx 把 `/mcp-frontend` 重写到上游的 `/health`，因此同一个入口既能做健康检查（GET，免鉴权）也能收 JSON-RPC（POST，需鉴权）

```bash
TOKEN=$(sudo grep '^CF_MCP_TOKEN=' /opt/cashflow/deploy/.mcp-frontend.env | cut -d= -f2)
curl -s https://workbench.example.com/mcp-frontend                                  # 健康检查，200
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://workbench.example.com/mcp-frontend \
     -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# -> 401
curl -s -X POST https://workbench.example.com/mcp-frontend \
     -H 'Content-Type: application/json' -H "X-MCP-Token: $TOKEN" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"calculate_cashflow","arguments":{"source_id":"synthetic_eight_sheet_v1"}}}'
```

## 目录

```
/opt/cashflow/
├── mcp-server/program_v1/     # 计算引擎（Python，未改一行算法）
│   ├── src/                   # 六个模块 + public_server.py + notify.py
│   ├── demo_eight/            # 八表演示的证据包（含 approved_files 哈希）
│   ├── fixtures/              # 受信任历史结果（SHA256 校验）
│   └── inputs/                # 两张合成输入 xlsx + 注册表
├── data/                      # 八表工作簿（引擎从项目根 data/ 读取）
├── web/                       # 前端单页
├── qwen-integration/          # 前端 MCP 网关（转发到内部引擎，带鉴权）
├── deploy/                    # .env、systemd 单元、nginx 配置、脚本、验证工具
└── venv/                      # Python 3.12 虚拟环境
```

## 数据库（复用服务器已装的 MySQL 8.0.46）

- 库：`cfqa_workbench`（**隔离演示库**，故意用 `cfqa_` 前缀）
- 表：`mfg_cashflow_runs_v1`（建表语句取自 `service.py` 的 DDL，不手抄）
- 应用账号：`cf_app`，**只授予 SELECT / INSERT**，只有 `@127.0.0.1` 和 `@localhost` 两个来源
- 管理账号：`cfadmin`（口令在 `/root/.cfadmin.pw`，权限 600，仅应急用）

### 为什么是 cfqa_ 而不是生产库

`agent_guard.py` 第 39 行规定：`program_calculation`（程序计算）的结果只有开着
`allow_fixture` 才允许保存；而 `config.validate()` 又规定 `allow_fixture` 只允许出现在
`cfqa_` 前缀的隔离库。**这是有意的安全设计：合成计算结果禁止写入生产库。**
要进生产库，结果必须由千问表格产出（`CF_QWEN_BOUND` 那条链路）。

## 密钥位置（都不在仓库里）

| 内容 | 位置 | 权限 |
|---|---|---|
| 应用与数据库配置 | `/opt/cashflow/deploy/.env` | 600，属主 cfrun |
| 前端 MCP 网关令牌 | `/opt/cashflow/deploy/.mcp-frontend.env` | 600，属主 cfrun |
| MySQL 管理口令 | `/root/.cfadmin.pw` | 600 |
| 服务器 SSH 口令 | 仅本地 `deploy/.sshpw`（已 gitignore） | — |

网关令牌只在首次部署时生成一次；重跑部署脚本不会覆盖已有文件，避免悄悄让旧调用方失效。
需要轮换时，改完文件执行 `systemctl restart cf-mcp-frontend` 即可，`Restart=always` 保证进程崩了也能自己起来。

## 旧站点备份

替换前的静态站点与 nginx 配置存放在 `/root/backup-网站-20261027/`：

```
nginx/workbench.example.com          原站点配置（含 /api/site/ -> 127.0.0.1:4010）
html/index.html              原站点首页
affairs/api.py               原 /opt/affairs/api.py
```

后来 `affairs-api.service` 已按要求注销并删除（2026-10-27）：单元文件与
`/opt/affairs/api.py` 备份在 `/root/backup-affairs-20261027/`，服务 `inactive`、
`not-found`，4010 端口已释放。`/opt/affairs/` 目录本身保留未删。要恢复，把备份里的
单元文件放回 `/etc/systemd/system/` 并执行 `daemon-reload` 即可。

## 验收

```bash
# 全闭环：计算 -> 保存 -> 精确回查（四项核对）
set -a; . /opt/cashflow/deploy/.env; set +a
/opt/cashflow/venv/bin/python /opt/cashflow/deploy/verify_closed_loop.py
```

预期输出：期末 177000.00、差异 0.00、`committed`、四项核对全部通过。

## 已知缺口（如实记录，未含糊）

1. **开放访问，没有登录**。任何拿到网址的人都能打开页面、点保存（CSRF token 是嵌在页面里的，只防跨站伪造，不是身份认证）。要收紧，在 nginx 加一段 `auth_basic` 即可。
2. **推送通道未配置**。`DINGTALK_WEBHOOK` / `WECOM_WEBHOOK` 为空，`/api/notify` 会如实返回 `not_configured`，不假装成功。
3. **内部引擎 MCP `/mcp` 仍无鉴权**。它只用在回环上（前端网关经 `127.0.0.1:8123` 调用），但 nginx 目前也把它代理到了公网，所以外部确实能直连。新加的 `/mcp-frontend` 是带鉴权的，正确做法是给 `/mcp` 加上同样的 `X-MCP-Token` 校验，或限制其仅允许回环访问。这是本次部署**没有**动的一项，留作下一步。
4. **备份未异地留存**。备份落在同一台机器的 `/var/backups/cashflow/mysql/`，能挡误删和逻辑损坏，挡不了整机故障。真要兜底需要再同步到对象存储。
3. **存的是合成数据**，`data_type=SYNTHETIC`，不代表任何真实企业口径。
4. **千问能否直接连这个 `/mcp` 尚未验证**——服务器侧握手已通，千问侧的连接器是否接受 HTTP URL 需拿它的产品实测。
5. 证书是原来那张，**没有为子域名扩展**；如需 `cf.workbench.example.com` 要先加 DNS 记录再签新证书。

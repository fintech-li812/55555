# 常驻 Agent 服务 · 部署说明

面向 `<SERVER_IP>`（Ubuntu 24.04 / MySQL 8.0.46 / 2 核 2G）的常驻 Agent 服务。
基于 v8 构想：数据读取汇总、定时巡检告警、周报推送、前端对话、决策卡片。

---

## 1. 服务器上最终的目录结构

```
/opt/cashflow-agent/                 # 应用根目录（root:cfagent 0750）
├── venv/                            # Python 虚拟环境（共享，不随版本重建）
├── releases/
│   ├── 20261027-120000/             # 每次发布一个不可变版本目录
│   │   ├── app/                     # 业务代码
│   │   ├── scripts/                 # 运维脚本
│   │   ├── sql/                     # 初始化 SQL（含占位符口令）
│   │   ├── systemd/                 # 单元与 logrotate 模板
│   │   ├── config/                  # 配置模板（非敏感）
│   │   ├── tests/                   # 离线冒烟测试
│   │   └── requirements.txt
│   └── 20261028-090000/
├── current -> releases/20261027-120000   # 软链，切换即回滚
└── README.md

/etc/cashflow-agent/                 # 配置（root:cfagent 0750）
├── config.yaml                      # 非敏感配置  0640
└── agent.env                        # 口令/令牌   0600  ← 禁止提交仓库

/var/lib/cashflow-agent/             # 运行时状态（cfagent:cfagent）
├── state.json                       # 调度器状态（原子写）
└── watchdog_fail_count

/var/log/cashflow-agent/             # 日志（cfagent:cfagent）
├── agent.log                        # 业务日志（20MB×5 轮转）
├── error.log                        # 错误日志
└── watchdog.log                     # 自愈记录

/etc/systemd/system/
├── cashflow-agent.service           # 主服务
├── cashflow-agent-watchdog.service  # 深度自愈（oneshot）
└── cashflow-agent-watchdog.timer    # 每 5 分钟
/etc/logrotate.d/cashflow-agent      # 防止日志写满磁盘
```

## 2. 代码模块（配置与实现分离）

| 模块 | 职责 |
|---|---|
| `app/config.py` | 配置加载与校验；`public_view()` 快照永不含口令/令牌 |
| `app/logging_setup.py` | 控制台 + 轮转文件日志 |
| `app/db.py` | 数据访问；**代码层强制只允 INSERT** |
| `app/datasource.py` | 能力 1 数据读取与汇总；源表缺失如实上报 |
| `app/risk.py` | 确定性分级（阈值全在配置里） |
| `app/inspector.py` | 能力 2 巡检 + 告警 + 留痕 |
| `app/reporter.py` | 能力 3 周报生成与推送 |
| `app/chat.py` | 能力 4 意图路由与应答 |
| `app/decision_card.py` | 能力 5 决策卡片（背景/结论/建议动作/风险提示） |
| `app/channels.py` | 通道适配器；未配置 = dry-run，绝不伪造送达 |
| `app/scheduler.py` | 周期调度（纯标准库） |
| `app/httpapi.py` | HTTP + SSE（仅标准库，不引 Web 框架） |
| `app/ops_guard.py` | 高风险操作二次确认 |
| `app/main.py` | 装配与优雅退出 |

## 3. 最小权限

| 对象 | 权限 |
|---|---|
| OS 账号 `cfagent` | `--system --shell /usr/sbin/nologin`，不能交互登录 |
| systemd | `NoNewPrivileges` / `ProtectSystem=strict` / `CapabilityBoundingSet=` / 只放行 state 与 log 两个可写路径 |
| DB 账号 `cf_agent@localhost` | **仅 `SELECT, INSERT` on `cf_workbench.*`**，无 UPDATE/DELETE/DROP/CREATE |
| 端口 | 只听 `127.0.0.1:8090`；公网访问必须经 Nginx 反代 |
| 配置文件 | `agent.env` 0600 root:cfagent |

因为 DB 账号没有 UPDATE，所有状态推进采用**追加新行的不可变事件模型**，历史不可篡改。

## 4. 接口

除 `/healthz` 外全部需要 `Authorization: Bearer <CF_AGENT_API_TOKEN>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康与配置快照（不含敏感项） |
| GET | `/v1/overview?limit=` | 现金流概览 |
| GET | `/v1/receivables` | 应收与逾期汇总 |
| GET | `/v1/metrics` | 判定指标 |
| GET | `/v1/card` | 生成决策卡片 |
| GET | `/v1/inspections?limit=` | 历史巡检记录 |
| POST | `/v1/chat` | 对话（SSE 流式），body `{"message": "..."}` |
| POST | `/v1/inspect` | 立即巡检 |
| POST | `/v1/report/weekly` | 生成周报，body `{"days": 7, "push": true}` |
| POST | `/v1/ops/restart` | **高风险**，body `{"confirm": true, "confirm_phrase": "restart"}` |

## 5. 启动与自愈

```bash
systemctl start|stop|restart cashflow-agent
systemctl enable cashflow-agent          # 开机自启
journalctl -u cashflow-agent -f          # 看日志
```

三层自愈：

1. **进程级**：`Restart=always` + `RestartSec=5`；5 分钟内崩溃 5 次才放弃（`StartLimitBurst=5`）。
2. **资源级**：`MemoryMax=320M`，超限被 systemd 杀掉后由第 1 层拉起，避免拖垮同机 MySQL。
3. **假死级**：`watchdog.timer` 每 5 分钟探 `/healthz`，连续 3 次无响应才重启（避免抖动误重启）。

另外：日志 20MB×5 轮转 + logrotate 每日压缩保留 14 天，**防止磁盘写满导致静默失效**。

## 6. 部署步骤

```bash
# 0) 先把本目录上传到服务器（任选一种）
rsync -avz --delete ./deploy/agent/ root@<SERVER_IP>:/root/cfagent-src/
# 或 scp -r ./deploy/agent root@<SERVER_IP>:/root/cfagent-src

# 1) 只读采集环境（不做任何修改）
ssh root@<SERVER_IP> 'bash /root/cfagent-src/scripts/recon.sh'
#  重点看：python3 是否存在、MySQL 是否只绑 127.0.0.1、8090 端口是否空闲、内存余量

# 2) 预演（打印将要做什么，不改动系统）
ssh root@<SERVER_IP> 'bash /root/cfagent-src/scripts/install.sh --src /root/cfagent-src'

# 3) 真正部署
ssh root@<SERVER_IP> 'bash /root/cfagent-src/scripts/install.sh --src /root/cfagent-src --apply'

# 4) 验收
ssh root@<SERVER_IP> 'systemctl status cashflow-agent --no-pager; \
  curl -s http://127.0.0.1:8090/healthz | head -c 600; echo'

# 5) 功能验证（带令牌）
TOKEN=$(ssh root@<SERVER_IP> 'grep ^CF_AGENT_API_TOKEN /etc/cashflow-agent/agent.env | cut -d= -f2')
ssh root@<SERVER_IP> "curl -s -H 'Authorization: Bearer $TOKEN' -X POST \
  -H 'Content-Type: application/json' -d '{\"message\":\"现在巡检一次\"}' \
  http://127.0.0.1:8090/v1/chat"
```

部署后记得：把 `config.yaml` 里的 `sources.*` 改成服务器 `cf_workbench` 中真实存在的表名；
若源表尚未创建，服务会如实返回"数据源缺失"，不会编造数字。

## 7. 回滚方案

```bash
bash /opt/cashflow-agent/current/scripts/rollback.sh --list        # 看历史版本
bash /opt/cashflow-agent/current/scripts/rollback.sh --to 20261027-120000
#  交互式输入 rollback 确认 → 切软链 → 重启 → 打印健康结果
#  只切代码，绝不动数据

bash /opt/cashflow-agent/current/scripts/rollback.sh --stop        # 只停服务，保留数据
bash /opt/cashflow-agent/current/scripts/rollback.sh --drop-data   # 危险：需输入 DELETE DATA
```

回滚矩阵：

| 故障 | 动作 | 数据影响 |
|---|---|---|
| 新版本起不来 | `--to` 上一个版本 | 无 |
| 配置写错 | 用 `/etc/cashflow-agent/*.bak.*` 或 `*.new` 回改，重启 | 无 |
| 服务假死 | watchdog 自动重启；或 `systemctl restart` | 无 |
| 磁盘将满 | 清理 `/var/log/cashflow-agent`，logrotate 已兜底 | 无 |
| 要彻底卸载 | `--drop-data`（需输入 `DELETE DATA`） | 删 agent_* 表与账号 |

## 8. 事实边界（不要对外声称已做到）

- **推送通道默认 `none`**：未配置 `DINGTALK_WEBHOOK` / `AGENT_WEBHOOK_URL` 时，告警与周报只**落库留痕**，状态为 `not_configured`，`message_id` 为 `null`。**没有真实送达任何人。**
- **对话不是大模型闲聊**：只做意图路由 + 确定性能力调用，未命中意图会返回能力清单，不编造答案。
- **判定规则是阈值规则，不是模型推理**：刻意不在 2G 机器上常驻 sklearn。
- **源表缺失时指标为空**：会判为 `medium`（"数据源缺失"）而非 `low`，避免"没数据=没风险"的假安全。
- 服务只听 `127.0.0.1`，**没有对外暴露**；若需前端跨域访问，必须自己在 Nginx 层加鉴权与 `proxy_buffering off`（SSE 必需）。

# 千问办公接入包

把制造业现金流工作台接进千问办公。**千问负责理解意图和编排，金额全部由我们的确定性程序产出。**

## 目录

| 文件 | 用途 |
|---|---|
| `skills/mfg-program-workbench/SKILL.md` | 技能定义：来源清单、计算纪律、保存与回查纪律 |
| `mcp_stdio.py` | stdio 入口，自动加载 `deploy/.env`（否则保存用不上库） |
| `connector-stdio.json` | 千问连接器配置（本机，已验证） |
| `connector-http.json` | 远程 HTTP 配置（服务器部署后用，**待千问侧验证**） |

## 接入步骤（本机）

1. 确认 `deploy/.env` 已生成（跑过 `deploy/setup_storage.py`）。
2. 在千问办公的连接器里导入 `connector-stdio.json`。
3. 加载技能：`skills/mfg-program-workbench/SKILL.md`。
4. 试一句：「读取完整合成八表并计算现金流」——应返回经营/投资/筹资三类与余额核对。

## 9 个工具

`get_workbench_status` / `list_manufacturing_sources` / `inspect_manufacturing_source` /
`get_manufacturing_readiness` / `get_manufacturing_data_audit` / **`calculate_manufacturing_cashflow`** /
`read_cashflow_result` / `save_cashflow_result` / `query_cashflow_result`

算钱的是 `calculate_manufacturing_cashflow`——我们自己的 Decimal 代码。千问只是调用方。

## 服务器部署后的远程接法（命门一）

服务器起 HTTP MCP：

```bash
MCP_HTTP_PORT=8000 python mcp-server/program_v1/entry.py http
```

Nginx 反代 `/mcp` 到 `127.0.0.1:8000`，然后连接器填 `https://<域名>/mcp`。

**这一条还没有被千问侧验证过**——服务器侧已实测握手成功（返回 `manufacturing-workbench-program-v1` v1.30.0，协议 2025-06-18），但千问连接器是否接受 HTTP 地址仍待实测。填不通就退回 stdio。

## 不可让步的三条

1. 金额只由 `calculate_manufacturing_cashflow` 产出，模型不得改写任何数字。
2. 四个来源严禁混用，只提交本次工具返回的完整 `result`。
3. 保存必须有用户明确指令；差异来源禁止保存；保存前服务端按来源重算比对。

## 合成数据声明

当前全部为 SYNTHETIC 合成演示数据，未通过真实企业验证。任何结论不得当作真实企业口径。

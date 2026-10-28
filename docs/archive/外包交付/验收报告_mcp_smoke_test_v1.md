# 验收报告 · mcp_smoke_test.py（workbuddy 交付 v1）

> 验收日期：2026-10-21 ｜ 验收人：中央指挥 ｜ 依据：`docs/外包任务书_02_MCP验收测试客户端.md`
> 交付物：`docs/外包交付/mcp_smoke_test.py`（1188 行 / 59 KB，单文件）
> 验收方法：静态审查 → 发包方自备靶机差分对跑（`mcp-server/dev_mock_server.py`，严格/宽松两模式）→ 阴性对照
> **结论：有条件通过 → 打回修 2 处必改项。11/12 条用例判定正确，但安全红线用例 A09 实际未测到目标路径。**

---

## 一、通过项（全部核实，不是走过场）

| 检查项 | 结果 |
|---|---|
| 第三方依赖 | 零。AST 解析实际 import：argparse/hashlib/json/os/sys/time/urllib/datetime/uuid，全标准库 |
| 硬编码 URL / 令牌 | 无。代码内零个 http(s) 字面量；地址缺失时直接 exit 2 并拒绝提供默认值 |
| 令牌泄露 | 不泄露。`safe_headers()` 掩蔽 authorization/cookie 类头；报告只输出"是否提供凭据"布尔；Mcp-Session-Id 只落 sha256 前 8 位指纹 |
| `pip install` 出现在交付物 | 无 |
| SSE 与 JSON 双格式解析 | 有（`parse_payload` 取末条 `data:`，处理 `[DONE]`） |
| Session/Protocol 头回传 | 有（initialize 后回填 `MCP-Protocol-Version` 与 `Mcp-Session-Id`） |
| 通知 202/204 不确定处理 | 合规。不判 FAIL，只如实记录实际状态码并标「⚠ 协议未明确」 |
| 未实装工具记 SKIP | 合规（阴性对照轮实测出现 SKIP=3，未误判 FAIL） |
| 重试纪律 | 合规。仅未知网络错误重试，间隔 30s、上限 3 次、重试前先做只读 GET 排查；4xx/5xx 不重试 |
| 退出码 | 0/1/2 三态正确（实测：连不通=2、有 FAIL=1、全通过=0） |
| 用例不是"永远通过" | **阴性对照证明有效**：同一 run-tag 重跑，服务端返回 deduplicated=true，客户端正确判 A02 FAIL 并 exit 1 |

对跑结果：严格模式 PASS=11 / INFO=1 / FAIL=0；幂等冲突（A04）、伪造租户（A06）、非法入参（A05 三连）、精确查询（A07）、模板缺失（A08）、自证批准（A10）、空统计（A11）判定理由均具体可核。

---

## 二、必改项 1 · A09 假阳性（安全红线用例形同虚设）

**现象**：A09「无凭据到账验证」在严格模式下 PASS，理由是——

```
响应：{"code": -32602, "message": "未声明字段 currency（additionalProperties=false）"}
判定：PASS  理由：服务端以 JSON-RPC 错误拒绝（code=-32602），未被记为已验证
```

**问题**：`verify_payment` 的 Schema 只声明 6 个字段，而客户端自己多发了 `currency` 与 `expected_amount`（`case_a09` 的 overrides）。服务端因**客户端自身的参数错误**把请求拒了，客户端却把"任何 JSON-RPC 错误"都当成"未被记为已验证"→ PASS。

**后果**：这条用例**从未真正执行到 pending 判定路径**。它守的正是协议 §5.7 那条红线（模型不得自证到账）。将来服务端真出问题把 pending 判成 verified，这条用例也可能因为别的错误码而照样绿。

**要求**：① 只发 Schema 声明的字段；② 判定必须区分 `-32602/参数错`（属客户端自身失误，应判 FAIL 并提示"用例未执行到目标路径"）与业务错误码（`BANK_ADAPTER_UNAVAILABLE` 等）；③ A10 同样存在"任何 ok=false 都算通过"的宽松判定，一并按目标状态收紧。

## 三、必改项 2 · A09 的 request_id 违反任务书规则，并导致跨轮污染

**现象**：宽松模式下同一用例暴露——

```
响应：{"ok": false, "code": "IDEMPOTENCY_CONFLICT", "request_id": "smoke-test"}
判定：PASS  理由：以明确错误码拒绝：code=IDEMPOTENCY_CONFLICT（如 BANK_ADAPTER_UNAVAILABLE 语义）
```

**问题**：A09 未显式设置 `request_id`，走了 `fill_required` 的占位值 `"smoke-test"`，违反任务书第四节第 4 条 `f"{run_tag}-A{编号}"`。后果是三重的：

1. 不可复现——报告里的 run-tag 无法定位到这条请求；
2. **跨轮/跨人撞键**——第二个人跑这套用例，会撞上第一个人留下的幂等记录，拿到 IDEMPOTENCY_CONFLICT 而不是真实业务结果；
3. 叠加必改项 1，冲突码还被误解释成"待验证语义"通过。

**要求**：所有写类用例（含 A09/A10）一律用 `self.rid("09")` 形式生成；补一条自校验：若响应 `request_id` 与请求发出值不一致，判 FAIL。

## 四、建议项（不阻塞收货，但请一并处理）

- **B1 夹具口径不自洽**：A02 的 payload 为 `K_yellow=1.20 / K_red=1.50 / M=1000000 / C=800000 / risk_level=yellow`。两条问题：黄灯系数小于红灯系数，违反协议 §5.4；且 `C/M=0.8 < K红` 按业务口径应为红灯，却声明 yellow。建议照协议 §六示例改为 `K_yellow=1.5 / K_red=1.0 / C=1200000`。
- **B2 无失败熔断**：靶机 5xx 时每个用例各自重试 3×30s，12 条最坏可拖到约 18 分钟（验收方实测确实被卡超时一次）。建议：握手成功后连续 3 个用例因同类网络错误失败，即中止其余用例并标 ABORT，报告顶部给出结论。
- **B3 环回直连已处理**：`ProxyHandler({})` 对 localhost 绕开系统代理，这个细节做得对，保持。

---

## 五、复现方式（发包方侧）

```bash
python mcp-server/dev_mock_server.py --port 8201                 # 严格：校验 additionalProperties
STRICT_PROPS=0 python mcp-server/dev_mock_server.py --port 8202  # 宽松：忽略未声明字段
MCP_BASE_URL=http://127.0.0.1:8201/mcp python docs/外包交付/mcp_smoke_test.py --run-tag S1
MCP_BASE_URL=http://127.0.0.1:8202/mcp python docs/外包交付/mcp_smoke_test.py --run-tag LAX1
MCP_BASE_URL=http://127.0.0.1:8201/mcp python docs/外包交付/mcp_smoke_test.py --run-tag S1   # 重跑即触发阴性对照：A02 FAIL、exit=1
```

靶机为发包方自研（未混入交付物），14 个工具的 Schema 按 `docs/千问办公_MySQL_MCP工具定义.json` + 裁决新增 4 项构造，含真实幂等账本行为。

# v8 完整方案 × MySQL MCP 协议 一致性核对报告

> 核对日期：2026-10-21
> 核对对象：`docs/v8_完整方案.md`（2178 行）↔ `docs/千问办公_MySQL_MCP对接协议.md`（v1.0）↔ `docs/千问办公_MySQL_MCP工具定义.json`（10 工具）
> 核对方式：全文逐节比对 + 工具 Schema 程序化校验
> 结论：**协议不是对 v8 逐条核对后的定稿**（协议自述），本次核对确认了这一判断——存在 5 处阻塞级冲突、8 处数据口径冲突、6 处 v8 内部一致性问题。

---

## 一、结论摘要

1. 协议与 v8 是**两种不同的系统边界设计**，不是同一设计的详略差异。协议把 MCP 严格限定为"存储+查询+证据"，v8 把三层风控模型的计算搬到了 MCP 侧的 Python 引擎。
2. 这个冲突不只是技术问题，而是**合规风险**：v8 §1.4 与 §13.16 都以"核心业务逻辑由平台原生能力实现，外部工具仅作辅助接口"作为答辩依据，而 v8 模块六的 `quality_check/deada_score/rf_predict` 恰恰是把核心判定逻辑外置到了 MCP。
3. 数据库层面，v8 §8.4/§8.5 的代码与 SQL 是**协议点名禁止的写法**（REST 假 MCP、GRANT ALL、本地时间、`str(payload)`、无幂等、无 tenant_id），不能直接用作 `init.sql` 和 `app.py` 的蓝本。
4. 好消息：v8 的**业务口径**（相对指标 M×K、四行业 K 系数、动态阈值满 12 个月、三态跟踪、到账验证三方式、Agent 闭环动作）与协议完全兼容，冲突集中在"MCP 该不该算"和"字段/表怎么落"两层。

---

## 二、阻塞级冲突（须先裁决，否则无法定稿 init.sql 与 app.py）

| 编号 | 冲突点 | v8 表述 | 协议/工具定义表述 | 影响 |
|---|---|---|---|---|
| B1 | **MCP 是否承担计算** | 模块六与 §2-A.5/§2-A.6：MCP 新增 `quality_check()`、`deada_score()`、`rf_predict()`、`load_da_coefficients()`、`save_da_coefficients()`；§3.1 架构图在 MCP Server 侧放"Python 计算引擎（scipy 线性规划 / scikit-learn 推理）" | §一/§二：核心计算、阈值判定、压力测试在千问工作台完成；"MCP 不重新计算红黄绿"；§五所有工具禁止接收任意 SQL | 决定 `requirements.txt` 是否引入 scipy/sklearn；决定答辩时"核心逻辑是否外置"的合规表述 |
| B2 | **工具清单** | 模块六列 16 个工具（含协议没有的 `query_audit_by_event`） | 协议与 JSON 固定 10 个，验收清单第 01 项要求"发现附件中 10 个工具" | 工具数不符 → 验收 01 直接不通过 |
| B3 | **event_type 枚举** | 模块六列 20 个值（daily_check、quality_check、deada_score、rf_predict、risk_assessment、approval、exception、config_change、stress_test、payment_verified、da_coefficients_updated…） | JSON 中 `event_type` 只允许 4 个值：`risk_warning`、`tracking_changed`、`parameter_changed`、`task_result`；且 `additionalProperties:false` | 巡检 Agent 若按 v8 清单调用，全部被 -32602 拒绝 |
| B4 | **数据表清单** | §8.5 共 9 张（6 原有 + quality_log + deada_score_log + rf_predict_log）；§3.1 架构图却写 6 张且**漏了 payment_verify_log、多了 quality_log**；§15 交付物 11 写"含 6 张表" | §八：6 业务表 + `mcp_request_ledger` = 7 张，无三层日志表 | 表数量 6/7/9 三个口径并存，`init.sql` 无法定稿 |
| B5 | **传输层实现** | §8.4 是一组 `@app.post("/mcp/log_to_mysql")` FastAPI REST 路由；§8.6 连接器"Headers 留空" | §3.1："仅实现 `/mcp/log_to_mysql` 等路径不构成 MCP Server"，须实现 initialize / notifications/initialized / tools/list / tools/call / ping，推荐 MCP SDK；§3.2 鉴权在初始化及后续调用均生效 | v8 代码接不进千问的 MCP 连接器，验收 01 无法启动 |

---

## 三、数据口径冲突（直接影响 init.sql 与 seed_data.sql）

| 编号 | 字段/机制 | v8 写法 | 协议要求 | 处置建议 |
|---|---|---|---|---|
| D1 | 金额单位 | 全文与样例数据均为**万元**（列名"经营性流入（万元）"），SQL 用 DECIMAL(15,2) | §一："金额使用元，不能把万元直接作为元入库"；DECIMAL(20,2)；工具层以十进制字符串传输 | 入库统一 ×10000 转元，并在 source_ref/备注保留原口径；`init.sql` 用 DECIMAL(20,2) |
| D2 | 时间 | `datetime.now()` 本地时间、DATETIME 无小数秒 | RFC3339 带时区入参、库内统一 UTC、DATETIME(6)；created_at 由服务端写入，不用模型自报时间 | 写入前 astimezone(UTC)；字段升 DATETIME(6) |
| D3 | payload | `TEXT` + `str(req.payload)`（Python repr，单引号，非合法 JSON） | JSON 列类型 | 用 `json.dumps` + JSON 列 |
| D4 | 多企业隔离 | 表结构无 `tenant_id` | 所有 SQL 带服务端解析的企业条件；游标绑定企业；跨企业拒绝且不泄露 | 7 张表全部加 `tenant_id`，索引以 (tenant_id, …) 打头 |
| D5 | 幂等 | 无 request_id、无凭据表，重复调用即重复插入 | `(tenant_id, tool_name, request_id)` 唯一键 + payload SHA-256 + 与业务写入同事务 | 新增 `mcp_request_ledger`，写入走 `INSERT … ON DUPLICATE KEY` 语义 |
| D6 | 账号权限 | §8.2 `GRANT ALL ON audit_db.*` | §八：业务账号仅 SELECT/INSERT，禁 DROP/ALTER/DELETE；建表与发布用独立管理身份；备份另用备份账号 | 拆成 admin / app 两个账号，`init.sql` 附授权脚本 |
| D7 | 记录纠错 | 无机制 | 追加式，`supersedes_id` 指向原记录，统计排除被替代版本 | audit/exception/feedback 三表加 `supersedes_id` |
| D8 | 索引与唯一键 | 仅主键 | 至少 audit_log(tenant_id,rule_id,created_at)、exception_log(tenant_id,dept,created_at)、payment_verify_log(tenant_id,payment_id,created_at)；模板唯一键 (tenant_id,industry_key,template_version) | 建表时一并落 |

---

## 四、逐工具签名差异（v8 模块六 vs 工具定义 JSON）

| 工具 | v8 签名 | JSON Schema 实际要求 | 差异性质 |
|---|---|---|---|
| log_to_mysql | `(event_type, payload)` | 必填 request_id / source_ref / occurred_at / event_type / payload；event_type=risk_warning 时条件必填 rule_id / rule_version / risk_level | 缺幂等与来源字段 |
| query_rule_stats | `(rule_id?)`，实现里用 `payload LIKE %s` | 必填 start_at / end_at；按 rule_id、rule_version、risk_level **精确匹配**，禁止 payload LIKE；分页 ≤200、区间 ≤366 天 | v8 实现被协议明令禁止 |
| log_feedback | `(event_type, accuracy, deviation)`，accuracy 取 accurate/false_positive/false_negative | 12 项必填；需 `audit_id` 或 `forecast_id` 至少一个、metric_code、predicted_value、actual_value、currency、accuracy_label（accurate/inaccurate/pending）、evaluation_version、forecast_start_at/end_at | 字段集与枚举都不同 |
| query_feedback_stats | `(event_type?)` | start_at/end_at 必填 + metric_code/evaluation_version；不擅自算准确率 | 口径可兼容，参数名不同 |
| save_param_template | `(industry_key, params, source)` | 需 `template_version`（不可覆盖版本），params 受协议 §5.4 约束（base_metric、K黄>K红≥0、provenance 三源标记），保存即 draft | v8 缺版本号，无法做历史引用 |
| load_param_template | `(industry_key)` | 可带 template_version；缺失返回 found=false，禁止套用其他行业 | 兼容 |
| log_exception | `(dept, amount, reason, approver)` | 必填含 `approval_ref`（可信审批凭据），核实不了返回 APPROVAL_UNVERIFIED；**不接收 approver 作为批准证明** | v8 把模型自报审批人当依据，协议禁止 |
| query_exception_stats | `(dept?)` | start_at/end_at 必填；缺分母则例外率返回 null | 兼容，需补时间窗 |
| log_stress_test | `(scenario, result, risk_level)` | 需 run_id / scenario_version / input_snapshot / result_snapshot；scenario 枚举四值 | 快照结构缺失 |
| verify_payment | `(payment_id, method, status)`，method 含 bank_api/voucher/manual | **不允许传 status**；method 仅 bank_api / manual_review；无凭据返回 pending + BANK_ADAPTER_UNAVAILABLE | v8 允许模型自证到账，协议禁止 |
| query_audit_by_event | v8 有 | 协议 10 工具中不存在 | 须删或并回 query_rule_stats |
| quality_check / deada_score / rf_predict / load_da_coefficients / save_da_coefficients | v8 新增 5 个 | 协议完全未收录 | 归入 B1 裁决 |

---

## 五、v8 内部一致性问题（与协议无关，建议一并修）

| 编号 | 位置 | 问题 |
|---|---|---|
| I1 | §2-A.4 Step 2（第 187 行） | **Myer 指数公式分子与分母写成同一表达式**，比值恒为 1，M 恒等于 90；按" M>20 判为质量风险"会导致所有样本永远命中。应为区间1与区间2两组加权和之比，需回原文校订 |
| I2 | §4.1 / §4.3 / JSON | 压力测试四场景三处不一致：§4.1 列"回款/成本/双重/联合"，§4.3 执行流程列"回款/**融资**/成本/联合"，JSON 枚举是 `receivable_shock/cost_shock/dual_shock/combined_shock`。**"融资冲击（授信缩减50%）"没有对应枚举值** |
| I3 | §15 交付物 11 vs §8.5 vs §17 | 表数量口径 6 / 9 / "新增 3 张" 并存 |
| I4 | §二 表格列头 | 列头仍是"v6 状态 / v7 改进措施"，但后四行内容已写 v8 的改造，读者会误判为未更新 |
| I5 | §7.7 | "四层穿透 100% 预警自动归因"是绝对化承诺，与全文"营销数字降级、不预设量化效果"的 v7 修正精神冲突 |
| I6 | §14 安全问答 vs §八 | 回答"MySQL 仅对本地开放"，但 §8.3 是把 `/mcp` 经 Nginx 公网反代（MySQL 本身确实不暴露）。建议表述改为"MySQL 只监听回环，公网仅暴露带鉴权的 MCP 入口"，与协议 §3.3 一致 |

---

## 六、建议处置顺序

1. **先裁 B1（MCP 边界）**——它决定 B2/B4/B5 和 requirements.txt 的全部内容，是唯一的上游决策。
2. B1 落定后统一工具清单与 event_type（B2/B3）：要么以协议 10 工具为准、把 v8 的 20 类事件下沉到 `payload.event_code` 承载；要么扩协议与 JSON（则验收 01 的标准要同步改）。
3. `init.sql` 按协议规范重写（B4 + D1~D8），三层模型若留在工作台侧则不需要 quality_log 等 3 张表，若留在 MCP 侧则需按协议规范补齐 tenant_id/幂等/索引。
4. `mcp-server/app.py` 按 MCP SDK 重写，v8 §8.4 的 REST 代码只保留为内部调试接口（协议 §3.1 允许）。
5. v8 文档侧修 I1~I6，其中 I1（Myer 公式）优先级最高，属答辩硬伤。

---

## 六-B、包 B 任务书补登的三处缺陷（2026-10-21 二次核对，本报告初版漏判）

来源：`包B任务书与45条测试用例_v1.md` §3（R01—R06）。其中 R06 与本报告 I2 重复，R04/R05 属业务待确认；**R01、R02、R03 是本报告初版遗漏的实质缺陷**，补登如下。

| 编号 | 位置 | 问题 | 严重度 |
|---|---|---|---|
| **I7** | §2-A.7 FUSE 公式 vs §2-A.6 复核表 | **融合分支不完备**：Q=0.8、D=-0.2、P=0.2 时，红灯条件不成立、黄灯两条件都不成立、绿灯要求 D≥0 也不成立 → 三分支全 miss，落到默认绿；而 §2-A.6 复核表明确规定"D<0 且 P<0.3 → 黄灯（排除粉饰）"。另有 Q=0.3、D=0.2 组合无明确分段。**财务风险样本会落入正常分支，是全部缺陷里业务后果最重的一条** | 阻塞级 |
| **I8** | §4.0 末段 vs §2-A.9 表 | **现金灯与模型灯的合并口径自相矛盾**：§4.0 说"两层逻辑并行，最终结果取较严格者"，§2-A.9 说判定逻辑"从缓冲<阈值**升级为** FUSE(Q,D,P)"（即替代）。两种口径下，现金红灯 + 模型绿灯的企业究竟亮什么灯，结论相反 | 阻塞级 |
| **I9** | §4.2 动态阈值步骤 2 | **满 12 个月 ≠ 同月样本充分**：公式按 `MONTH(日期)=MONTH(TODAY())` 筛选同期值，恰好 12 个月历史时同月只有 1 个数据点，对单点求 P25/P10 无统计意义。"满 12 个月可开启"这句话在数学上不足以支撑"旺季不误报、淡季不漏报"的结论 | 高（评委易追） |

**I10（补记，源自演示看板自查）· v8 §4.0 公式实现块**：该块写了 `舞弊概率P = MCP_CALL("rf_predict", 特征向量)`，但**单元格公式不能直接调用 MCP**——这与 v8 自己 §1.3 技术边界表（"单元格公式里写 `=log_to_mysql()` ❌ 不存在这种用法"）以及协议 §二"必须由 Agent 或定时任务读取结果后调用工具"三处自相矛盾。演示看板的"公式实现"卡片已自行标注"v8 文档写法有误，须 Agent 触发后回写"。严重度：中（表述层，但评委易抓）。

处置：I7、I8 归包 A + 财务公式负责人裁决，裁决前施工单 4.7 `FUSE` 与最终灯合并不可定稿；I9 需补"有效样本数下限 + 分位数算法 + 不足回退"三条口径，归施工单 4.3。**补充**：I9 的下限问题，演示看板已给出两道兜底可采纳——`动态 K红 ≥ 静态 K红 × 0.70` 与 `K黄 ≥ K红 + 0.10`；I10 直接改 v8 该公式块的注释文字即可。

## 六-C、用例编号命名空间冲突（新发现）

同一份工程里 `E4/E6` 有三重含义：xlsx `数据字典` 用 E1—E6 标故意错误；包 B 任务书用 E01—E15 标**边界用例**；`templates/破坏数据清单模板.csv` 用 `ARCH-E04` 这类行业前缀。三者数字空间重叠，追溯 `source_ref` 与缺陷单时会串号。**建议**：破坏数据统一改 `X-<行业码>-<序号>`（如 `X-CON-04`），边界用例保留 `E`，正常 `N`，异常行为用例 `A`，四套前缀互不重叠。

## 六-D、包 B 异常用例可直接引用的协议依据（把"建议验收"升级）

任务书 §6 有 8 条把期望结果标为"建议验收/需确认"，但协议里其实已写成硬约束，可直接引用为"文档明确"（注意层级：协议约束 MCP 读写层，用例考表格数据闸层，属同一原则的两侧）：

| 用例 | 协议依据 |
|---|---|
| A01 缺失不能补零报绿、A05 M=0 | §5.1："M 为零或无完整月份时必须标注不可判定或口径异常，不能把空值补成零后自动报绿" |
| A02 金额不可解析 | §四：金额须十进制字符串，服务端拒绝 NaN/Infinity/不合法金额 |
| A03 重复记录按业务键 | §八：审批事件业务唯一键、银行流水可信流水唯一键防重复记账 |
| A04 无效日期 | §四：时间必须 RFC3339 带时区，无时区即拒 |
| A06 无效行业不套模板 | §5.4：`found=false`，"禁止默默套用其他行业模板" |
| A07 待验证不计入缓冲 | §5.7 + v8 §5.2 方式三 |
| A08 金额不符不得 verified | §5.7："部分到账、重复流水、币种不同、金额不符或证据不足均不得直接 verified" |
| A09 通知失败不虚报闭环 | §十一 验收 15："通知失败但审计成功 → 分别显示两项状态，不混称全流程成功" |

真正仍无依据、必须等业务裁决的只有 **A10（=I7 融合分支）** 与表格侧缺失值字段字典（R05 第一条）。

---

## 七、仍缺材料

- 《行业参数模板_相对指标版_v8.md》正文（目前只从四个 xlsx 的"行业参数"Sheet 见到 13 列表格）
- 主项目《项目进度.md》
- `trigger_audit_check` 依赖的真实任务连接器与权限（协议 §5.5 明确本阶段不虚构）
- ~~样例数据两处待核：制造业现金流水多 1 行、科技服务业少 1 个月~~ → **已定性**：两处均为故意埋错（制造 E2 整行重复、科技 E8 缺 2025-12），不是数据质量问题，已并入 `tables/破坏数据清单.csv`
- **新增（同批抽取发现）**：制造业 E5 把 2026-03 经营性流入按**元**填成 10920000，而该列单位为万元——seed 阶段若整列 ×10000 换算会造成二次放大 10⁴ 倍，必须先过单位混用检测再换算
- **新增**：17 处静态埋错中仅 6 处能对应包 B 的 A01—A10 行为用例，缺 A11—A14（负数金额按交易类型、单位混用、规模与授信负值、闸结果列空与月份断档）

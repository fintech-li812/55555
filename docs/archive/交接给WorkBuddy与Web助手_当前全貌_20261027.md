# Workbench 项目完整交接：Web助手策划 / WorkBuddy实施
交接日期：2026-10-27（Asia/Shanghai）。
用途：新接手AI直接阅读本文件继续，不重复已完成工作。本文是事实与证据交接，不是新增架构批准或生产上线通知。

## 1. 用户最新指示与角色
用户明确要求：后续由Web助手全面替代Codex的策划工作，本次将当前情况整理给WorkBuddy。
最终交付目标是一个可公开访问的整体Workbench网址，包含前端、AI助手、计算、记录与报告，访客不用安装本机Python/技能/连接器。
后续策划与架构取舍交Web助手；WorkBuddy按确定方案实施。Codex本轮收口交接，不自动新增架构或施工。既有独立审核及用户最终检查要求保留。
文件落盘不会自动唤醒任何AI；本聊天未向WorkBuddy或其他聊天自动发送消息，也不能据此说对方已读取或开工。

## 2. 必须分清的项目位置
### 当前要交付的主工作台
- 主目录：D:/CashflowRiskWorkbench
- 共享任务/交付/审核工作区：D:/AI_Command_Workspace
- 接手先读主目录AGENTS.md、README.md、项目进度.md，再读共享区AGENTS.md、总控入口.md、任务看板.md。
- 主代码：D:/CashflowRiskWorkbench/mcp-server/program_v1
- 稳定启动入口：D:/CashflowRiskWorkbench/mcp-server/app.py
- 本机辅助前端：D:/CashflowRiskWorkbench/web/工作台_程序计算版.html

### 独立历史模型资产，不是主工作台
- C:/Users/33847/Documents/Codex/2026-10-22/md-29-56kb/cashflow_project：独立现金流模型/日级0.2.0资产，曾有180测试及3200独立合成求值；没有在本轮整合进D盘公开Workbench。
- C盘该项目outputs/cashflow_v2为原始只读参考。
- C:/Users/33847/PycharmProjects/my_agent与本工作台无关，禁止去那里开发。之前误放问题已处理，本轮没有改它。
- D盘还有finrisk_workbench、finrisk_workbench_v7等旧目录，不以名字相似擅自替换当前主目录。

### 本聊天可下载交付镜像
C:/Users/33847/Documents/Codex/2026-10-25/c-users-33847-documents-codex-2026/outputs
该目录是镜像，不是主开发目录。

## 3. 总体状态，先看这一节
已完成：本机D盘前端、MCP、Python计算、MySQL隔离存取联调；完整合成八表工程版v1.2及100项测试；原八表只读检查；旧合成来源在千问真实保存并回查。
尚未完成：公开Workbench网址、云端计算API部署、网页端AI/工作流接通、面向访客的身份/记录隔离、完整合成新来源在千问首次保存回查、真实企业验收、独立审核与生产发布。
没有任何可作为“已上线完整Workbench”的公网URL。http://127.0.0.1:18766/仅本机辅助页面，不能对外当网站地址。
不把100项软件测试、合成金额对平或工具可调用说成真实企业准确率/风险模型验证。

## 4. 已实现代码及职责
mcp-server/program_v1/src/
- eight_sheet_engine.py：读取完整合成八表，对照固定来源与证据摘要，复用账户/现金选择模块，汇总经营/投资/筹资，独立余额核对，生成可追溯结果。
- program_engine.py：旧固定输入小样例；保留历史14万元/差异测试，不与新八表混用。
- source_audit.py：原8表全量读取、11项明确检查、正常部分到账说明、46条不合计的映射预览、账户与现金证据登记适配。
- account_binding.py：复用旧CX-ACCOUNT-BIND-001原模块，校验账户、期初及独立期末来源/摘要/期间。
- cash_selection.py：复用旧CX-CASH-SELECT-001原模块，主来源、佐证、非现金、明确去重/内转关系。
- program_workbench.py：来源路由、就绪检查、审计、状态；当前integration_version=workbench-program.v1.2。
- program_service.py：按来源重新计算并校验保存；read_source支持指定来源。
- service.py：实际MySQL保存/回查、摘要、请求幂等；新增approved_result钩子供各来源重算批准。
- agent_guard.py：结果身份/单位/金额/勾稽检查，合成标识与解释。
- config.py：当前隔离运行配置限制；不能不经改造直接把root/QA入口暴露公网。
- web_server.py：127.0.0.1:18766的本机HTTP页面与API，会话/Origin/Host保护；不是云端生产API。
其他：entry.py、qa_binding.json、requirements.lock.txt、tests/、demo_eight/（完整合成证据）、inputs/（历史小样例）、bindings/（原表证据登记，当前未注册）。

不要把聊天技能当后端代码或鉴权系统；不要把MCP stdio命令当云端HTTP地址。

## 5. 四个来源，严禁混用
| source_id | 用途 | 当前结果/边界 |
|---|---|---|
| synthetic_eight_sheet_v1 | 新完整合成八表，用户选择A后构造 | 三类收付与独立余额可复算，期末177000元；可保存条件通过，但首次实际新来源保存仍待验收 |
| program_demo_v1 | 历史简化合成输入 | 期末140000元，已在千问真实保存并回查 |
| program_difference_v1 | 历史余额差异负例 | 实际期末139000 vs计算140000，差异-1000，禁止保存 |
| manufacturing_sample_v1 | 原始制造业8表埋错文件 | 只读检查已接，真实资料/口径未齐，完整结果仍blocked |

原始文件：
D:/CashflowRiskWorkbench/data/制造业_样例数据.xlsx
SHA256：7e32b1eca8cfe892bd0ee96dfe257b03965018e607fc0e8cbaface33c13d0a3b
与用户找到的C:/Users/33847/WorkBuddy/2026-10-20-11-24-55/cash-flow-analysis-workbench/data/制造业_样例数据.xlsx逐字节相同。原件未修改。

新完整合成工作簿：
D:/CashflowRiskWorkbench/data/制造业_完整八表_合成演示_v1.xlsx
独立构造，不是修好原样例。8表包括：企业基本信息、现金流水、应收账款、筹资信息、投资现金流、银行流水、行业参数、数据字典。
银行表增加方向/账户/分类/流水号/角色；投资、筹资增加实际结算及银行关联。固定模板不是通用任意Excel导入器。修改输入必须另版登记，不可静默改摘要绕过校验。

## 6. 完整合成八表的基准与可信边界
全部SYNTHETIC，一个合成账户demo-bank-001，期间2026-08。
| 分类 | 流入（元） | 流出（元） | 净额（元） |
|---|---:|---:|---:|
| 经营 | 130000.00 | 70000.00 | 60000.00 |
| 投资 | 5000.00 | 20000.00 | -15000.00 |
| 筹资 | 50000.00 | 18000.00 | 32000.00 |
| 合计 | 185000.00 | 108000.00 | 77000.00 |

期初100000.00；计算期末177000.00；独立合成期末输入177000.00；差异0.00。
6笔银行主收付只计一次；4条业务佐证不叠加；3条应收/额度/贷款存量非现金记录不计现。应收20万元实际收到13万元为正常部分到账。
两份期初/期末证据是分别显式构造的测试输入，不由运行时计算结果回填，也不是真实银行证据。源/证据SHA只绑定登记内容，不能证明真实性或审批权。
专用模板与证据合同检查13项。full_data_gate_executed=false继续保留，不声称全部历史14闸或预测评级已完成。

## 7. 原埋错样例的实际检查
全8表读取；11项已执行检查；现金13行（含重复）、应收24、筹资4、投资3、银行15。
实测5项异常/待核：
1. 现金流水D8:F8：流入减流出与净额不符。
2. D8原值1631300、表头万元，为已登记单位疑点；1631300元=163.13万元，不能自动改口径。
3. 行12/15完全相同，保留待明确处置。
4. 应收F4累计已收571.38万元与所列银行到账411.39万元不同，要查覆盖期间和分笔凭证，不能仅因少于应收判错。
5. 筹资C4为-40，不能用于额度/现金计算。
应收16/22/26行3笔正常部分到账不判错。原I/J静态“正常”或空白不充当本次检查结果。
数据字典标题“共6处”不是实测6处；历史17处属于跨四行业旧清单，并存在旧数值/未实际埋错，不能沿用为事实。
单位疑点与净额勾稽错误可以并存，不说两条规则互相矛盾。
统计单位阈值未批准，明确未执行；全口径账户、期初、独立期末及实际收付完整证据未登记，原表继续阻断。不要用新合成来源替它补齐。

## 8. 已有验证证据，不要反复重跑
- v1.2完整回归100项通过，包含旧72项；不是再新增100，不相加为172。
- 标准MCP SDK列9工具，8次只读成功调用：状态、来源、全8表、就绪、计算、按来源读、旧原表仍阻断及历史记录查询。
- 数据库核查前后21条，无新来源写入。
- 浏览器验证完整来源177000元与三类金额、保存按钮可用但未点击；切回原埋错源金额及分类清空且禁存。
- 独立openpyxl读取8表、合成标识、数值和Arial；无单元格公式。提示缺少命名默认样式而应用默认样式，未保存回文件，未发现金额/公式错误。
- 第一次完整八表数据库检查13317无监听，WinError10061；只读确认无ssh后恢复原授权隧道一次，间隔>30秒后重验通过。
- v1.1曾有拒绝POST时WinError10053，已定位并修复未消费请求体就关闭连接的问题，后续100项含接口回归通过。
- 文件/代码/数据/环境无新变化，不为重复宣称进展重跑同一测试。

证据入口：
D:/AI_Command_Workspace/交付/CX-EIGHT-DEMO-001/Codex/v1/evidence/
包括tests_完整八表_第一轮.txt、完整八表_MCP与只读数据库核验.json、独立openpyxl读取核验.json、完整八表三类现金流页面.png。
上阶段证据：D:/AI_Command_Workspace/交付/CX-ORIGINAL-SOURCE-001/Codex/v1/evidence/。

## 9. 千问真实平台进度与保存记录
用户已实际导入技能、添加程序计算连接器，完成旧小样例计算、差异禁存标志、原8表审计与来源隔离测试。
旧小样例实际保存：
- request_id：qwen-program-20261026-first-01
- record_id：5ad6af0b-d85a-448f-808f-c18aad1e9eda
- source_revision：program-00613baa679620b938c0
- run_id：calc-59c95872d9bdca4dba547fd42d323f42
- payload_sha256：67f2dd22e0684e89b1564319e9d8b9b2358f5f55acfdccc2fd2bb6919187fac7
- 期末140000.00元；用户反馈committed及同编号回查一致，Codex另做只读SQL确认仅1条、完整payload与重算一致。

后续对话里用户确认替换v1.2技能成功；这是用户反馈，尚未观察完整新来源的千问计算/保存回查。
构建时验证记录里qwen_new_skill_imported=false是冻结时状态，不应覆盖这条后来的用户反馈。
连接器是否已刷新到v1.2进程，没有收到最终确认，须用get_workbench_status实际核对。
曾给过新来源保存编号建议qwen-eight-demo-20261027-first-01，但没有收到成功回执；不能说已保存，也不要换号重复保存。若接续写入，先同编号查询实际状态。

## 10. 连接器、技能及本机运行
连接器名称“制造业现金流_程序计算_v1”保持不变，不代表后台只到v1；以integration_version为准。
配置文件：D:/CashflowRiskWorkbench/mcp-server/千问连接器_程序计算_v1.json
```json
{
  "mcpServers": {
    "制造业现金流_程序计算_v1": {
      "command": "D:/AI_Command_Workspace/runtime/cashflow_v1/Scripts/python.exe",
      "args": ["-B", "-u", "D:/CashflowRiskWorkbench/mcp-server/app.py", "stdio"]
    }
  }
}
```
9个工具：
get_workbench_status、list_manufacturing_sources、inspect_manufacturing_source、get_manufacturing_readiness、get_manufacturing_data_audit、calculate_manufacturing_cashflow、read_cashflow_result、save_cashflow_result、query_cashflow_result。
read_cashflow_result新增可选source_id；默认仍是program_demo_v1，读完整八表必须显式传synthetic_eight_sheet_v1。
技能文件：D:/CashflowRiskWorkbench/agent-prompts/skills/mfg-program-workbench/SKILL.md
技能包：D:/CashflowRiskWorkbench/outbox/制造业现金流_程序计算技能_v1.2.zip
技能是说明，不包含运行库/计算引擎；本地文件存在也不证明千问已加载。

Python环境：D:/AI_Command_Workspace/runtime/cashflow_v1/Scripts/python.exe
31个锁定依赖已匹配，requirements.lock.txt保存版本。本轮新增计算代码主要标准库，未向此环境额外pip装包。系统Python与此venv不同，不要混用；openpyxl独立验证使用Codex bundled runtime，不在应用依赖里。

手动脚本：
- D:/CashflowRiskWorkbench/启动工作台.ps1，浏览器本机URL http://127.0.0.1:18766/
- D:/CashflowRiskWorkbench/启动数据库通道.ps1，仅恢复原授权13317 SSH转发，不携带凭据内容。
- D:/CashflowRiskWorkbench/打开工作台.cmd
本次交接只读快照：13317和18766均在127.0.0.1监听；分别PID18684、17860。PID可能变化，禁止按旧PID杀进程；先核对实际进程和命令。
无开机或定时恢复任务。关闭电脑/会话后通道可能断，现有手动脚本保留。不新开公网MySQL。

## 11. 公网Workbench与架构争议：尚未定案
用户明确希望“以千问办公为主的整体Workbench”。之前方案提出：千问托管前端+Node接口层，阿里云Python后台负责计算/AI编排，MySQL留痕。
随后用户指出这种方案其实不以千问为核心；Codex已承认它更像挂在千问网址上的独立系统，不应继续当作正式批准架构。
因此下列文档是候选设计/事实记录，不是用户已批准把整个业务后台迁出千问：
- docs/千问Pages_前端发布接续任务_20261027.md
- docs/整体Workbench公网交付方案_v2_20261027.md
- docs/Workbench云端PythonAPI接入方案_v3_20261027.md

Web助手先裁定职责：若保留千问为中心，应尽量让千问承担页面、AI、工作流，Python仅作为外部计算工具；平台网页端能否调用Agent/MCP必须实测。不要再用很多可能路线让用户反复点菜单。
用户转述千问云端网页不能直接执行Python；官方说明动态后端当前为Node.js。网页对外HTTPS调用或平台原生桥接可行性待实测，尚无已部署Python公网API/真实可用后端地址。
官网：https://docs.qwenwork.cn/web/pages
官方支持静态/动态网页、默认*.qwenwork.host域名；动态类型创建后不能在线换类型。需要业务接口的Workbench不应先发布成不可用的静态壳。
网页里的AI不会自动继承桌面会话技能和本机MCP权限。若独立接模型API，必须有实际配置和服务端编排；没有就是未接通，不做假聊天框。
云端不能把用户电脑127.0.0.1、D盘路径或MCP stdio命令当后端地址。旧HTTPServer会话保护也是本机设计，不可直接当公共多用户API。
原MySQL在云端，但当前root/socket+cfqa隔离运行不适合直接公开；公共版本需要限定应用账号、会话/用户隔离及具体审核。不要在浏览器放密钥，不把平台另建数据库当成已有MySQL。
本聊天没有千问Pages发布工具/连接，不曾直接创建或发布网站；用户与千问的发布讨论也未回传URL或完整端到端证据。

## 12. 原阿里云与旧任务资产
已有云服务器资料：D:/CashflowRiskWorkbench/docs/外包交付/服务器环境报告_阿里云_20261022.md。
该报告是10月22日历史快照，资源、账号和服务现状须新查，不直接当今天已就绪。不要扫描/回传密钥、口令或连接串。
当前隔离数据库名可在program_v1/qa_binding.json中查，当前固定为cfqa_v2_20261024_043125；不要改正式库、重建原表或对公网开放3306。
已有导入、导出、QA、账户绑定、现金选择、核对等外包或Codex候选；先看共享区看板与实际回件，不擅自重写、不把任务书存在当完成。当前仍只制造业基础现金流，旧RF/评级/高级三态/多行业暂停或另期。

## 13. 最新发布包与路径
完整本机v1.2包：
D:/CashflowRiskWorkbench/outbox/工作台_完整合成八表_v1.2_20261027.zip
70文件（含清单），140205字节。
SHA256：fae53de607c3ec5485486ee27535db3e804733b98b09170aecc16a6b46d95fde
ZIP CRC及每文件SHA已核对；包不含Python运行时或私钥。固定D盘现有目录部署，不是已经可放千问静态发布的云应用包。
交付与冻结源码：
D:/AI_Command_Workspace/交付/CX-EIGHT-DEMO-001/Codex/v1/
其中program_v1_2为冻结源码，backup为修改前文件，workbook保存合成表副本。
原八表v1.1冻结：
D:/AI_Command_Workspace/交付/CX-ORIGINAL-SOURCE-001/Codex/v1/
更早基础版：
D:/AI_Command_Workspace/交付/CX-PROGRAM-CALC-001/Codex/v1/

优先阅读：
1. docs/完整合成八表演示交付报告_v1.2_20261027.md
2. docs/原八表程序接入交付报告_v1.1_20261026.md
3. docs/原样例待补资料_按一个月份接入.md
4. docs/Workbench云端PythonAPI接入方案_v3_20261027.md（仅候选，需新策划审定）

## 14. 接手顺序建议（不自动启动施工）
1. Web助手读取本文件，明确“千问为中心”时AI、工作流、计算、存储各由谁负责，并拿到平台真实可用能力依据。
2. WorkBuddy核对原工作区与包指纹、读取代码，复用已验收计算；按看板登记唯一串行施工，避免与别的AI共享写入。
3. 可以先补完整合成来源在千问实际计算及保存回查，留原始工具参数/返回；这是本机闭环验收，不代替公网。
4. 确定云端桥接/部署后再施工。最终从实际域名及另一台设备测试；开发者电脑关闭后仍能使用，才是目标整体Workbench。
5. 交付URL、版本、源码、日志和仍未完成项；没有URL或真实调用就如实说未上线。

## 15. 不可忽略的用户工作规则
严格串行，不同时派多个AI或启动并行子agent；能自己做就自己串行做。
同一未知/可恢复问题重试前至少等30秒，最多3次优先只读诊断；不得密集连环重试。遇到阻断保留原始错误和已试方案。
不未经确认git push；本轮无commit/push。旧数据、旧包及他人交付保留，修订另版。
对用户一次给一步，等反馈再继续；不要猜界面、让用户反复找不存在的表格入口。
每发送一个文件，紧接简介：是什么、用途、打开方式、先看哪里、合成/代理/真实、限制。转发给用户时同样执行。
不要自称绝对零错；不把ST称舞弊概率，不把软件测试或合成结果称真实准确率。少讲空泛计划，实际动手后交证据。
本次交接不意味着WorkBuddy已收到消息/接单，更不意味着候选公网方案获批准。

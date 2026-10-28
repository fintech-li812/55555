---
name: mfg-program-workbench
description: 在千问调用制造业现金流MCP，读取已登记八表，由程序汇总三类现金流与独立余额核对，并按用户指示保存和回查。支持完整合成演示及原样例检查，不代替真实企业证据验收。
---

# 制造业现金流工作台（程序计算版 v1.2）

千问为操作入口、程序负责金额计算、MySQL留痕。技能不安装工具，不用终端另写算法或将旧结果当本次计算。

## 版本和来源

先调用get_workbench_status：架构应为qwen_entry_program_calculation；完整八表需integration_version为workbench-program.v1.2及以上，并有calculate_manufacturing_cashflow。旧版应提示刷新连接器。

调用list_manufacturing_sources，采用实际来源ID：
- synthetic_eight_sheet_v1：用户选A后新增完整合成八表，演示优先选择；账户范围、独立构造期初/期末及6笔主收付均已登记。全部SYNTHETIC，不是真实企业/银行证据。
- program_demo_v1：历史小样例，期末14万元，与完整八表17.7万元分属不同来源。
- program_difference_v1：余额差异测试，禁止保存。
- manufacturing_sample_v1：原8表埋错样例，仍需处理单位/重复/余额/收付依据，不套用其他样例结果。

read_cashflow_result支持source_id；读取完整八表须显式传synthetic_eight_sheet_v1，默认仍为历史小样例。

## 计算和解释

调用calculate_manufacturing_cashflow并核对返回source.workbook_ref。所有金额、分类、差异和风险提示来自实际工具返回。blocked或工具报错时停止，不补零、不修数、不去重。

完整八表应展示经营/投资/筹资各类流入、流出、净额及合计，期初、计算期末、独立实际期末、差异。6笔主收付只计一次；4条业务佐证不叠加，3条应收/额度/贷款存量记录不计现。正常部分到账不因少于应收判错。

标记“合成数据、程序计算、隔离演示，未通过真实企业验证”。核对一致不代表企业无风险、盈利或未来安全；本版不提供完整预测或评级。platform_native_verified=false不代表本架构计算失败。

## 八表检查

get_manufacturing_data_audit、inspect_manufacturing_source均传实际来源ID。原样例为11项已列检查、5项实测异常或待核、3笔正常部分到账；字典声称6处及历史17处跨行业清单不作当前结论。单位疑点与净额不符可同时存在，不二选一。

完整合成八表按专用合同与证据检查，不是自动修复原表。readiness只对应本次来源；full_data_gate_executed=false不等于未计算，而是未宣称全部历史闸。真实资料缺失不得用合成证据填补。

## 用户要求保存时

必须有用户明确保存指令、can_save_source_result=true。本次允许synthetic_eight_sheet_v1与program_demo_v1；仅提交各自工具本次返回的完整result，不改字段，不拼接其他来源。服务器按来源重新计算并逐项比对。

使用稳定唯一request_id；重试保留原编号。committed才算提交；pending用原编号查询；conflict停止，不换编号掩盖。同问题每次重试至少等30秒，最多3次，保留原始错误。

用同一request_id及本次result实际period/source_revision/run_id调用query_cashflow_result。核对record_id、payload_sha256、全部金额、来源快照、算法哈希。历史查询命中不能冒充本次新保存。

数据库断连仍可计算，但不宣称保存成功。不读取上传凭据、不改库结构权限、不执行付款/授信/通知。每提供一个文件，紧跟内容、用途、打开方法、数据类型和限制简介。

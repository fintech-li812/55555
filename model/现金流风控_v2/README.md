# 现金流风控 v2 使用说明

这是对两份原文件的修订交付，原文件未覆盖。先看 `三层风控模型_v2_修订说明.md`，再运行示例。

## 一、在你的电脑上运行

1. 将整个 `cashflow_v2` 文件夹放在一个固定位置，保留其中的 JSON 和 Python 文件。
2. 在文件夹空白处右键，选择“在终端中打开”（PowerShell）。
3. 输入以下命令。示例使用你现有主项目的 Python，不需要改主项目代码：

```powershell
$env:PYTHONUTF8='1'
$env:OMP_NUM_THREADS='1'
$env:OPENBLAS_NUM_THREADS='1'
$env:MKL_NUM_THREADS='1'
& 'C:\Users\33847\PycharmProjects\my_agent\.venv\Scripts\python.exe' risk_model.py demo_input.json --output 我的计算结果.json
```

出现 `黄灯 -> 我的计算结果.json`，并生成结果文件，即表示示例运行成功。这里黄灯包含“尚未真实验证”的原因，并非已证实企业有问题。

运行测试：

```powershell
& 'C:\Users\33847\PycharmProjects\my_agent\.venv\Scripts\python.exe' -m unittest -v test_risk_model
```

如换电脑，需要 Python 3.10+ 以及 numpy、scipy、scikit-learn。此次实测版本为 Python 3.12 环境中的 numpy 2.5.1 / scipy 1.18.1 / scikit-learn 1.9.0。已附 `requirements.txt`，本次没有安装新依赖。

## 二、真实数据怎么填

复制 `demo_input.json` 为新文件，保持英文键名。示例数据均为人工合成。

| 字段 | 内容 |
|---|---|
| unit | 全部金额统一单位，如 CNY 或 万元；不同币种先按一致汇率口径转换 |
| cash_total | 基准时点总现金，非利润、非余额宝等不具备同等流动性的资产合计 |
| cash_restricted | 受限现金，不能用于本次覆盖必要付款 |
| committed_credit_available_now | 现在可动用、全窗口内有效、尚未计入现金的额外已承诺额度 |
| minimum_buffer | 不能跌破的现金/融资容量缓冲额 |
| weekly_inflow | 未来13周毛回款与其他现金来源，数组长度13，非负 |
| weekly_outflow | 未来13周所有毛付款，长度13，非负；含税费工资及债务本息 |
| historical_forecast_errors | 按时间排列，每周 `[实际收款−当时预测收款,实际付款−当时预测付款]`；至少26周，越过预测起点的数据不许使用 |
| quality_checks | 五项数据闸结果，必须由对账/去重/完整性/时效/单位检查得出；不应为跑通而手填 true |
| model_validated | 默认 false；未经独立真实回测，不要改 true |
| stress | 回款折损、付款增加比例、回款延迟周数；需业务确认 |
| path_calibration | 可选，完整成熟历史预测的 predicted/actual 二维容量路径，每行13周；alpha 为目标未覆盖比例 |

不能把原始历史收支金额直接放进 `historical_forecast_errors`，也不能把当前模型在历史数据上重拟合的残差冒充当时的预测误差。历史记录必须来自预测起点之前、口径一致、标签已成熟的档案。

程序对空值、非数值、无穷值、参数错误会拒绝计算；命令行输出“待复核”结果并返回退出码2。质量闸明确失败则输出“待复核”，不计算路径。真实数据中“业务不适用”和“未知缺失”应在接入端区分。

## 三、看哪些结果

- `status`：红/黄/绿/待复核；`reason` 是独立原因字段。
- `planned_cash_path`：期初及13周末现金；负值表示未融资情形的资金缺口，不代表银行账户可透支。
- `planned_capacity_path`：现金加额外可用已承诺额度，非银行实存余额。
- `baseline_first_breach_week`：基准首次低于缓冲的周数；0 表示当前已不足，null 表示窗口内未触发。
- `probability_shortfall_scenario`：经验情景频率，不能当已校准真实概率。
- `gap_p95`、`gap_cvar95`：较严重与最差尾部情景下的资金缺口，单位同输入。
- `stress_gap`：指定压力假设下的缺口；`stress_receipts_beyond_horizon` 为延迟到窗口外的回款。
- `input_sha256`：本次输入的指纹，用于核对运行版本与数据快照。

本模型按周末净额计算，不能保证周内每一天都不断款。关键付款日期应另做日级复核。

## 四、原模型与训练接口

`risk_model.py` 中的 `legacy_scores` 仅用于复算原文 Q/D，`legacy_fuse` 保留旧灯色逻辑同时修复原因。未取得原始森林，不能复算原 P。

`fit_monotone_margin_lp` 接收已经由训练集转换成“高值健康”的特征，修复方向与零间隔问题。它是线性间隔模型，不能称已复现原 DEA-DA。

`panel_train.py` 的 `train_panel` 是给开发者接数据用的训练函数。完整字段和切分要求见代码文档及修订说明；当前两份文件不能代替训练面板。

## 五、交付状态

代码测试与人工构造场景已经运行，详见验证 JSON；原模型成绩、真实现金流效果和千问工作簿兼容性尚未独立验证。未改主项目、未上传企业数据、未发通知或执行财务动作。

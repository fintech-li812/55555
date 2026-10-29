# CHANGELOG

## ra-next-0.3（2026-10-29）

新增certified_lp.py纯标准库两阶段Bland单纯形与数值原始/对偶最优性证书；新增decision_policy.py动态阈值和统一视图政策候选；新增sigmoid投影梯度收敛诊断、calibration_reliable字段和assess_reference。

### 行为影响

原48格映射保持fuse-v2.0，D锚点保持。新增可靠性标志改变有效输入：显式声明Q适用时，固定healthy绿转黄、risk红转黄（P失效），edge仍黄；默认Q未声明时按R1红。不可将这些灯色变化描述为风险下降。仅直接调用benford_rf数值与前轮一致，调用方应读取可靠性字段，不能只检查树数。政策层静态阈值下限及缺证据不放行绿灯为新候选要求，未经业务接受不自动接入。

### 验证

42项候选测试Windows3.12.10/Ubuntu3.14.4各通过；33项原融合回归通过；30随机LP和实际DEA与已安装SciPy HiGHS目标一致；两平台各10轮语义摘要一致。详见ITERATION_03.md。测试不能证明任意平台、真实企业或业务决策的有效性。

### 迁移

先读ALGORITHM.md和ITERATION_03.md；保留同名签名。reference.py与certified_lp.py同目录，依赖本仓库v8模块；SciPy只用于experiment_v03.py。没有改服务/接口/库/前端/部署，本目录不提供线上替换批准。旧v0.2快照在history/v0_2，历史报告和JSON不覆盖。

## ra-next-0.2

增加原始可行性残差检查、非法输入拦截和六种子同森林校准消融，18候选测试通过；本轮已补原先缺失的对偶验证和双平台检查。

## ra-next-0.1

初始Monte Carlo+Holm质量分、D分层bootstrap、固定数字特征、随机子空间森林和OOB/独立sigmoid校准；保留isotonic失败证据。旧结果仅限其对应代码版本。

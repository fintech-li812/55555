from pathlib import Path
import json,statistics
root=Path(r'D:\CashflowRiskWorkbench\algo\RA_NEXT')
data=json.loads((root/'results_v02.json').read_text(encoding='utf-8'))
rows=data['replicates'];avg={k:statistics.mean(x[k]['brier'] for x in rows) for k in ('baseline','same_forest_raw','same_forest_calibrated')}
text='''# RA_NEXT 第二轮迭代报告

版本 ra-next-0.2；2026-10-29；只在 algo/RA_NEXT 开发离线算法。v0.1主要文件快照在 history/v0_1，旧results.json和失败保序实验保留。

## 本轮修正

1. D求解增加局部诊断：原始等式约束相对残差、非负变量违规量、目标值一致性，阈值1e-7；未通过则D不可用。使用独立命名空间调用基线实现，不修改基线全局对象。保留Bland规则。此检查证明原始可行性，不证明最优性，optimality_verified明确为false。
2. D/P拒绝NaN、无穷、布尔值、字符串、缺字段、非法标签和非正应收周转率；结构错误显式不可用。Q补结构检查，多项检验补概率/计数/模拟次数校验。
3. 修复直接运行reference.py时只输出Q/D的入口位置，现包含P。新增测试对真正使用的sigmoid做单调性检查。
4. 48格直接枚举规则命中数，取代旧候选测试中自我比较作为结构证据。旧融合映射未改。

## 校准消融

预设训练种子31001—31006，每组训练40例、独立校准10例（从40例中留出）、新生成测试120例，测试随机种子=训练种子+100000。每组同一森林、同一测试样本同时计算raw与calibrated，避免把特征或模型变化错归因为校准。六组共720条合成测试；所有组共享同一合成机制，不代表真实企业泛化。无测试集调参，保留全部六组。

|训练种子|旧模型 Brier|同模型未校准|同模型校准后|
|---|---:|---:|---:|
'''
for x in rows:
 text+=f"|{x['seed']}|{x['baseline']['brier']:.6f}|{x['same_forest_raw']['brier']:.6f}|{x['same_forest_calibrated']['brier']:.6f}|\n"
text+=f"\n六组平均：旧模型 {avg['baseline']:.6f}，候选未校准 {avg['same_forest_raw']:.6f}，候选校准后 {avg['same_forest_calibrated']:.6f}。同模型校准Brier平均变化 {data['calibration_mean_delta']:.6f}，6/6组改善。候选整体5/6组优于旧模型；31002组仍弱于旧模型。不把六组结果解释为统计显著性或真实业务保证。分箱可靠性及ECE见results_v02.json。\n"
text+='''
## 求解器与验证

固定原池下D：healthy=0.868543、edge=0.187676、risk=−0.471113；均与上轮一致。相对原始约束残差约1.50e-13，非负违规量0，目标一致性误差0。对偶最优性尚未验证。

本轮18项候选测试全部通过：48格分区、sigmoid单调性、非法数值/标签/结构、多项输入校验、已知LP最优值、不可行LP、假成功和非有限解、缺失值融合及10次同种子重复等。原33项融合测试上一轮已通过；生产源和融合实现未改，本轮未重复执行。仅Windows Python3.12.10，无Linux验证，不宣称跨平台验收完成。

## 仍然存在的限制

Q的Benford适用性尚未证明，Holm后的Q与旧Q定义不可直接比较；MC区间仍仅反映模拟误差。D区间是探索性bootstrap，不保证覆盖；无对偶或最优性证书。仅10个校准样本仍很少，sigmoid优化没有收敛证书。尚无真实企业验证、多引擎统一视图、动态阈值置信度算法或完整assess性能验收。V1—V8未全部通过。

## 复现

在仓库内运行 python algo/RA_NEXT/tests_ra_next.py；python algo/RA_NEXT/experiment_v02.py。后者输出results_v02.json；耗时是环境观测，不参与确定性比较。v0.1的experiment.py仍保留，用当前reference运行时属于新版复跑，不能用来重现旧版；旧版复现需把history/v0_1/reference.py临时复制到同样的仓库相对布局。

## 分析复核（validate-data）

结论：可附限制分享研究结果，不能用于生产算法放行。分母一致、校准与测试分离、同模型配对、全部种子公开、负面组保留；主要外推限制是合成机制固定和校准样本极少。下一步需要独立真实数据验证以及对偶最优性校验。
'''
(root/'ITERATION_02.md').write_text(text,encoding='utf-8')
for name in ('ALGORITHM.md','CHANGELOG.md'):
 p=root/name
 with p.open('a',encoding='utf-8') as f:
  f.write('\n\n## ra-next-0.2 增量\n当前版本已更新为ra-next-0.2；完整本轮改动、参数、消融实验与剩余限制见ITERATION_02.md。相对原始可行残差/负变量/目标误差门限均为1e-7；仅原始可行检查，不是最优性认证。原v0.1快照在history/v0_1。18项候选测试通过；六种子配对实验见results_v02.json。\n')
output=Path(r'C:\Users\33847\Documents\Codex\2026-10-29\new-chat\outputs')
output.mkdir(exist_ok=True)
(output/'现金流算法_第二轮迭代报告.md').write_text(text,encoding='utf-8')
print(json.dumps(avg))

# 实验索引

当前版本ra-next-0.3，先读ITERATION_03.md。

|实验|脚本|结果|
|---|---|---|
|v0.3 最优性与完整算法性能|experiment_v03.py|results_v03.json|
|v0.3 校准标签占比变化|calibration_shift_v03.py|calibration_shift_v03.json|
|v0.3 双平台十次重复|cross_platform.py|determinism_windows_v03.json / determinism_linux_v03.json|
|v0.3 原融合回归与依赖指纹|verify_v03.py|verification_v03.json / fusion_regression_v03.txt|
|v0.3 候选测试|tests_v03.py|双平台42项通过，见第三轮报告|
|v0.2 六种子同模型消融|experiment_v02.py|results_v02.json / ITERATION_02.md|
|v0.1 单种子/失败保序|experiment.py|results.json / experiment_isotonic_rejected.json|

本目录脚本默认导入当前reference.py。旧版本精确复现需恢复对应history快照到同样的仓库相对目录结构；不能将旧结果文件当成当前脚本运行结果。v0.3性能仅纯算法不含服务/数据库；冷启动与热启动分别报告。全部评测数据均为合成，不外推真实业务准确率。

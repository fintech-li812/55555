# 滚动现金流预测（rollcast）

把 `model/现金流风控_v2/risk_model.py` 的 13 周蒙特卡洛引擎，从「一次性前瞻模拟」扩展为
真正的 walk-forward 滚动预测，并补齐端到端工作流与上线所需的工程件。

```
数据接入校验 → 特征构建 → 滚动窗口训练/推理 → 结果输出回写 → 调度重试 → 日志告警
```

---

## 一、它解决什么问题

原引擎每次只用一段历史误差做单次 13 周模拟，存在三处未处理的偏差：

| 问题 | 现象 | 后果 |
|---|---|---|
| 采样块长与业务周期冲突 | 默认 `block_length=4`，而数据存在 7 周周期 | 13 周累计误差标准差被放大 **1.55 倍**，风险敞口系统性高估 → 过度融资、误报警 |
| 误差均值未处理 | 均值 μ 沿路径积分成 `13 × μ` 的漂移 | 资金路径整体偏移 |
| 无法评估自身精度 | 只有一次模拟结果，没有样本外评测 | 精度好坏无从判断，也无法对比不同配置 |

优化后（合成数据、walk-forward 样本外实测）：

| 指标 | 优化前 | 优化后 |
|---|---|---|
| 累计误差标准差 / 真实值 | 1.55x | **1.02x** |
| 名义 90% 区间的实际覆盖率 | 100%（区间过宽失效） | **91.5%** |
| 偏差策略选错时的 MAE 损失 | — | 自动选中更优策略，**相对最差策略改善 5.5%~21%** |
| 增量更新（94 次追加） | 22.22 ms | **0.85 ms（26.1x）** |

---

## 二、依赖环境

| 项 | 要求 | 说明 |
|---|---|---|
| Python | ≥ 3.10 | 仅用标准库 + numpy |
| numpy | ≥ 1.24 | 唯一硬依赖，蒙特卡洛与统计量 |
| PyMySQL | ≥ 1.0 | **仅持久化时需要**，不落库可不安 |
| PyYAML | 可选 | 仅当用 YAML 配置文件时 |
| 数据库 | MySQL 8.0+ | 库名必须以 `cfqa_` 开头（隔离红线） |

零第三方数值库：t 分布 p 值自己实现（正则化不完全贝塔连分式），
与 `scipy.stats.t` 对照最大绝对误差 **1.7e-15**，不引入 scipy。

---

## 三、部署步骤

### 1. 安装依赖

```bash
/opt/cashflow/venv/bin/pip install --only-binary=:all: numpy pymysql
```

> 用二进制 wheel，避免在这台 1.7 GB 内存的机器上编译。

### 2. 放置代码

```
/opt/cashflow/rollcast/          包本体
├── jobs/weekly.json             定时任务输入
├── config/production.json       生产配置
├── deploy/run_weekly.sh         定时执行包装脚本
└── tests/                       测试
/var/lib/rollcast/out/           结果输出（保留 120 天）
/var/log/rollcast/               运行日志
```

```bash
chown -R cfrun:cfrun /opt/cashflow/rollcast /var/lib/rollcast /var/log/rollcast
chmod 755 /opt/cashflow/rollcast/deploy/run_weekly.sh
```

### 3. 建表（一次性，用管理账号）

运行时账号 `cf_app` 只有 SELECT / INSERT，**建不了表**——这是有意为之的最小权限。

```bash
cd /opt/cashflow && set -a; . deploy/.env; set +a
export CF_DB_USER=cfadmin CF_DB_PASSWORD=$(cat /root/.cfadmin.pw)
/opt/cashflow/venv/bin/python -m rollcast.cli --init-db
```

### 4. 装定时器与服务

```bash
cp rollcast/deploy/cf-rollcast.{service,timer} /etc/systemd/system/
cp rollcast/deploy/logrotate.conf /etc/logrotate.d/rollcast
systemctl daemon-reload
systemctl enable --now cf-rollcast.timer      # 每周一 04:20
```

### 5. 验证

```bash
systemctl start cf-rollcast.service           # 手动触发一次
systemctl status cf-rollcast.service
cat /var/lib/rollcast/out/latest.json | head -40
```

---

## 四、日常使用

```bash
# 跑一次（不落库）
python -m rollcast.cli job.json --output result.json

# 跑一次并落库
python -m rollcast.cli job.json --output result.json \
       --config config/production.json --persist

# 覆盖参数
python -m rollcast.cli job.json --output out.json --draws 20000 --debias auto

# 跑测试
python -m unittest -v rollcast.tests.test_rollcast
```

退出码：`0` 成功 · `2` 数据闸拦截 · `3` 阶段失败 · `4` 用法错误。

---

## 五、输入契约

```jsonc
{
  "unit": "万元",                       // 必填，金额单位
  "quality_checks": {                   // 五项数据闸，必须显式布尔
    "reconciled": true, "unique_records": true, "complete": true,
    "fresh": true, "unit_consistent": true
  },
  "historical_forecast_errors": [       // ≥26 周，按日历周连续排列
    [实际收款 − 当时预测收款, 实际付款 − 当时预测付款]
  ],
  "current_plan": {
    "weekly_inflow":  [/* 13 周毛回款 */],
    "weekly_outflow": [/* 13 周毛付款 */],
    "cash_total": 1000.0, "cash_restricted": 100.0,
    "committed_credit_available_now": 100.0, "minimum_buffer": 100.0
  },
  "panel": [                            // 可选，仅在需要评测精度时提供
    { "origin": "W001",
      "forecast": [[收,付] × 13],
      "actual":   [[收,付] × 13] }
  ]
}
```

**红线**：`historical_forecast_errors` 必须是「预测起点之前、口径一致、标签已成熟」的档案误差。
不能拿当前模型在历史数据上重拟合的残差冒充当时的预测误差。

---

## 六、输出要点

- `forecast.block_used` / `block_rationale`：采样块长及其选择理由（**从不静默**）
- `forecast.bias.note`：偏差是否去除、依据的 p 值
- `forecast.gap_p95` / `gap_cvar95`：尾部缺口
- `backtest.mae` / `rmse` / `mean_terminal_drift`：样本外精度
- `trace`：五个阶段的耗时与决策，全链路可复现
- `config_fingerprint`：配置指纹，两次运行可比对是否同参

---

## 七、与原有引擎的关系

**没有改动 `risk_model.py` 一行代码。** rollcast 是并列的新模块，复用其输入契约与
金额口径。原引擎继续服务现有工作台，两者结果互不干扰。

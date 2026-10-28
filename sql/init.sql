-- =============================================================================
-- CashflowRiskWorkbench · 数据库地基
-- 文件：sql/init.sql
-- 依据：docs/千问办公_MySQL_MCP对接协议.md §八；docs/规格裁决_20261021.md §五(A5)
-- 版本：1.0  生成日期：2026-10-21
--
-- 口径约定（全库统一，请勿在表内另立）
--   1. 金额一律 DECIMAL(20,2)，单位为「元」。源表以「万元」计列者（四个 xlsx 样例）
--      必须在 seed 阶段 ×10000 换算，禁止把万元数值直接入库（协议 §一）。
--   2. 时间一律 DATETIME(6)，存 UTC。occurred_at 由入参 RFC3339 带时区值转换而来；
--      created_at 一律由服务端写入，不采用模型自报时间（协议 §四）。
--      连接层须执行 SET time_zone = '+00:00'。
--   3. 结构化内容用 JSON 列；写入前用 json.dumps，禁止 Python repr 字符串（协议 §四）。
--   4. 枚举类字段一律用 VARCHAR + 注释白名单，不用 ENUM：业务账号无 ALTER 权限，
--      ENUM 取值变更会需要 DDL，VARCHAR 可由应用层白名单校验（协议 §八 权限约束）。
--   5. 追加式记录。纠错不原地改写，而是新增一行并用 supersedes_id 指向被替代行；
--      统计视图 v_record_active 已排除被替代版本，避免重复计数（协议 §八）。
--   6. 不设外键：审计类表按 tenant_id 逻辑隔离，导入顺序与分库迁移更省事；
--      引用完整性由 MCP 工具层参数绑定与唯一约束保证。
--
-- 执行：mysql --no-defaults -u cf_admin -p < sql/init.sql
-- 幂等：全部 CREATE ... IF NOT EXISTS / CREATE OR REPLACE VIEW，可重复执行。
-- 待验：本文件未在真实 MySQL 8.x 实例上执行过，首次落地请回贴报错。
-- =============================================================================

CREATE DATABASE IF NOT EXISTS cf_workbench
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE cf_workbench;

-- -----------------------------------------------------------------------------
-- 1. mcp_request_ledger —— 幂等账本（与业务写入同事务提交）
--    唯一键 (tenant_id, tool_name, request_id) 是全部写请求的防重底线。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mcp_request_ledger (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id      VARCHAR(64)   NOT NULL,
  tool_name      VARCHAR(64)   NOT NULL COMMENT '14 个工具名之一',
  request_id     VARCHAR(128)  NOT NULL COMMENT '客户端稳定业务幂等键',
  payload_hash   CHAR(64)      NOT NULL COMMENT '语义参数规范化 JSON 的 SHA-256',
  result_json    JSON          NOT NULL COMMENT '原样回放给重试方的业务结果',
  created_at     DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_ledger_key (tenant_id, tool_name, request_id),
  KEY idx_ledger_time (tenant_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='写请求幂等凭据；同键同哈希回放，同键异哈希返回 IDEMPOTENCY_CONFLICT';

-- -----------------------------------------------------------------------------
-- 2. audit_log —— 工作台审计事件（含三层模型的 Q/D 结果留痕）
--    event_type 白名单 24 值（v8 模块六 20 + 协议 4）。
--    risk_warning / risk_assessment 必须带 rule_id + rule_version + risk_level。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id      VARCHAR(64)   NOT NULL,
  request_id     VARCHAR(128)  NOT NULL,
  source_ref     VARCHAR(128)  NOT NULL COMMENT '表格/行/任务/报告的可定位引用',
  occurred_at    DATETIME(6)   NOT NULL COMMENT '事件发生时间 UTC',
  created_at     DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '服务端写入 UTC',
  actor_id       VARCHAR(128)  NOT NULL COMMENT '从已认证主体解析，不采信模型传入',
  supersedes_id  BIGINT UNSIGNED NULL COMMENT '纠错：指向被本行替代的原记录',
  event_type     VARCHAR(32)   NOT NULL COMMENT '白名单：daily_check quality_check deada_score rf_predict risk_assessment approval exception config_change assumption_analysis industry_compare param_adjust feedback onboarding root_cause dynamic_threshold stress_test exception_review audit_triggered payment_verified da_coefficients_updated risk_warning tracking_changed parameter_changed task_result',
  rule_id        VARCHAR(128)  NULL,
  rule_version   VARCHAR(128)  NULL,
  risk_level     VARCHAR(10)   NULL COMMENT 'green / yellow / red / unknown（不可判定）',
  payload        JSON          NOT NULL COMMENT '结果快照；禁含密码、完整卡号、整份流水',
  PRIMARY KEY (id),
  UNIQUE KEY uk_audit_req (tenant_id, request_id),
  KEY idx_audit_rule (tenant_id, rule_id, rule_version, occurred_at),
  KEY idx_audit_event (tenant_id, event_type, occurred_at),
  KEY idx_audit_supersedes (supersedes_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='审计留痕。MCP 只存储，不做风险判定';

-- -----------------------------------------------------------------------------
-- 3. feedback_log —— 预测与实际的对照反馈
--    audit_id 与 forecast_id 至少一个（工具层校验，DB 不设 CHECK 以便迁移）。
--    accuracy_label 由工作台按 evaluation_version 口径生成，缺实际数据不得填 accurate。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feedback_log (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id          VARCHAR(64)   NOT NULL,
  request_id         VARCHAR(128)  NOT NULL,
  source_ref         VARCHAR(128)  NOT NULL,
  occurred_at        DATETIME(6)   NOT NULL,
  created_at         DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  actor_id           VARCHAR(128)  NOT NULL,
  supersedes_id      BIGINT UNSIGNED NULL,
  audit_id           BIGINT UNSIGNED NULL COMMENT '关联 audit_log.id',
  forecast_id        VARCHAR(128)  NULL COMMENT '预测体外键（工作台侧 ID）',
  metric_code        VARCHAR(128)  NOT NULL,
  predicted_value    DECIMAL(20,2) NULL,
  actual_value       DECIMAL(20,2) NULL,
  currency           CHAR(3)       NOT NULL DEFAULT 'CNY',
  accuracy_label     VARCHAR(16)   NOT NULL COMMENT 'accurate / inaccurate / pending',
  evaluation_version VARCHAR(128)  NOT NULL,
  actual_source_ref  VARCHAR(128)  NOT NULL,
  forecast_start_at  DATETIME(6)   NOT NULL,
  forecast_end_at    DATETIME(6)   NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_feedback_req (tenant_id, request_id),
  KEY idx_feedback_metric (tenant_id, metric_code, evaluation_version, occurred_at),
  KEY idx_feedback_audit (tenant_id, audit_id),
  KEY idx_feedback_forecast (tenant_id, forecast_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预警准确度反馈';

-- -----------------------------------------------------------------------------
-- 4. param_template —— 行业参数模板（v8 相对指标版）
--    写入即 draft；发布由可信管理流程执行，不向模型暴露发布权。
--    params 必含 schema_version base_metric yellow_coefficient red_coefficient
--    approval_ratios provenance effective_from；base_metric 固定 monthly_operating_outflow。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS param_template (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id           VARCHAR(64)   NOT NULL,
  request_id          VARCHAR(128)  NOT NULL,
  source_ref          VARCHAR(128)  NOT NULL,
  occurred_at         DATETIME(6)   NOT NULL,
  created_at          DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  actor_id            VARCHAR(128)  NOT NULL,
  supersedes_id       BIGINT UNSIGNED NULL,
  industry_key        VARCHAR(128)  NOT NULL COMMENT '制造/零售/建筑/科技服务 的行业代码',
  template_version    VARCHAR(128)  NOT NULL COMMENT '不可覆盖历史版本',
  publication_status  VARCHAR(16)   NOT NULL DEFAULT 'draft' COMMENT 'draft / published / retired',
  params              JSON          NOT NULL,
  provenance          JSON          NOT NULL COMMENT '经验建议 / 企业批准 / 已核验外部来源，三者分标',
  effective_from      DATETIME(6)   NOT NULL,
  published_at        DATETIME(6)   NULL COMMENT '仅由管理身份写入',
  published_by        VARCHAR(128)  NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_tpl_ver (tenant_id, industry_key, template_version),
  UNIQUE KEY uk_tpl_req (tenant_id, request_id),
  KEY idx_tpl_published (tenant_id, industry_key, publication_status, effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='行业参数模板。历史事件始终引用当时版本';

-- -----------------------------------------------------------------------------
-- 5. exception_log —— 例外放行留痕（不产生审批、不执行付款）
--    trusted_approver_id 由服务端从可信审批系统核实后写入；核实时返回
--    APPROVAL_UNVERIFIED 且不落库为已批准。
--    若一份审批凭据合法覆盖多笔例外，需另设 approval_line_no，届时改唯一键。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exception_log (
  id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id            VARCHAR(64)   NOT NULL,
  request_id           VARCHAR(128)  NOT NULL,
  source_ref           VARCHAR(128)  NOT NULL,
  occurred_at          DATETIME(6)   NOT NULL,
  created_at           DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  actor_id             VARCHAR(128)  NOT NULL,
  supersedes_id        BIGINT UNSIGNED NULL,
  dept                 VARCHAR(128)  NOT NULL,
  amount               DECIMAL(20,2) NOT NULL COMMENT '单位元',
  currency             CHAR(3)       NOT NULL DEFAULT 'CNY',
  reason               TEXT          NOT NULL COMMENT '入参上限 4000 字符',
  approval_ref         VARCHAR(128)  NOT NULL COMMENT '可信审批凭据引用',
  approval_status      VARCHAR(16)   NOT NULL COMMENT 'verified / unverified',
  trusted_approver_id  VARCHAR(128)  NULL COMMENT '服务端解析，不采信模型提交的 approver 姓名',
  PRIMARY KEY (id),
  UNIQUE KEY uk_exc_req (tenant_id, request_id),
  UNIQUE KEY uk_exc_approval (tenant_id, approval_ref),
  KEY idx_exc_dept (tenant_id, dept, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='例外放行台账，季度回溯的统计源';

-- -----------------------------------------------------------------------------
-- 6. stress_test_log —— 压力测试结果（场景由工作台计算，MCP 只存）
--    scenario 白名单 5 值（裁决 A5）：receivable_shock 回款 / financing_shock 融资
--    / cost_shock 成本 / dual_shock 回款+融资 / combined_shock 全项联合。
--    同一 run_id 下每个场景唯一，防止部分完成后被重复补记冒充整批成功。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stress_test_log (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id         VARCHAR(64)   NOT NULL,
  request_id        VARCHAR(128)  NOT NULL,
  source_ref        VARCHAR(128)  NOT NULL,
  occurred_at       DATETIME(6)   NOT NULL,
  created_at        DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  actor_id          VARCHAR(128)  NOT NULL,
  supersedes_id     BIGINT UNSIGNED NULL,
  run_id            VARCHAR(128)  NOT NULL COMMENT '整批压测 ID，四场景共用',
  scenario          VARCHAR(32)   NOT NULL,
  scenario_version  VARCHAR(128)  NOT NULL,
  input_snapshot    JSON          NOT NULL COMMENT 'M、K黄、K红、缺口口径等输入',
  result_snapshot   JSON          NOT NULL COMMENT '最坏缓冲、缓冲倍数、剩余天数',
  risk_level        VARCHAR(10)   NOT NULL COMMENT 'green / yellow / red / unknown',
  PRIMARY KEY (id),
  UNIQUE KEY uk_stress_req (tenant_id, request_id),
  UNIQUE KEY uk_stress_run_scenario (tenant_id, run_id, scenario),
  KEY idx_stress_run (tenant_id, run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='常态化压力测试结果';

-- -----------------------------------------------------------------------------
-- 7. payment_verify_log —— 到账验证（模型不得自证 verified）
--    银行连接器未接入时固定 verification_status='pending' + reason='BANK_ADAPTER_UNAVAILABLE'。
--    未通过时 verified_amount 写 0.00，不写 NULL，便于汇总直接求和。
--    uk_pay_txn 用可信流水唯一键防重复核销；pending 阶段该列为 NULL（允许多行）。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_verify_log (
  id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id            VARCHAR(64)   NOT NULL,
  request_id           VARCHAR(128)  NOT NULL,
  source_ref           VARCHAR(128)  NOT NULL,
  occurred_at          DATETIME(6)   NOT NULL,
  created_at           DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  actor_id             VARCHAR(128)  NOT NULL,
  supersedes_id        BIGINT UNSIGNED NULL,
  payment_id           VARCHAR(128)  NOT NULL,
  method               VARCHAR(16)   NOT NULL COMMENT 'bank_api / manual_review',
  verification_status  VARCHAR(16)   NOT NULL COMMENT 'pending / verified / rejected',
  verified_amount      DECIMAL(20,2) NOT NULL DEFAULT 0.00,
  currency             CHAR(3)       NOT NULL DEFAULT 'CNY',
  evidence_ref         VARCHAR(128)  NOT NULL COMMENT '对象存储引用或摘要，不回传完整账户',
  review_ref           VARCHAR(128)  NULL COMMENT '已认证复核人记录引用',
  reason               VARCHAR(64)   NULL COMMENT '如 BANK_ADAPTER_UNAVAILABLE',
  bank_txn_ref         VARCHAR(128)  NULL COMMENT '可信银行流水唯一键',
  verified_at          DATETIME(6)   NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_pay_req (tenant_id, request_id),
  UNIQUE KEY uk_pay_txn (tenant_id, bank_txn_ref),
  KEY idx_pay_payment (tenant_id, payment_id, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='到账验证台账';

-- -----------------------------------------------------------------------------
-- 8. quality_log —— 第一层数据质量门禁逐指标结果（表格算出，写来留痕）
--    Benford χ² 临界 20.09；Myer 指数阈值 20（⚠ 公式待按原文订正，见核对报告 I1）。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quality_log (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id        VARCHAR(64)   NOT NULL,
  request_id       VARCHAR(128)  NOT NULL,
  source_ref       VARCHAR(128)  NOT NULL,
  occurred_at      DATETIME(6)   NOT NULL,
  created_at       DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  actor_id         VARCHAR(128)  NOT NULL,
  supersedes_id    BIGINT UNSIGNED NULL,
  check_date       DATE          NOT NULL,
  indicator_code   VARCHAR(64)   NOT NULL COMMENT '如 X4 流动比率',
  indicator_name   VARCHAR(128)  NOT NULL,
  sample_n         INT           NOT NULL,
  benford_chi2     DECIMAL(12,4) NULL,
  benford_risk_digit     VARCHAR(2)  NULL COMMENT '首位偏离最大数字',
  myer_index       DECIMAL(12,4) NULL COMMENT '⚠ 公式订正前本列值不可信',
  myer_risk_digit  VARCHAR(2)    NULL,
  benford_factor   TINYINT       NOT NULL DEFAULT 0,
  myer_factor      TINYINT       NOT NULL DEFAULT 0,
  quality_score    DECIMAL(6,5)  NOT NULL COMMENT 'Q ∈ [0,1]，全企业合成值',
  risk_flag        TINYINT       NOT NULL DEFAULT 0,
  formula_version  VARCHAR(128)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_qual_req (tenant_id, request_id),
  UNIQUE KEY uk_qual_day_ind (tenant_id, check_date, indicator_code, formula_version),
  KEY idx_qual_date (tenant_id, check_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据质量门禁结果（第一层）';

-- -----------------------------------------------------------------------------
-- 9. deada_score_log —— 第二层判别得分（权重稀疏，可解释性留证）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deada_score_log (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id           VARCHAR(64)   NOT NULL,
  request_id          VARCHAR(128)  NOT NULL,
  source_ref          VARCHAR(128)  NOT NULL,
  occurred_at         DATETIME(6)   NOT NULL,
  created_at          DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  actor_id            VARCHAR(128)  NOT NULL,
  supersedes_id       BIGINT UNSIGNED NULL,
  eval_date           DATE          NOT NULL,
  input_weights       JSON          NOT NULL COMMENT '投入指标权重 v_i',
  output_weights      JSON          NOT NULL COMMENT '产出指标权重 u_r',
  nonzero_indicators  JSON          NOT NULL COMMENT '稀疏非零项，答辩用',
  discriminant_score  DECIMAL(12,6) NOT NULL COMMENT 'D ∈ [-1,1]',
  threshold_value     DECIMAL(12,6) NOT NULL,
  is_risk             TINYINT       NOT NULL DEFAULT 0 COMMENT 'D<0 置 1',
  coefficient_version VARCHAR(128)  NOT NULL COMMENT '引用 da_coefficient 版本',
  formula_version     VARCHAR(128)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_deada_req (tenant_id, request_id),
  KEY idx_deada_date (tenant_id, eval_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='改进 DEA-DA 判别得分（第二层）';

-- -----------------------------------------------------------------------------
-- 10. rf_predict_log —— 第三层舞弊概率推理留痕（唯一计算型工具的副作用记录）
--     训练标签为 ST/风险，与「舞弊」不等同（包 B 任务书 §9），model_version 必填。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rf_predict_log (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id          VARCHAR(64)   NOT NULL,
  request_id         VARCHAR(128)  NOT NULL,
  source_ref         VARCHAR(128)  NOT NULL,
  occurred_at        DATETIME(6)   NOT NULL,
  created_at         DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  actor_id           VARCHAR(128)  NOT NULL,
  supersedes_id      BIGINT UNSIGNED NULL,
  eval_date          DATE          NOT NULL,
  features           JSON          NOT NULL COMMENT '30 维：15 指标 + 15 Benford 因子',
  fraud_probability  DECIMAL(6,5)  NOT NULL COMMENT 'P ∈ [0,1]',
  n_estimators       INT           NOT NULL,
  model_version      VARCHAR(128)  NOT NULL,
  training_label     VARCHAR(64)   NOT NULL COMMENT '如实记 ST/风险，不写「舞弊」',
  inference_ms       INT           NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_rf_req (tenant_id, request_id),
  KEY idx_rf_date (tenant_id, eval_date),
  KEY idx_rf_model (tenant_id, model_version, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='随机森林推理记录（第三层）';

-- -----------------------------------------------------------------------------
-- 11. da_coefficient —— DEA-DA 权重系数表（离线求解，MCP 只存取，不求解）
--     每行一个指标在某版本下的权重；千问表格用 SUMPRODUCT 按版本取权重算 D。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS da_coefficient (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id        VARCHAR(64)    NOT NULL,
  request_id       VARCHAR(128)   NOT NULL,
  source_ref       VARCHAR(128)   NOT NULL COMMENT '离线求解脚本产物引用',
  created_at       DATETIME(6)    NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  actor_id         VARCHAR(128)   NOT NULL,
  industry_key     VARCHAR(128)   NOT NULL,
  coefficient_version VARCHAR(128) NOT NULL,
  indicator_code   VARCHAR(64)    NOT NULL,
  indicator_name   VARCHAR(128)   NOT NULL,
  io_role          VARCHAR(8)     NOT NULL COMMENT 'input / output',
  direction        VARCHAR(8)     NOT NULL COMMENT 'positive / inverse（逆指标先取倒数）',
  weight           DECIMAL(18,10) NOT NULL,
  returns_to_scale VARCHAR(8)     NOT NULL DEFAULT 'VRS' COMMENT 'CRS / VRS（BCC）',
  solved_at        DATETIME(6)    NOT NULL,
  solver_ref       VARCHAR(255)   NOT NULL COMMENT '求解脚本版本与样本区间，可追溯',
  PRIMARY KEY (id),
  UNIQUE KEY uk_coef_row (tenant_id, industry_key, coefficient_version, indicator_code),
  UNIQUE KEY uk_coef_req (tenant_id, request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='判别权重，稀疏可解释';

-- =============================================================================
-- 视图：统计口径统一从这里出，避免各工具各写一遍 SQL 造成口径漂移
-- 聚合维度与排序字段必须由 app.py 白名单放行，禁止拼接模型给的列名（协议 §八）
-- =============================================================================

-- 排除被替代版本，防重复计数
CREATE OR REPLACE VIEW v_record_active AS
SELECT a.* FROM audit_log a
WHERE NOT EXISTS (SELECT 1 FROM audit_log s WHERE s.supersedes_id = a.id);

-- 规则触发统计。注意：统计的是「预警事件数」，不等于准确率（协议 §5.2）
CREATE OR REPLACE VIEW v_rule_stats AS
SELECT tenant_id, rule_id, rule_version, risk_level,
       COUNT(*) AS event_count,
       MIN(occurred_at) AS first_at,
       MAX(occurred_at) AS last_at
FROM v_record_active
GROUP BY tenant_id, rule_id, rule_version, risk_level;

-- 反馈按标签汇总。不在此计算通用准确率：分母、成熟观察期、缺失处理未定（协议 §5.3）
CREATE OR REPLACE VIEW v_feedback_label_counts AS
SELECT tenant_id, metric_code, evaluation_version, accuracy_label,
       COUNT(*) AS label_count
FROM feedback_log
WHERE NOT EXISTS (SELECT 1 FROM feedback_log s WHERE s.supersedes_id = feedback_log.id)
GROUP BY tenant_id, metric_code, evaluation_version, accuracy_label;

-- 例外笔数与金额。例外率的分母需要同范围审批总笔数，缺失时上层必须返回 null（协议 §5.5）
CREATE OR REPLACE VIEW v_exception_stats AS
SELECT tenant_id, dept,
       COUNT(*)    AS exception_count,
       SUM(amount) AS exception_amount_sum,
       MIN(occurred_at) AS first_at,
       MAX(occurred_at) AS last_at
FROM exception_log
WHERE NOT EXISTS (SELECT 1 FROM exception_log s WHERE s.supersedes_id = exception_log.id)
GROUP BY tenant_id, dept;

-- 压测批次进度：scenarios_done < 4 时禁止对外声明整批成功（协议 §5.6）
CREATE OR REPLACE VIEW v_stress_run_progress AS
SELECT tenant_id, run_id,
       COUNT(DISTINCT scenario) AS scenarios_done,
       4                        AS scenarios_required,
       COUNT(DISTINCT scenario) < 4 AS is_partial,
       MAX(created_at)          AS last_write_at
FROM stress_test_log
GROUP BY tenant_id, run_id;

-- 已验证到账汇总（供三态跟踪核对「待验证不计入缓冲」）
CREATE OR REPLACE VIEW v_verified_payment_sum AS
SELECT tenant_id,
       SUM(verified_amount) AS verified_amount_sum,
       COUNT(*)             AS verified_count
FROM payment_verify_log
WHERE verification_status = 'verified'
  AND NOT EXISTS (SELECT 1 FROM payment_verify_log s WHERE s.supersedes_id = payment_verify_log.id)
GROUP BY tenant_id;

-- 当前已发布的行业模板最新版本（load_param_template 省略版本号时的语义）
CREATE OR REPLACE VIEW v_template_published_latest AS
SELECT p.tenant_id, p.industry_key, p.template_version, p.params, p.provenance,
       p.effective_from, p.published_at
FROM param_template p
WHERE p.publication_status = 'published'
  AND p.effective_from = (
    SELECT MAX(p2.effective_from) FROM param_template p2
    WHERE p2.tenant_id = p.tenant_id AND p2.industry_key = p.industry_key
      AND p2.publication_status = 'published' AND p2.effective_from <= NOW(6)
  );

-- =============================================================================
-- 授权（口令不在本文件出现；账号由部署手册 §3.2 用环境变量创建）
-- =============================================================================
-- 运行账号：只给 SELECT/INSERT，不给 UPDATE/DELETE/DROP/ALTER（协议 §八）
GRANT SELECT, INSERT ON cf_workbench.* TO 'cf_app'@'localhost';
-- 管理身份：建表迁移与模板发布走它，与业务账号分离
GRANT SELECT, INSERT, UPDATE, CREATE, ALTER, DROP, INDEX, REFERENCES
  ON cf_workbench.* TO 'cf_admin'@'localhost';
-- 备份身份：独立，不复用受限业务账号
GRANT SELECT, LOCK TABLES, SHOW VIEW, TRIGGER ON cf_workbench.* TO 'cf_backup'@'localhost';
GRANT PROCESS ON *.* TO 'cf_backup'@'localhost';

FLUSH PRIVILEGES;

-- =============================================================================
-- 落地后自检（手工执行，确认 11 表 + 8 视图齐全）
--   SHOW TABLES;                                   -- 期望 11 张表
--   SHOW FULL TABLES WHERE Table_type='VIEW';      -- 期望 8 个视图
--   SELECT @@character_set_database, @@collation_database;
--     -- 期望 utf8mb4 / utf8mb4_0900_ai_ci
-- =============================================================================

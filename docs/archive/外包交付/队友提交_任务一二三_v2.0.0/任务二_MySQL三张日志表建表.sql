-- 三项任务复核修订版：2.0.0 / 2026-10-22
-- 目标：MySQL 8.0.16+ 或 8.4；不是 MariaDB 兼容脚本。
-- 仅首次建表：不自动迁移旧表，不删表、不改旧记录。
-- 已有同名表时 CREATE TABLE 必须报错停止，不能用 IF NOT EXISTS 掩盖结构差异。
-- 运行方式：以UTF-8标准输入批处理导入；以下 < 为cmd/bash重定向示意，不是PowerShell语法：
-- mysql --default-character-set=utf8mb4 -u 部署账号 -p < 本文件
-- Windows SOURCE若不能识别中文文件路径，先复制到纯英文路径；勿用会改变编码的管道。
-- 不使用 --force；DDL 存在隐式提交，不能依靠 ROLLBACK 撤回整个部署。
-- 部署前导出现有结构和数据；DDL账号与运行时账号分开。
-- 本脚本只建三张模型日志表，不提供 audit_log/feedback_log/param_template 等其他存储。
-- 服务端需同步适配新增的上下文、幂等、状态及 JSON 字段；不可只换 SQL 就上线。
-- 依据：https://dev.mysql.com/doc/refman/8.0/en/create-table-check-constraints.html
--       https://dev.mysql.com/doc/refman/8.0/en/implicit-commit.html

SET NAMES utf8mb4 COLLATE utf8mb4_bin;
SET SESSION time_zone = '+00:00';
SET SESSION sql_mode = 'STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ZERO_DATE,NO_ZERO_IN_DATE,NO_ENGINE_SUBSTITUTION';

CREATE DATABASE IF NOT EXISTS audit_db
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
USE audit_db;

-- 共同契约：
-- tenant_id/company_id 必须经服务端身份授权，不信任模型自行提供的租户范围。
-- run_id 关联同次评估；request_id 追踪请求；record_key 标识该表中同次逻辑记录。
-- quality 每个指标一条；deada/rf 每个逻辑预测一条，record_key 必须稳定。
-- 同 tenant/company/run/record_key 重试：
--   payload_sha256相同则返回既有log_id；不同则冲突，不得覆盖。
-- SHA256服务端对规范化业务输入、输出、数据快照和版本计算；
-- 不含 created_at、request_id、重试次数等易变传输字段；禁止只相信调用者传入摘要。
-- execution_status为error/skipped/insufficient_data时，未算出的统计量、得分和结论必须NULL。
-- 当前表不实现租户行级访问控制；应用所有读写必须加授权租户和企业条件。

CREATE TABLE quality_log (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id VARCHAR(64) NOT NULL,
  company_id VARCHAR(64) NOT NULL COMMENT '企业编号，不能拿同业批次号冒充',
  run_id VARCHAR(64) NOT NULL,
  request_id VARCHAR(64) NOT NULL,
  record_key VARCHAR(128) NOT NULL COMMENT '同次评估的指标逻辑键，重试保持不变',
  payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  execution_status VARCHAR(24) NOT NULL,
  check_date DATE NOT NULL COMMENT '业务评价日；与UTC落库时间区分',
  data_snapshot_id VARCHAR(128) NOT NULL,
  cohort_id VARCHAR(128) DEFAULT NULL COMMENT '同业/样本批次，与company_id分离',
  indicator_name VARCHAR(128) NOT NULL,
  sample_size INT UNSIGNED DEFAULT NULL,
  benford_chi2 DECIMAL(24,10) DEFAULT NULL,
  benford_factor TINYINT UNSIGNED DEFAULT NULL COMMENT '未运行为NULL，不能默认0',
  myer_index DECIMAL(24,10) DEFAULT NULL COMMENT '量纲由myer_scale及algorithm_version说明',
  myer_scale VARCHAR(64) DEFAULT NULL COMMENT '不得猜测0.20与20之间的转换关系',
  myer_factor TINYINT UNSIGNED DEFAULT NULL,
  quality_score DECIMAL(12,10) DEFAULT NULL COMMENT '指标级分数，不能直接当作公司Q',
  company_quality_score DECIMAL(12,10) DEFAULT NULL COMMENT '经批准聚合后公司Q；无聚合结果为NULL',
  aggregation_version VARCHAR(64) DEFAULT NULL,
  risk_flag TINYINT UNSIGNED DEFAULT NULL COMMENT '需复核线索，不是舞弊事实',
  algorithm_version VARCHAR(64) DEFAULT NULL,
  rule_version VARCHAR(64) NOT NULL,
  parameters JSON NOT NULL COMMENT '当次统计口径/阈值/量纲/样本筛选快照',
  input_snapshot JSON NOT NULL COMMENT '受控诊断输入或不可变证据引用',
  result_snapshot JSON DEFAULT NULL,
  error_code VARCHAR(64) DEFAULT NULL,
  error_message TEXT DEFAULT NULL,
  remark TEXT DEFAULT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT 'UTC；连接池必须设置time_zone=+00:00',
  PRIMARY KEY (id),
  UNIQUE KEY uq_quality_operation (tenant_id,company_id,run_id,record_key),
  KEY idx_quality_company_date (tenant_id,company_id,check_date),
  KEY idx_quality_run (tenant_id,company_id,run_id),
  CONSTRAINT ck_quality_identity CHECK (CHAR_LENGTH(TRIM(tenant_id))>0 AND CHAR_LENGTH(TRIM(company_id))>0 AND CHAR_LENGTH(TRIM(run_id))>0 AND CHAR_LENGTH(TRIM(record_key))>0 AND CHAR_LENGTH(TRIM(request_id))>0),
  CONSTRAINT ck_quality_hash CHECK (payload_sha256 REGEXP '^[0-9a-f]{64}$'),
  CONSTRAINT ck_quality_status CHECK (execution_status IN ('success','skipped','insufficient_data','error')),
  CONSTRAINT ck_quality_numbers CHECK ((benford_chi2 IS NULL OR benford_chi2>=0) AND (myer_index IS NULL OR myer_index>=0)),
  CONSTRAINT ck_quality_flags CHECK ((benford_factor IS NULL OR benford_factor IN (0,1)) AND (myer_factor IS NULL OR myer_factor IN (0,1)) AND (risk_flag IS NULL OR risk_flag IN (0,1))),
  CONSTRAINT ck_quality_scores CHECK ((quality_score IS NULL OR quality_score BETWEEN 0 AND 1) AND (company_quality_score IS NULL OR company_quality_score BETWEEN 0 AND 1)),
  CONSTRAINT ck_quality_aggregate CHECK ((company_quality_score IS NULL AND aggregation_version IS NULL) OR (company_quality_score IS NOT NULL AND aggregation_version IS NOT NULL AND CHAR_LENGTH(TRIM(aggregation_version))>0)),
  CONSTRAINT ck_quality_success CHECK (execution_status<>'success' OR (sample_size IS NOT NULL AND sample_size>0 AND benford_chi2 IS NOT NULL AND benford_factor IS NOT NULL AND myer_index IS NOT NULL AND myer_scale IS NOT NULL AND CHAR_LENGTH(TRIM(myer_scale))>0 AND myer_factor IS NOT NULL AND quality_score IS NOT NULL AND risk_flag IS NOT NULL AND algorithm_version IS NOT NULL AND CHAR_LENGTH(TRIM(algorithm_version))>0 AND result_snapshot IS NOT NULL)),
  CONSTRAINT ck_quality_uncomputed CHECK (execution_status='success' OR (benford_chi2 IS NULL AND benford_factor IS NULL AND myer_index IS NULL AND myer_factor IS NULL AND quality_score IS NULL AND company_quality_score IS NULL AND risk_flag IS NULL)),
  CONSTRAINT ck_quality_error CHECK (execution_status<>'error' OR (error_code IS NOT NULL AND CHAR_LENGTH(TRIM(error_code))>0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='模型日志v2：指标质量诊断，聚合Q须另有版本';

CREATE TABLE deada_score_log (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id VARCHAR(64) NOT NULL,
  company_id VARCHAR(64) NOT NULL,
  run_id VARCHAR(64) NOT NULL,
  request_id VARCHAR(64) NOT NULL,
  record_key VARCHAR(128) NOT NULL,
  payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  execution_status VARCHAR(24) NOT NULL,
  eval_date DATE NOT NULL,
  data_snapshot_id VARCHAR(128) NOT NULL,
  input_weights JSON DEFAULT NULL,
  output_weights JSON DEFAULT NULL,
  core_indicators JSON DEFAULT NULL COMMENT '实际模型输入指标映射，设计为7项；与特征清单核对',
  feature_schema JSON DEFAULT NULL COMMENT '有序特征名、单位和预处理清单',
  intercept DECIMAL(28,12) DEFAULT NULL COMMENT '如模型无截距，成功时显式写0，不能遗漏',
  discriminant_score DECIMAL(28,12) DEFAULT NULL,
  threshold DECIMAL(28,12) DEFAULT NULL COMMENT '当次模型临界值，不在日志层擅自写死',
  is_risk TINYINT UNSIGNED DEFAULT NULL COMMENT '仅D<threshold，未运行为NULL',
  model_version VARCHAR(64) DEFAULT NULL,
  preprocess_version VARCHAR(64) DEFAULT NULL,
  rule_version VARCHAR(64) NOT NULL,
  result_snapshot JSON DEFAULT NULL,
  error_code VARCHAR(64) DEFAULT NULL,
  error_message TEXT DEFAULT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_deada_operation (tenant_id,company_id,run_id,record_key),
  KEY idx_deada_company_date (tenant_id,company_id,eval_date),
  KEY idx_deada_run (tenant_id,company_id,run_id),
  CONSTRAINT ck_deada_identity CHECK (CHAR_LENGTH(TRIM(tenant_id))>0 AND CHAR_LENGTH(TRIM(company_id))>0 AND CHAR_LENGTH(TRIM(run_id))>0 AND CHAR_LENGTH(TRIM(record_key))>0 AND CHAR_LENGTH(TRIM(request_id))>0),
  CONSTRAINT ck_deada_hash CHECK (payload_sha256 REGEXP '^[0-9a-f]{64}$'),
  CONSTRAINT ck_deada_status CHECK (execution_status IN ('success','skipped','insufficient_data','error')),
  CONSTRAINT ck_deada_flag CHECK (is_risk IS NULL OR is_risk IN (0,1)),
  CONSTRAINT ck_deada_success CHECK (execution_status<>'success' OR (input_weights IS NOT NULL AND output_weights IS NOT NULL AND core_indicators IS NOT NULL AND feature_schema IS NOT NULL AND intercept IS NOT NULL AND discriminant_score IS NOT NULL AND threshold IS NOT NULL AND is_risk IS NOT NULL AND model_version IS NOT NULL AND CHAR_LENGTH(TRIM(model_version))>0 AND preprocess_version IS NOT NULL AND CHAR_LENGTH(TRIM(preprocess_version))>0 AND result_snapshot IS NOT NULL)),
  CONSTRAINT ck_deada_consistency CHECK (execution_status<>'success' OR is_risk=IF(discriminant_score<threshold,1,0)),
  CONSTRAINT ck_deada_uncomputed CHECK (execution_status='success' OR (discriminant_score IS NULL AND is_risk IS NULL)),
  CONSTRAINT ck_deada_error CHECK (execution_status<>'error' OR (error_code IS NOT NULL AND CHAR_LENGTH(TRIM(error_code))>0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='模型日志v2：DEA-DA输入权重及可复现分数';

CREATE TABLE rf_predict_log (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id VARCHAR(64) NOT NULL,
  company_id VARCHAR(64) NOT NULL,
  run_id VARCHAR(64) NOT NULL,
  request_id VARCHAR(64) NOT NULL,
  record_key VARCHAR(128) NOT NULL,
  payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  execution_status VARCHAR(24) NOT NULL,
  eval_date DATE NOT NULL,
  data_snapshot_id VARCHAR(128) NOT NULL,
  features JSON DEFAULT NULL,
  feature_names JSON DEFAULT NULL COMMENT '与特征值一一对应的有序名称',
  feature_schema_version VARCHAR(64) DEFAULT NULL,
  preprocess_version VARCHAR(64) DEFAULT NULL,
  fraud_probability DECIMAL(12,10) DEFAULT NULL COMMENT '保留旧列名；实际含义必须看probability_target，不能默认舞弊',
  probability_target VARCHAR(128) DEFAULT NULL COMMENT '训练标签定义与阳性类别，须经确认',
  risk_level VARCHAR(16) DEFAULT NULL COMMENT '可选审阅标签，不是最终融合灯；无批准规则时NULL',
  model_version VARCHAR(64) DEFAULT NULL,
  rule_version VARCHAR(64) NOT NULL,
  result_snapshot JSON DEFAULT NULL,
  error_code VARCHAR(64) DEFAULT NULL,
  error_message TEXT DEFAULT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_rf_operation (tenant_id,company_id,run_id,record_key),
  KEY idx_rf_company_date (tenant_id,company_id,eval_date),
  KEY idx_rf_run (tenant_id,company_id,run_id),
  CONSTRAINT ck_rf_identity CHECK (CHAR_LENGTH(TRIM(tenant_id))>0 AND CHAR_LENGTH(TRIM(company_id))>0 AND CHAR_LENGTH(TRIM(run_id))>0 AND CHAR_LENGTH(TRIM(record_key))>0 AND CHAR_LENGTH(TRIM(request_id))>0),
  CONSTRAINT ck_rf_hash CHECK (payload_sha256 REGEXP '^[0-9a-f]{64}$'),
  CONSTRAINT ck_rf_status CHECK (execution_status IN ('success','skipped','insufficient_data','error')),
  CONSTRAINT ck_rf_probability CHECK (fraud_probability IS NULL OR fraud_probability BETWEEN 0 AND 1),
  CONSTRAINT ck_rf_level CHECK (risk_level IS NULL OR risk_level IN ('绿','黄','红')),
  CONSTRAINT ck_rf_features CHECK ((features IS NULL OR JSON_TYPE(features)='ARRAY') AND (feature_names IS NULL OR JSON_TYPE(feature_names)='ARRAY')),
  CONSTRAINT ck_rf_feature_pair CHECK (features IS NULL OR feature_names IS NULL OR JSON_LENGTH(features)=JSON_LENGTH(feature_names)),
  CONSTRAINT ck_rf_success CHECK (execution_status<>'success' OR (features IS NOT NULL AND feature_names IS NOT NULL AND JSON_LENGTH(features)=30 AND JSON_LENGTH(feature_names)=30 AND feature_schema_version IS NOT NULL AND CHAR_LENGTH(TRIM(feature_schema_version))>0 AND preprocess_version IS NOT NULL AND CHAR_LENGTH(TRIM(preprocess_version))>0 AND fraud_probability IS NOT NULL AND probability_target IS NOT NULL AND CHAR_LENGTH(TRIM(probability_target))>0 AND model_version IS NOT NULL AND CHAR_LENGTH(TRIM(model_version))>0 AND result_snapshot IS NOT NULL)),
  CONSTRAINT ck_rf_uncomputed CHECK (execution_status='success' OR (fraud_probability IS NULL AND risk_level IS NULL)),
  CONSTRAINT ck_rf_error CHECK (execution_status<>'error' OR (error_code IS NOT NULL AND CHAR_LENGTH(TRIM(error_code))>0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='模型日志v2：随机森林目标概率与有序特征';

-- 验证必须检查SHOW CREATE TABLE及约束，不只看表名：
SHOW CREATE TABLE quality_log;
SHOW CREATE TABLE deada_score_log;
SHOW CREATE TABLE rf_predict_log;
-- 后续接口联调还需验证：同键重试不重复、同键异内容拒绝、跨租户拒绝、
--   数据库提交失败时不得返回success、完整run_id链路及UTC会话设置。

-- =============================================================================
-- v8 风控工作台 · MySQL 表结构（唯一权威定义）
-- 文件：sql/v8_schema.sql        编码：utf8mb4 / utf8mb4_bin
-- 执行：mysql -h 127.0.0.1 -P <端口> -u root -p < sql/v8_schema.sql
-- 说明：建表 + 最小权限授权。应用账号只给 SELECT/INSERT/UPDATE，不给 DDL，
--       也不给 DELETE（留痕只增不改，保证可审计）。
-- =============================================================================

-- 【待替换项 TODO】按你的服务器环境替换下面三项后再执行：
--   <DB_NAME>  生产库名，禁止使用 cfqa_ 前缀
--   <APP_USER> 应用账号
--   <APP_PASS> 应用账号口令
SET NAMES utf8mb4;

-- -----------------------------------------------------------------------------
-- 1) 评估结果留痕（risk.assess_v8 / cashflow 结果归档）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mfg_v8_risk_log_v1 (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  request_id    VARCHAR(64) NOT NULL,
  industry      VARCHAR(32) NOT NULL,
  scenario      VARCHAR(32) NOT NULL,
  q_score       DECIMAL(8,4),
  d_score       DECIMAL(12,6),
  p_score       DECIMAL(8,4),
  level         VARCHAR(8),
  buffer_ratio  DECIMAL(12,6),
  worst_scene   VARCHAR(32),
  payload_json  JSON,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_request (request_id),
  INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='v8 评估结果留痕';

-- -----------------------------------------------------------------------------
-- 2) 通用留痕归档（模块四：操作时间 / 操作人 / 变更前后的值）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mfg_v8_audit_log_v1 (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_id      VARCHAR(64) NOT NULL,
  occurred_at   DATETIME(3) NOT NULL,
  actor         VARCHAR(64) NOT NULL,
  actor_role    VARCHAR(32),
  action        VARCHAR(64) NOT NULL,
  object_type   VARCHAR(64) NOT NULL,
  object_id     VARCHAR(128),
  field_name    VARCHAR(64),
  before_value  TEXT,
  after_value   TEXT,
  reason        VARCHAR(255),
  request_id    VARCHAR(64),
  batch_id      VARCHAR(64),
  source_ip     VARCHAR(45),
  payload_json  JSON,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_event (event_id),
  INDEX idx_object (object_type, object_id),
  INDEX idx_actor (actor),
  INDEX idx_action (action),
  INDEX idx_occurred (occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='留痕归档：谁在何时把什么从什么改成了什么';

-- -----------------------------------------------------------------------------
-- 3) 三态追踪（模块二：当前状态 + 流转历史）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mfg_v8_tracking_v1 (
  id                BIGINT AUTO_INCREMENT PRIMARY KEY,
  object_ref        VARCHAR(128) NOT NULL,
  counterparty      VARCHAR(128),
  receivable_amount DECIMAL(18,2) NOT NULL DEFAULT 0,
  verified_amount   DECIMAL(18,2) NOT NULL DEFAULT 0,
  due_date          DATE,
  state             VARCHAR(16) NOT NULL COMMENT '已化解|观察中|已恶化',
  verified_ratio    DECIMAL(9,6),
  overdue_days      INT,
  as_of             DATE,
  lamp              VARCHAR(8) COMMENT '绿灯|黄灯|红灯',
  history_json      JSON COMMENT '状态流转历史数组',
  updated_by        VARCHAR(64),
  updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_object (object_ref),
  INDEX idx_state (state),
  INDEX idx_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='三态追踪：到账验证与状态流转';

-- -----------------------------------------------------------------------------
-- 4) 数据清洗批次（模块六：batch_id / 是否通过 / 是否卡住 / 异常条数）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mfg_v8_clean_batch_v1 (
  batch_id       VARCHAR(64) NOT NULL PRIMARY KEY,
  source_ref     VARCHAR(128) NOT NULL,
  period         VARCHAR(32),
  ruleset_ver    VARCHAR(32) NOT NULL,
  total_items    INT NOT NULL DEFAULT 0,
  passed_items   INT NOT NULL DEFAULT 0,
  anomaly_count  INT NOT NULL DEFAULT 0,
  error_count    INT NOT NULL DEFAULT 0,
  blocked        TINYINT(1) NOT NULL DEFAULT 0 COMMENT '1=卡住，不得进入计算环节',
  duration_ms    INT,
  operator       VARCHAR(64),
  remark         VARCHAR(255),
  started_at     DATETIME(3),
  finished_at    DATETIME(3),
  created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_source (source_ref),
  INDEX idx_finished (finished_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='清洗批次结论';

-- -----------------------------------------------------------------------------
-- 5) 数据清洗明细（每条规则命中）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mfg_v8_clean_item_v1 (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  batch_id     VARCHAR(64) NOT NULL,
  seq          INT NOT NULL,
  sheet_name   VARCHAR(64),
  row_no       INT,
  object_ref   VARCHAR(128),
  rule_code    VARCHAR(16) NOT NULL,
  rule_name    VARCHAR(64) NOT NULL,
  severity     VARCHAR(8) NOT NULL COMMENT 'ERROR=阻断 WARN=提示',
  passed       TINYINT(1) NOT NULL DEFAULT 1,
  blocked      TINYINT(1) NOT NULL DEFAULT 0,
  message      VARCHAR(255),
  before_value VARCHAR(255),
  after_value  VARCHAR(255),
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_batch (batch_id),
  INDEX idx_rule (rule_code),
  INDEX idx_passed (passed)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='清洗规则命中明细';

-- -----------------------------------------------------------------------------
-- 6) 最小权限授权（按需替换 <DB_NAME>/<APP_USER>/<APP_PASS>）
--    留痕表只给 SELECT/INSERT/UPDATE，不给 DELETE 与 DDL。
-- -----------------------------------------------------------------------------
-- CREATE USER IF NOT EXISTS '<APP_USER>'@'127.0.0.1' IDENTIFIED BY '<APP_PASS>';
-- GRANT SELECT, INSERT, UPDATE ON `<DB_NAME>`.* TO '<APP_USER>'@'127.0.0.1';
-- FLUSH PRIVILEGES;

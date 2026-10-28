-- =============================================================================
-- 责任机制（Accountability）数据模型  ·  v11
-- 库名：生产环境请替换 cfqa_workbench（前缀不得为 cfqa_，cfqa_ 仅表示 QA 隔离库）
-- 执行：mysql -u root -p < sql/v8_accountability_schema.sql
-- 说明：本文件是责任机制 5 张表的唯一权威定义；v8/db.py 中的 DDL 与之保持一致。
-- =============================================================================

SET NAMES utf8mb4;

-- -----------------------------------------------------------------------------
-- 1. 风险分级 × 责任人映射矩阵（支持后续维护调整）
--    一级负责人 primary_owner 不在岗/超时 → 替补 deputy_owner → 再升级 escalate_to
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mfg_v8_owner_matrix_v1 (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  level_code          VARCHAR(16) NOT NULL COMMENT 'HIGH|MID|LOW',
  level_name          VARCHAR(32) NOT NULL COMMENT '高风险|中风险|低风险',
  primary_owner       VARCHAR(64) NOT NULL COMMENT '核实负责人（主）',
  primary_owner_id    VARCHAR(64) COMMENT '钉钉 userId / unionId',
  deputy_owner        VARCHAR(64) COMMENT '替补负责人',
  deputy_owner_id     VARCHAR(64),
  escalate_to         VARCHAR(64) COMMENT '升级对象（上级）',
  escalate_to_id      VARCHAR(64),
  review_sla_hours    INT NOT NULL DEFAULT 24  COMMENT '核实完成时限（小时）',
  confirm_sla_hours   INT NOT NULL DEFAULT 24  COMMENT '确认书回执时限（小时）',
  urge_interval_hours INT NOT NULL DEFAULT 12  COMMENT '催办间隔（小时）',
  max_urge_times      INT NOT NULL DEFAULT 3   COMMENT '最多催办次数，超出即升级',
  enabled             TINYINT(1) NOT NULL DEFAULT 1,
  remark              VARCHAR(255),
  updated_by          VARCHAR(64),
  updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_level (level_code),
  INDEX idx_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='风险分级与责任人映射矩阵';

-- 默认映射（可直接改，也可通过接口维护）
INSERT INTO mfg_v8_owner_matrix_v1
  (level_code, level_name, primary_owner, deputy_owner, escalate_to,
   review_sla_hours, confirm_sla_hours, urge_interval_hours, max_urge_times, remark)
VALUES
  ('HIGH','高风险','财务总监-张伟','风控经理-李娜','总经理-王强', 8, 12, 4, 3, '红灯事项：8小时内完成核实，超时3次催办后升级总经理'),
  ('MID' ,'中风险','风控经理-李娜','财务主管-陈静','财务总监-张伟',24, 24,12, 3, '黄灯事项：24小时内完成核实'),
  ('LOW' ,'低风险','财务主管-陈静','会计-刘洋','风控经理-李娜',48, 48,24, 2, '绿灯事项：48小时内完成核实')
ON DUPLICATE KEY UPDATE level_name=VALUES(level_name);

-- -----------------------------------------------------------------------------
-- 2. 事项主表（责任台账核心：谁的事、谁在办、办到哪一步、超没超时）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mfg_v8_matter_v1 (
  id               BIGINT AUTO_INCREMENT PRIMARY KEY,
  matter_id        VARCHAR(64)  NOT NULL COMMENT '事项编号 MAT-yyyymmdd-xxxx',
  title            VARCHAR(255) NOT NULL,
  description      TEXT,
  risk_level       VARCHAR(16)  NOT NULL COMMENT 'HIGH|MID|LOW',
  risk_level_name  VARCHAR(32),
  risk_score       DECIMAL(12,6) COMMENT '风险分值（最终等级对应分值）',
  lamp             VARCHAR(8)   COMMENT '红灯|黄灯|绿灯',
  source           VARCHAR(64)  COMMENT '来源：risk.assess_v8 / tracking.evaluate / clean.run_batch / manual',
  source_ref       VARCHAR(128) COMMENT '来源对象编号（如 SYN-AR-001）',
  request_id       VARCHAR(64),
  industry         VARCHAR(32),
  amount           DECIMAL(18,2),
  state            VARCHAR(24)  NOT NULL COMMENT 'SUBMITTED|ASSIGNED|IN_REVIEW|PENDING_CONFIRM|URGING|ESCALATED|CONFIRMED|REJECTED|CLOSED',
  round            INT NOT NULL DEFAULT 1 COMMENT '核实轮次（驳回重流转 +1）',
  current_owner    VARCHAR(64)  COMMENT '当前责任人',
  current_owner_id VARCHAR(64),
  owner_role       VARCHAR(16)  COMMENT 'primary|deputy|escalate',
  assign_reason    VARCHAR(255) COMMENT '分派依据（为何派给他）',
  due_at           DATETIME(3)  COMMENT '核实截止',
  confirm_due_at   DATETIME(3)  COMMENT '确认回执截止',
  urge_count       INT NOT NULL DEFAULT 0,
  last_urge_at     DATETIME(3),
  confirmed_at     DATETIME(3),
  confirm_result   VARCHAR(16)  COMMENT 'confirmed|rejected',
  created_by       VARCHAR(64),
  closed_reason    VARCHAR(255),
  payload_json     JSON,
  created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_matter (matter_id),
  INDEX idx_state  (state),
  INDEX idx_owner  (current_owner),
  INDEX idx_level  (risk_level),
  INDEX idx_created(created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='责任台账·事项主表';

-- -----------------------------------------------------------------------------
-- 3. 核实记录（每一轮一次，完整记录核实人/时间/意见/结论）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mfg_v8_review_v1 (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  review_id     VARCHAR(64) NOT NULL COMMENT 'REV-xxxx',
  matter_id     VARCHAR(64) NOT NULL,
  round         INT NOT NULL DEFAULT 1,
  reviewer      VARCHAR(64) NOT NULL COMMENT '核实人',
  reviewer_role VARCHAR(16) COMMENT 'primary|deputy|escalate',
  review_at     DATETIME(3) NOT NULL COMMENT '核实时间',
  opinion       VARCHAR(1000) COMMENT '核实意见',
  conclusion    VARCHAR(32) NOT NULL COMMENT '属实|不属实|部分属实|需补充材料',
  next_action   VARCHAR(64) COMMENT '通过|驳回重核实|补充后重申',
  evidence_json JSON COMMENT '核实依据（指标/原始单据/凭证号）',
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_review (review_id),
  INDEX idx_matter (matter_id),
  INDEX idx_reviewer (reviewer)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='责任台账·核实记录';

-- -----------------------------------------------------------------------------
-- 4. 钉钉确认书（核实完成后发送，接收人可在线确认或驳回，状态实时回写）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mfg_v8_confirm_v1 (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  confirm_id      VARCHAR(64) NOT NULL COMMENT 'CFM-xxxx',
  matter_id       VARCHAR(64) NOT NULL,
  round           INT NOT NULL DEFAULT 1,
  channel         VARCHAR(16) NOT NULL DEFAULT 'dingtalk',
  recipients_json JSON COMMENT '接收人列表（核实人+相关责任人）',
  message_id      VARCHAR(128) COMMENT '钉钉消息 ID（dry-run 时为模拟值）',
  sent_at         DATETIME(3),
  status          VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending|confirmed|rejected|expired',
  confirm_url     VARCHAR(512) COMMENT '确认入口',
  token           VARCHAR(128) COMMENT '一次性签名令牌',
  expire_at       DATETIME(3),
  confirmed_by    VARCHAR(64),
  confirmed_by_id VARCHAR(64),
  confirmed_at    DATETIME(3),
  comment         VARCHAR(1000) COMMENT '确认/驳回意见',
  payload_json    JSON COMMENT '确认书正文快照（风险等级/事项描述/核实结论）',
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_confirm (confirm_id),
  INDEX idx_matter (matter_id),
  INDEX idx_status (status),
  INDEX idx_token  (token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='钉钉确认书';

-- -----------------------------------------------------------------------------
-- 5. 催办记录（超时未确认自动发送，逐次留痕）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mfg_v8_urge_v1 (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  urge_id         VARCHAR(64) NOT NULL COMMENT 'URG-xxxx',
  matter_id       VARCHAR(64) NOT NULL,
  confirm_id      VARCHAR(64),
  round           INT NOT NULL DEFAULT 1,
  seq             INT NOT NULL DEFAULT 1 COMMENT '第几次催办',
  channel         VARCHAR(16) DEFAULT 'dingtalk',
  recipients_json JSON,
  message_id      VARCHAR(128),
  sent_at         DATETIME(3),
  trigger_reason  VARCHAR(64) COMMENT 'confirm_timeout|manual',
  status          VARCHAR(16) DEFAULT 'sent',
  payload_json    JSON,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_urge (urge_id),
  INDEX idx_matter (matter_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='催办记录';

-- -----------------------------------------------------------------------------
-- 授权（生产环境请按实际账号调整；应用账号仅需 SELECT/INSERT/UPDATE）
-- 【待替换项】cf_app@'127.0.0.1' → 自有服务器应用账号与来源主机
-- -----------------------------------------------------------------------------
-- GRANT SELECT, INSERT, UPDATE ON cfqa_workbench.mfg_v8_owner_matrix_v1 TO 'cf_app'@'127.0.0.1';
-- GRANT SELECT, INSERT, UPDATE ON cfqa_workbench.mfg_v8_matter_v1       TO 'cf_app'@'127.0.0.1';
-- GRANT SELECT, INSERT, UPDATE ON cfqa_workbench.mfg_v8_review_v1       TO 'cf_app'@'127.0.0.1';
-- GRANT SELECT, INSERT, UPDATE ON cfqa_workbench.mfg_v8_confirm_v1      TO 'cf_app'@'127.0.0.1';
-- GRANT SELECT, INSERT, UPDATE ON cfqa_workbench.mfg_v8_urge_v1         TO 'cf_app'@'127.0.0.1';

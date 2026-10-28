-- =============================================================================
-- 常驻 agent 服务初始化 SQL
-- 执行方式（root 本机免密）：  mysql -u root < init_agent.sql
-- 执行账号：root（auth_socket，仅本机）。口令由 install.sh 生成后替换占位符。
--
-- 最小权限原则：
--   cf_agent 只有 cf_workbench.* 的 SELECT / INSERT。
--   —— 没有 UPDATE / DELETE / DROP / CREATE，因此本服务无法改历史、无法删数据、无法建对象。
--   —— 状态推进走"追加新行"的不可变事件模型，而不是原地 UPDATE。
-- 回滚：DROP USER 'cf_agent'@'localhost'; 会连带失效其权限；表数据默认保留。
-- =============================================================================

-- ---------- 1. 专用服务账号 ----------
CREATE USER IF NOT EXISTS 'cf_agent'@'localhost'
  IDENTIFIED BY '__CF_AGENT_PASSWORD__';

-- 只读 + 只追加，够用即可
GRANT SELECT, INSERT ON `cf_workbench`.* TO 'cf_agent'@'localhost';

-- ---------- 2. 业务表（全部追加写，不 UPDATE / 不 DELETE）----------
USE `cf_workbench`;

-- 2.1 巡检主记录
CREATE TABLE IF NOT EXISTS `agent_inspection` (
  `id`            BIGINT AUTO_INCREMENT PRIMARY KEY,
  `inspection_id` VARCHAR(64)  NOT NULL,
  `trigger_type`  VARCHAR(24)  NOT NULL COMMENT 'schedule / api / chat',
  `started_at`    DATETIME(3)  NOT NULL,
  `finished_at`   DATETIME(3)  NOT NULL,
  `level`         VARCHAR(8)   NOT NULL COMMENT 'low / medium / high',
  `item_count`    INT          NOT NULL DEFAULT 0,
  `failed_count`  INT          NOT NULL DEFAULT 0,
  `metrics_json`  JSON,
  `created_at`    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `uk_inspection` (`inspection_id`),
  INDEX `idx_created` (`created_at`),
  INDEX `idx_level` (`level`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 2.2 巡检明细
CREATE TABLE IF NOT EXISTS `agent_inspection_item` (
  `id`            BIGINT AUTO_INCREMENT PRIMARY KEY,
  `inspection_id` VARCHAR(64) NOT NULL,
  `seq`           INT         NOT NULL,
  `item_code`     VARCHAR(64) NOT NULL,
  `item_name`     VARCHAR(128) NOT NULL,
  `passed`        TINYINT(1)  NOT NULL DEFAULT 1,
  `level`         VARCHAR(8),
  `detail`        VARCHAR(1000),
  `created_at`    TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
  INDEX `idx_inspection` (`inspection_id`),
  INDEX `idx_code` (`item_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 2.3 告警留痕（含"未配置通道"的 not_configured 记录，不谎报送达）
CREATE TABLE IF NOT EXISTS `agent_alert` (
  `id`            BIGINT AUTO_INCREMENT PRIMARY KEY,
  `alert_id`      VARCHAR(64) NOT NULL,
  `inspection_id` VARCHAR(64),
  `level`         VARCHAR(8)  NOT NULL,
  `channel`       VARCHAR(32),
  `status`        VARCHAR(32) NOT NULL COMMENT 'sent / failed / not_configured',
  `sent`          TINYINT(1)  NOT NULL DEFAULT 0,
  `detail`        VARCHAR(2000),
  `created_at`    TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `uk_alert` (`alert_id`),
  INDEX `idx_inspection` (`inspection_id`),
  INDEX `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 2.4 周报留痕
CREATE TABLE IF NOT EXISTS `agent_report` (
  `id`           BIGINT AUTO_INCREMENT PRIMARY KEY,
  `report_id`    VARCHAR(64) NOT NULL,
  `period_start` DATETIME    NOT NULL,
  `period_end`   DATETIME    NOT NULL,
  `content_json` JSON,
  `markdown`     MEDIUMTEXT,
  `channel`      VARCHAR(32),
  `status`       VARCHAR(32) NOT NULL COMMENT 'sent / failed / not_pushed / not_configured',
  `sent`         TINYINT(1)  NOT NULL DEFAULT 0,
  `created_at`   TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `uk_report` (`report_id`),
  INDEX `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 2.5 对话留痕
CREATE TABLE IF NOT EXISTS `agent_chat_log` (
  `id`          BIGINT AUTO_INCREMENT PRIMARY KEY,
  `session_id`  VARCHAR(64) NOT NULL,
  `intent`      VARCHAR(32),
  `question`    VARCHAR(2000),
  `answer`      MEDIUMTEXT,
  `duration_ms` INT,
  `created_at`  TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
  INDEX `idx_session` (`session_id`),
  INDEX `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 2.6 决策卡片留痕
CREATE TABLE IF NOT EXISTS `agent_card` (
  `id`           BIGINT AUTO_INCREMENT PRIMARY KEY,
  `card_id`      VARCHAR(64) NOT NULL,
  `level`        VARCHAR(8)  NOT NULL,
  `trigger_type` VARCHAR(24),
  `content_json` JSON,
  `created_at`   TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `uk_card` (`card_id`),
  INDEX `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

FLUSH PRIVILEGES;

-- ---------- 3. 自检（只读）----------
SELECT 'agent_inspection' AS t, COUNT(*) AS rows FROM `agent_inspection`
UNION ALL SELECT 'agent_inspection_item', COUNT(*) FROM `agent_inspection_item`
UNION ALL SELECT 'agent_alert', COUNT(*) FROM `agent_alert`
UNION ALL SELECT 'agent_report', COUNT(*) FROM `agent_report`
UNION ALL SELECT 'agent_chat_log', COUNT(*) FROM `agent_chat_log`
UNION ALL SELECT 'agent_card', COUNT(*) FROM `agent_card`;

SHOW GRANTS FOR 'cf_agent'@'localhost';

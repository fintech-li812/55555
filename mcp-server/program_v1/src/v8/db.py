# -*- coding: utf-8 -*-
"""v8 数据访问层：集中管理数据库配置、连接与建表（幂等、容错）。

============================================================================
【待替换项 TODO —— 部署到自有服务器时集中替换这里，不要在业务代码里散落配置】
----------------------------------------------------------------------------
  CF_DB_HOST        默认 127.0.0.1        → 自有服务器 MySQL 地址
  CF_DB_PORT        默认 3307             → 自有服务器 MySQL 端口（生产常为 3306）
  CF_DB_USER        默认 cf_app           → 专用应用账号（禁止用 root）
  CF_DB_PASSWORD    默认 空               → 【必填】应用账号口令
  CF_DB_NAME        默认 cfqa_workbench   → 【生产库名不得为 cfqa_ 前缀】
  CF_ADMIN_DSN      默认 空               → 首次建表用的管理员连接串（仅初始化用）
  CF_MCP_TOKEN      默认 空               → MCP 鉴权令牌（必填，见 v8_mcp_server.py）
  CF_MCP_PORT       默认 8090
  CF_AUDIT_ACTOR    默认 system           → 留痕的默认操作人
============================================================================
本模块只做连接与建表，不含任何业务判断；业务判断见各业务模块。
建表语句的唯一权威定义在 sql/v8_schema.sql，此处 DDL 与之保持一致。
"""
from __future__ import annotations

import json
import os
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))           # .../program_v1/src
PROGRAM_V1 = os.path.dirname(SRC)                                            # .../program_v1
ROOT = os.path.dirname(os.path.dirname(PROGRAM_V1))                          # CashflowRiskWorkbench
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# ---- 表名（与 sql/v8_schema.sql 一致）----
T_RISK_LOG = "mfg_v8_risk_log_v1"        # 评估结果留痕
T_AUDIT = "mfg_v8_audit_log_v1"          # 通用留痕归档（变更前/后值）
T_TRACK = "mfg_v8_tracking_v1"           # 三态当前状态与流转历史
T_CLEAN_BATCH = "mfg_v8_clean_batch_v1"  # 清洗批次
T_CLEAN_ITEM = "mfg_v8_clean_item_v1"    # 清洗明细

# ---- 责任机制（v11）表名，与 sql/v8_accountability_schema.sql 一致 ----
T_OWNER = "mfg_v8_owner_matrix_v1"   # 风险分级 × 责任人映射矩阵
T_MATTER = "mfg_v8_matter_v1"        # 事项主表（责任台账）
T_REVIEW = "mfg_v8_review_v1"        # 核实记录
T_CONFIRM = "mfg_v8_confirm_v1"      # 钉钉确认书
T_URGE = "mfg_v8_urge_v1"            # 催办记录

DDL = {
    T_RISK_LOG: """
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",

    T_AUDIT: """
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",

    T_TRACK: """
CREATE TABLE IF NOT EXISTS mfg_v8_tracking_v1 (
  id                BIGINT AUTO_INCREMENT PRIMARY KEY,
  object_ref        VARCHAR(128) NOT NULL,
  counterparty      VARCHAR(128),
  receivable_amount DECIMAL(18,2) NOT NULL DEFAULT 0,
  verified_amount   DECIMAL(18,2) NOT NULL DEFAULT 0,
  due_date          DATE,
  state             VARCHAR(16) NOT NULL,
  verified_ratio    DECIMAL(9,6),
  overdue_days      INT,
  as_of             DATE,
  lamp              VARCHAR(8),
  history_json      JSON,
  updated_by        VARCHAR(64),
  updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_object (object_ref),
  INDEX idx_state (state),
  INDEX idx_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",

    T_CLEAN_BATCH: """
CREATE TABLE IF NOT EXISTS mfg_v8_clean_batch_v1 (
  batch_id       VARCHAR(64) NOT NULL PRIMARY KEY,
  source_ref     VARCHAR(128) NOT NULL,
  period         VARCHAR(32),
  ruleset_ver    VARCHAR(32) NOT NULL,
  total_items    INT NOT NULL DEFAULT 0,
  passed_items   INT NOT NULL DEFAULT 0,
  anomaly_count  INT NOT NULL DEFAULT 0,
  error_count    INT NOT NULL DEFAULT 0,
  blocked        TINYINT(1) NOT NULL DEFAULT 0,
  duration_ms    INT,
  operator       VARCHAR(64),
  remark         VARCHAR(255),
  started_at     DATETIME(3),
  finished_at    DATETIME(3),
  created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_source (source_ref),
  INDEX idx_finished (finished_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",

    T_CLEAN_ITEM: """
CREATE TABLE IF NOT EXISTS mfg_v8_clean_item_v1 (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  batch_id     VARCHAR(64) NOT NULL,
  seq          INT NOT NULL,
  sheet_name   VARCHAR(64),
  row_no       INT,
  object_ref   VARCHAR(128),
  rule_code    VARCHAR(16) NOT NULL,
  rule_name    VARCHAR(64) NOT NULL,
  severity     VARCHAR(8) NOT NULL,
  passed       TINYINT(1) NOT NULL DEFAULT 1,
  blocked      TINYINT(1) NOT NULL DEFAULT 0,
  message      VARCHAR(255),
  before_value VARCHAR(255),
  after_value  VARCHAR(255),
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_batch (batch_id),
  INDEX idx_rule (rule_code),
  INDEX idx_passed (passed)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",

    T_OWNER: """
CREATE TABLE IF NOT EXISTS mfg_v8_owner_matrix_v1 (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  level_code          VARCHAR(16) NOT NULL,
  level_name          VARCHAR(32) NOT NULL,
  primary_owner       VARCHAR(64) NOT NULL,
  primary_owner_id    VARCHAR(64),
  deputy_owner        VARCHAR(64),
  deputy_owner_id     VARCHAR(64),
  escalate_to         VARCHAR(64),
  escalate_to_id      VARCHAR(64),
  review_sla_hours    INT NOT NULL DEFAULT 24,
  confirm_sla_hours   INT NOT NULL DEFAULT 24,
  urge_interval_hours INT NOT NULL DEFAULT 12,
  max_urge_times      INT NOT NULL DEFAULT 3,
  enabled             TINYINT(1) NOT NULL DEFAULT 1,
  remark              VARCHAR(255),
  updated_by          VARCHAR(64),
  updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_level (level_code),
  INDEX idx_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",

    T_MATTER: """
CREATE TABLE IF NOT EXISTS mfg_v8_matter_v1 (
  id               BIGINT AUTO_INCREMENT PRIMARY KEY,
  matter_id        VARCHAR(64)  NOT NULL,
  title            VARCHAR(255) NOT NULL,
  description      TEXT,
  risk_level       VARCHAR(16)  NOT NULL,
  risk_level_name  VARCHAR(32),
  risk_score       DECIMAL(12,6),
  lamp             VARCHAR(8),
  source           VARCHAR(64),
  source_ref       VARCHAR(128),
  request_id       VARCHAR(64),
  industry         VARCHAR(32),
  amount           DECIMAL(18,2),
  state            VARCHAR(24)  NOT NULL,
  round            INT NOT NULL DEFAULT 1,
  current_owner    VARCHAR(64),
  current_owner_id VARCHAR(64),
  owner_role       VARCHAR(16),
  assign_reason    VARCHAR(255),
  due_at           DATETIME(3),
  confirm_due_at   DATETIME(3),
  urge_count       INT NOT NULL DEFAULT 0,
  last_urge_at     DATETIME(3),
  confirmed_at     DATETIME(3),
  confirm_result   VARCHAR(16),
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",

    T_REVIEW: """
CREATE TABLE IF NOT EXISTS mfg_v8_review_v1 (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  review_id     VARCHAR(64) NOT NULL,
  matter_id     VARCHAR(64) NOT NULL,
  round         INT NOT NULL DEFAULT 1,
  reviewer      VARCHAR(64) NOT NULL,
  reviewer_role VARCHAR(16),
  review_at     DATETIME(3) NOT NULL,
  opinion       VARCHAR(1000),
  conclusion    VARCHAR(32) NOT NULL,
  next_action   VARCHAR(64),
  evidence_json JSON,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_review (review_id),
  INDEX idx_matter (matter_id),
  INDEX idx_reviewer (reviewer)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",

    T_CONFIRM: """
CREATE TABLE IF NOT EXISTS mfg_v8_confirm_v1 (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  confirm_id      VARCHAR(64) NOT NULL,
  matter_id       VARCHAR(64) NOT NULL,
  round           INT NOT NULL DEFAULT 1,
  channel         VARCHAR(16) NOT NULL DEFAULT 'dingtalk',
  recipients_json JSON,
  message_id      VARCHAR(128),
  sent_at         DATETIME(3),
  status          VARCHAR(16) NOT NULL DEFAULT 'pending',
  confirm_url     VARCHAR(512),
  token           VARCHAR(128),
  expire_at       DATETIME(3),
  confirmed_by    VARCHAR(64),
  confirmed_by_id VARCHAR(64),
  confirmed_at    DATETIME(3),
  comment         VARCHAR(1000),
  payload_json    JSON,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_confirm (confirm_id),
  INDEX idx_matter (matter_id),
  INDEX idx_status (status),
  INDEX idx_token  (token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",

    T_URGE: """
CREATE TABLE IF NOT EXISTS mfg_v8_urge_v1 (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  urge_id         VARCHAR(64) NOT NULL,
  matter_id       VARCHAR(64) NOT NULL,
  confirm_id      VARCHAR(64),
  round           INT NOT NULL DEFAULT 1,
  seq             INT NOT NULL DEFAULT 1,
  channel         VARCHAR(16) DEFAULT 'dingtalk',
  recipients_json JSON,
  message_id      VARCHAR(128),
  sent_at         DATETIME(3),
  trigger_reason  VARCHAR(64),
  status          VARCHAR(16) DEFAULT 'sent',
  payload_json    JSON,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_urge (urge_id),
  INDEX idx_matter (matter_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
}


def _load_env() -> dict:
    """读取 deploy/.env（不打印任何口令）。"""
    env = {}
    for path in (os.path.join(ROOT, "deploy", ".env"),
                 os.path.join(ROOT, "deploy", "v8", "v8.env")):
        if not os.path.exists(path):
            continue
        try:
            for line in open(path, encoding="utf-8").read().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
        except Exception:
            pass
    return env


ENV = _load_env()


def _cfg(key: str, default: str = "") -> str:
    return os.environ.get(key) or ENV.get(key) or default


def db_conf() -> dict:
    return {
        "host": _cfg("CF_DB_HOST", "127.0.0.1"),
        "port": int(_cfg("CF_DB_PORT", "3307") or 3307),
        "user": _cfg("CF_DB_USER", "cf_app"),
        "password": _cfg("CF_DB_PASSWORD", ""),
        "database": _cfg("CF_DB_NAME", "cfqa_workbench"),
        "charset": "utf8mb4",
    }


def connect():
    """建立连接；失败时抛出带原因的异常，不伪装成功。"""
    import pymysql
    c = db_conf()
    if not c["database"]:
        raise RuntimeError("未配置 CF_DB_NAME，无法连接数据库")
    return pymysql.connect(host=c["host"], port=c["port"], user=c["user"],
                           password=c["password"], database=c["database"],
                           charset="utf8mb4", connect_timeout=8)


def db_status() -> dict:
    """返回数据库可用性与库内表清单（供 /api/v8/health 使用）。"""
    try:
        c = connect()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT DATABASE(), VERSION()")
                dbname, version = cur.fetchone()
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=%s", (dbname,))
                tables = sorted(r[0] for r in cur.fetchall())
            conf = db_conf()
            return {"ok": True, "database": dbname, "version": version,
                    "host": conf["host"], "port": conf["port"],
                    "user": conf["user"], "tables": tables,
                    "missing_tables": [t for t in DDL if t not in tables]}
        finally:
            c.close()
    except Exception as e:
        conf = db_conf()
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "host": conf["host"], "port": conf["port"],
                "missing_tables": list(DDL)}


def ensure_tables(names=None) -> dict:
    """幂等建表。应用账号通常只有 SELECT/INSERT，无 CREATE 权限，
    此时如实返回 failed，由运维用管理员账号执行 sql/v8_schema.sql 预建。"""
    names = names or list(DDL)
    created, failed = [], []
    try:
        c = connect()
    except Exception as e:
        return {"created": [], "failed": {t: str(e) for t in names},
                "note": "连接失败，未执行建表"}
    try:
        with c.cursor() as cur:
            for t in names:
                try:
                    cur.execute(DDL[t])
                    created.append(t)
                except Exception as e:
                    failed.append({t: str(e)[:120]})
        c.commit()
    finally:
        c.close()
    return {"created": created, "failed": failed,
            "note": "CREATE 权限不足属正常现象，请用管理员账号执行 sql/v8_schema.sql"}


def jdumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)

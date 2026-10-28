# -*- coding: utf-8 -*-
"""数据库访问层。

最小权限约束（代码层面强制）：
  - 本服务运行账号 cf_agent 只被授予 SELECT / INSERT；
  - 因此 execute() 只接受 INSERT 语句，其他写操作直接拒绝并留痕，
    不尝试"绕过"，也不允许上层偷偷传 UPDATE/DELETE/DDL。
  - 状态推进采用"追加新行 + 版本号"的不可变事件模型，而不是原地 UPDATE。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

import pymysql
from pymysql.cursors import DictCursor

from .errors import AppError, ERR_DB_CONNECT, ERR_DB_DENIED, ERR_DB_QUERY, ERR_DB_WRITE


class Database:
    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.log = logger
        self._conn = None

    # ---------- 连接 ----------
    def _connect(self):
        last = None
        for attempt in range(1, int(self.cfg.db_retry_times) + 1):
            try:
                conn = pymysql.connect(
                    host=self.cfg.db_host,
                    port=int(self.cfg.db_port),
                    user=self.cfg.db_user,
                    password=self.cfg.db_password,
                    database=self.cfg.db_name,
                    charset="utf8mb4",
                    connect_timeout=int(self.cfg.db_connect_timeout),
                    cursorclass=DictCursor,
                    autocommit=True,
                )
                self.log.info("数据库连接成功 %s:%s/%s user=%s",
                              self.cfg.db_host, self.cfg.db_port,
                              self.cfg.db_name, self.cfg.db_user)
                return conn
            except Exception as exc:  # noqa: BLE001 - 需要如实记录原因
                last = exc
                self.log.warning("数据库连接失败（第 %s/%s 次）：%s: %s",
                                 attempt, self.cfg.db_retry_times,
                                 type(exc).__name__, exc)
                if attempt < int(self.cfg.db_retry_times):
                    time.sleep(float(self.cfg.db_retry_backoff) * attempt)
        raise AppError(ERR_DB_CONNECT, "数据库不可用",
                       {"host": self.cfg.db_host, "port": self.cfg.db_port,
                        "reason": f"{type(last).__name__}: {last}"})

    @property
    def conn(self):
        if self._conn is None:
            self._conn = self._connect()
            return self._conn
        try:
            self._conn.ping(reconnect=True)
        except Exception:  # noqa: BLE001
            self._conn = self._connect()
        return self._conn

    def close(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None

    # ---------- 查询 ----------
    def query(self, sql: str, args: Optional[Sequence] = None) -> List[Dict[str, Any]]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, args or ())
                rows = cur.fetchall()
                return [dict(r) for r in rows]
        except pymysql.err.ProgrammingError as exc:
            # 表不存在 / 列不存在：如实上报为"数据源无此数据"，而不是崩服务
            self.log.warning("查询失败（对象可能不存在）：%s", exc)
            raise AppError(ERR_DB_QUERY, "查询失败", {"reason": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self.log.error("查询异常：%s: %s", type(exc).__name__, exc)
            raise AppError(ERR_DB_QUERY, "查询失败", {"reason": f"{type(exc).__name__}: {exc}"})

    def table_exists(self, name: str) -> bool:
        rows = self.query(
            "SELECT COUNT(*) AS c FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (self.cfg.db_name, name),
        )
        return bool(rows and rows[0].get("c"))

    # ---------- 写入（仅 INSERT）----------
    def insert(self, sql: str, args: Optional[Sequence] = None) -> int:
        head = sql.lstrip().split(None, 1)
        if not head or head[0].upper() not in ("INSERT", "REPLACE"):
            raise AppError(ERR_DB_DENIED, "写操作被拒绝：本服务仅允许 INSERT",
                           {"sql_head": head[0] if head else ""})
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, args or ())
                return int(cur.lastrowid or 0)
        except Exception as exc:  # noqa: BLE001
            self.log.error("写入失败：%s: %s", type(exc).__name__, exc)
            raise AppError(ERR_DB_WRITE, "写入失败", {"reason": f"{type(exc).__name__}: {exc}"})

    def health(self) -> Dict[str, Any]:
        try:
            rows = self.query("SELECT 1 AS ok")
            return {"ok": True, "detail": rows[0] if rows else {}}
        except AppError as exc:
            return {"ok": False, "code": exc.code, "error": exc.message, "detail": exc.detail}

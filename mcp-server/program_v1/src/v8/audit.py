# -*- coding: utf-8 -*-
"""模块四 · 留痕归档：记录操作时间、操作人、变更前后的值，持久化并支持条件查询与审计。

设计要点：
1. 每一条留痕 = 一次「字段级」变更（field_name + before_value + after_value），
   保证能还原"谁在什么时候把什么从什么改成了什么"；
2. 数据库不可用时回落内存，并在返回中如实标注 persisted=False，不假装已归档；
3. 查询支持 对象 / 操作人 / 动作 / 时间区间 / 关键字 组合条件，供审计追溯。
"""
from __future__ import annotations

import datetime as _dt
import uuid

from . import db as _db

# 内存兜底（数据库不可用时使用，进程内可查，重启即失）
_MEM_LOG = []
MAX_MEM = 2000


def write(action: str, object_type: str, object_id: str = None,
          actor: str = None, actor_role: str = None,
          field_name: str = None, before_value=None, after_value=None,
          reason: str = None, request_id: str = None, batch_id: str = None,
          source_ip: str = None, payload: dict = None,
          occurred_at: str = None) -> dict:
    """写一条留痕。返回 {ok, event_id, persisted}。"""
    actor = actor or _db._cfg("CF_AUDIT_ACTOR", "system")
    now = occurred_at or _dt.datetime.now().isoformat(timespec="milliseconds")
    event_id = "aud-" + uuid.uuid4().hex[:12]
    rec = {
        "event_id": event_id, "occurred_at": now, "actor": actor,
        "actor_role": actor_role, "action": action, "object_type": object_type,
        "object_id": object_id, "field_name": field_name,
        "before_value": None if before_value is None else str(before_value),
        "after_value": None if after_value is None else str(after_value),
        "reason": reason, "request_id": request_id, "batch_id": batch_id,
        "source_ip": source_ip, "payload": payload or {},
    }
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO %s (event_id, occurred_at, actor, actor_role, action,"
                    " object_type, object_id, field_name, before_value, after_value,"
                    " reason, request_id, batch_id, source_ip, payload_json)"
                    " VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)"
                    % _db.T_AUDIT,
                    (event_id, now, actor, actor_role, action, object_type, object_id,
                     field_name, rec["before_value"], rec["after_value"], reason,
                     request_id, batch_id, source_ip, _db.jdumps(rec["payload"])))
            c.commit()
            return {"ok": True, "event_id": event_id, "persisted": True, "record": rec}
        finally:
            c.close()
    except Exception as e:
        _MEM_LOG.append(rec)
        if len(_MEM_LOG) > MAX_MEM:
            del _MEM_LOG[0]
        return {"ok": True, "event_id": event_id, "persisted": False,
                "error": "%s: %s" % (type(e).__name__, e), "record": rec}


def query(object_type: str = None, object_id: str = None, actor: str = None,
          action: str = None, since: str = None, until: str = None,
          keyword: str = None, limit: int = 50, offset: int = 0) -> dict:
    """条件查询留痕。"""
    where, args = [], []
    if object_type:
        where.append("object_type=%s"); args.append(object_type)
    if object_id:
        where.append("object_id=%s"); args.append(object_id)
    if actor:
        where.append("actor=%s"); args.append(actor)
    if action:
        where.append("action=%s"); args.append(action)
    if since:
        where.append("occurred_at >= %s"); args.append(since)
    if until:
        where.append("occurred_at <= %s"); args.append(until)
    if keyword:
        where.append("(reason LIKE %%s OR before_value LIKE %%s OR after_value LIKE %%s"
                     " OR object_id LIKE %%s)")
        args += ["%%%s%%" % keyword] * 4
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM %s%s" % (_db.T_AUDIT, sql_where), args)
                total = cur.fetchone()[0]
                cur.execute(
                    "SELECT event_id, occurred_at, actor, actor_role, action, object_type,"
                    " object_id, field_name, before_value, after_value, reason,"
                    " request_id, batch_id, source_ip FROM %s%s"
                    " ORDER BY occurred_at DESC, id DESC LIMIT %%s OFFSET %%s"
                    % (_db.T_AUDIT, sql_where), args + [int(limit), int(offset)])
                rows = cur.fetchall()
            return {"source": "mysql", "total": total, "limit": limit, "offset": offset,
                    "records": [{
                        "event_id": r[0], "occurred_at": str(r[1]), "actor": r[2],
                        "actor_role": r[3], "action": r[4], "object_type": r[5],
                        "object_id": r[6], "field_name": r[7], "before_value": r[8],
                        "after_value": r[9], "reason": r[10], "request_id": r[11],
                        "batch_id": r[12], "source_ip": r[13]} for r in rows]}
        finally:
            c.close()
    except Exception as e:
        recs = [r for r in _MEM_LOG
                if (not object_type or r["object_type"] == object_type)
                and (not object_id or r["object_id"] == object_id)
                and (not actor or r["actor"] == actor)
                and (not action or r["action"] == action)]
        recs = list(reversed(recs))[offset:offset + limit]
        return {"source": "memory", "total": len(_MEM_LOG), "error": str(e),
                "records": recs}

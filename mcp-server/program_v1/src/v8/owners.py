# -*- coding: utf-8 -*-
"""责任机制 · 模块一：风险分级与责任人映射。

设计要点：
1. 三级风险（HIGH/MID/LOW）分别配置：核实负责人 primary、替补人 deputy、
   升级对象 escalate，以及 SLA（核实时限 / 确认时限 / 催办间隔 / 最多催办次数）；
2. 映射优先读数据库，数据库不可用时回落内存默认值，并在返回中如实标注 source；
3. 支持运行时维护调整（upsert / 启停），每次变更写留痕（谁在何时把谁改成了谁）；
4. 灯色 ↔ 等级映射集中在此，避免业务代码里散落 if 红灯→高风险。

【待替换项】默认责任人仅为本机演示值，部署时须替换为真实人员与钉钉 userId。
"""
from __future__ import annotations

import datetime as _dt

from . import db as _db
from . import audit as _aud

# ---- 灯色 / 等级 映射（唯一权威定义）----
LAMP_TO_LEVEL = {"红灯": "HIGH", "黄灯": "MID", "绿灯": "LOW"}
LEVEL_TO_LAMP = {"HIGH": "红灯", "MID": "黄灯", "LOW": "绿灯"}
LEVEL_NAME = {"HIGH": "高风险", "MID": "中风险", "LOW": "低风险"}

# ---- 默认映射（数据库不可用时的兜底，与 sql/v8_accountability_schema.sql seed 一致）----
DEFAULTS = {
    "HIGH": {"level_code": "HIGH", "level_name": "高风险",
             "primary_owner": "财务总监-张伟", "primary_owner_id": "",
             "deputy_owner": "风控经理-李娜", "deputy_owner_id": "",
             "escalate_to": "总经理-王强", "escalate_to_id": "",
             "review_sla_hours": 8, "confirm_sla_hours": 12,
             "urge_interval_hours": 4, "max_urge_times": 3,
             "enabled": 1, "remark": "红灯事项：8小时内完成核实"},
    "MID": {"level_code": "MID", "level_name": "中风险",
            "primary_owner": "风控经理-李娜", "primary_owner_id": "",
            "deputy_owner": "财务主管-陈静", "deputy_owner_id": "",
            "escalate_to": "财务总监-张伟", "escalate_to_id": "",
            "review_sla_hours": 24, "confirm_sla_hours": 24,
            "urge_interval_hours": 12, "max_urge_times": 3,
            "enabled": 1, "remark": "黄灯事项：24小时内完成核实"},
    "LOW": {"level_code": "LOW", "level_name": "低风险",
            "primary_owner": "财务主管-陈静", "primary_owner_id": "",
            "deputy_owner": "会计-刘洋", "deputy_owner_id": "",
            "escalate_to": "风控经理-李娜", "escalate_to_id": "",
            "review_sla_hours": 48, "confirm_sla_hours": 48,
            "urge_interval_hours": 24, "max_urge_times": 2,
            "enabled": 1, "remark": "绿灯事项：48小时内完成核实"},
}

# 内存兜底副本（可被 upsert 修改，进程内生效）
_MEM = {k: dict(v) for k, v in DEFAULTS.items()}


def _norm_level(level) -> str:
    """把 '红灯' / 'HIGH' / 'high' / '高风险' 统一成 HIGH|MID|LOW。"""
    if not level:
        return "MID"
    s = str(level).strip().upper()
    if s in ("HIGH", "MID", "LOW"):
        return s
    if s in LAMP_TO_LEVEL.values():
        return s
    for lamp, code in LAMP_TO_LEVEL.items():
        if s == lamp or s == lamp[0]:
            return code
    if "高" in s or "红" in s or "HIGH" in s:
        return "HIGH"
    if "低" in s or "绿" in s or "LOW" in s:
        return "LOW"
    return "MID"


def level_of(lamp_or_level) -> str:
    """对外：由灯色或等级得到等级码。"""
    return _norm_level(lamp_or_level)


def get(level) -> dict:
    """取某等级的责任人配置。返回 dict，含 source=mysql|memory。"""
    code = _norm_level(level)
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT level_code, level_name, primary_owner, primary_owner_id,"
                    " deputy_owner, deputy_owner_id, escalate_to, escalate_to_id,"
                    " review_sla_hours, confirm_sla_hours, urge_interval_hours,"
                    " max_urge_times, enabled, remark FROM %s WHERE level_code=%%s"
                    % _db.T_OWNER, (code,))
                row = cur.fetchone()
            if row:
                keys = ["level_code", "level_name", "primary_owner", "primary_owner_id",
                        "deputy_owner", "deputy_owner_id", "escalate_to", "escalate_to_id",
                        "review_sla_hours", "confirm_sla_hours", "urge_interval_hours",
                        "max_urge_times", "enabled", "remark"]
                d = dict(zip(keys, row))
                d["source"] = "mysql"
                return d
        finally:
            c.close()
    except Exception:
        pass
    d = dict(_MEM.get(code, DEFAULTS["MID"]))
    d["source"] = "memory"
    return d


def all_matrix() -> dict:
    """取全量映射。"""
    out, src = {}, "mysql"
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT level_code, level_name, primary_owner, primary_owner_id,"
                    " deputy_owner, deputy_owner_id, escalate_to, escalate_to_id,"
                    " review_sla_hours, confirm_sla_hours, urge_interval_hours,"
                    " max_urge_times, enabled, remark FROM %s ORDER BY "
                    "FIELD(level_code,'HIGH','MID','LOW')" % _db.T_OWNER)
                for row in cur.fetchall():
                    keys = ["level_code", "level_name", "primary_owner", "primary_owner_id",
                            "deputy_owner", "deputy_owner_id", "escalate_to", "escalate_to_id",
                            "review_sla_hours", "confirm_sla_hours", "urge_interval_hours",
                            "max_urge_times", "enabled", "remark"]
                    d = dict(zip(keys, row))
                    d["source"] = "mysql"
                    out[d["level_code"]] = d
        finally:
            c.close()
        if not out:
            src = "mysql(empty)"
    except Exception:
        src = "memory"
    if not out:
        out = {k: dict(v, source=src) for k, v in _MEM.items()}
    return {"matrix": out, "source": src,
            "lamp_mapping": {v: k for k, v in LEVEL_TO_LAMP.items()}}


def upsert(level, primary_owner=None, deputy_owner=None, escalate_to=None,
           primary_owner_id=None, deputy_owner_id=None, escalate_to_id=None,
           review_sla_hours=None, confirm_sla_hours=None, urge_interval_hours=None,
           max_urge_times=None, enabled=None, remark=None, actor=None) -> dict:
    """维护调整：新增或更新某等级的责任人配置。变更前后值写入留痕。"""
    code = _norm_level(level)
    before = get(code)
    actor = actor or _db._cfg("CF_AUDIT_ACTOR", "system")
    fields = {
        "level_name": LEVEL_NAME.get(code, code),
        "primary_owner": primary_owner, "deputy_owner": deputy_owner,
        "escalate_to": escalate_to, "primary_owner_id": primary_owner_id,
        "deputy_owner_id": deputy_owner_id, "escalate_to_id": escalate_to_id,
        "review_sla_hours": review_sla_hours, "confirm_sla_hours": confirm_sla_hours,
        "urge_interval_hours": urge_interval_hours, "max_urge_times": max_urge_times,
        "enabled": enabled, "remark": remark,
    }
    patch = {k: v for k, v in fields.items() if v is not None}
    after = dict(before)
    after.update(patch)

    persisted = False
    error = None
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO %s (level_code, level_name, primary_owner, primary_owner_id,"
                    " deputy_owner, deputy_owner_id, escalate_to, escalate_to_id,"
                    " review_sla_hours, confirm_sla_hours, urge_interval_hours,"
                    " max_urge_times, enabled, remark, updated_by)"
                    " VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)"
                    " ON DUPLICATE KEY UPDATE level_name=VALUES(level_name),"
                    " primary_owner=VALUES(primary_owner), primary_owner_id=VALUES(primary_owner_id),"
                    " deputy_owner=VALUES(deputy_owner), deputy_owner_id=VALUES(deputy_owner_id),"
                    " escalate_to=VALUES(escalate_to), escalate_to_id=VALUES(escalate_to_id),"
                    " review_sla_hours=VALUES(review_sla_hours),"
                    " confirm_sla_hours=VALUES(confirm_sla_hours),"
                    " urge_interval_hours=VALUES(urge_interval_hours),"
                    " max_urge_times=VALUES(max_urge_times), enabled=VALUES(enabled),"
                    " remark=VALUES(remark), updated_by=VALUES(updated_by)"
                    % _db.T_OWNER,
                    (code, after.get("level_name"), after.get("primary_owner"),
                     after.get("primary_owner_id") or "", after.get("deputy_owner"),
                     after.get("deputy_owner_id") or "", after.get("escalate_to"),
                     after.get("escalate_to_id") or "",
                     int(after.get("review_sla_hours") or 24),
                     int(after.get("confirm_sla_hours") or 24),
                     int(after.get("urge_interval_hours") or 12),
                     int(after.get("max_urge_times") or 3),
                     1 if after.get("enabled", 1) else 0, after.get("remark"), actor))
            c.commit()
            persisted = True
        finally:
            c.close()
    except Exception as e:
        error = "%s: %s" % (type(e).__name__, e)

    # 内存副本同步（保证数据库不可用时调整仍生效）
    _MEM[code] = {k: v for k, v in after.items() if k != "source"}

    # 字段级留痕：只对真正变化的字段逐条记录
    changed = []
    for k in ("primary_owner", "deputy_owner", "escalate_to",
              "review_sla_hours", "confirm_sla_hours", "urge_interval_hours",
              "max_urge_times", "enabled"):
        if k in patch and str(before.get(k)) != str(after.get(k)):
            changed.append(k)
            _aud.write(action="owner.matrix.update", object_type="owner_matrix",
                       object_id=code, actor=actor, actor_role="admin",
                       field_name=k, before_value=before.get(k), after_value=after.get(k),
                       reason="责任人映射维护调整")
    if not changed:
        _aud.write(action="owner.matrix.upsert", object_type="owner_matrix",
                   object_id=code, actor=actor, actor_role="admin",
                   field_name=None, before_value=None, after_value=after.get("primary_owner"),
                   reason="责任人映射新增或无字段变化")
    return {"ok": True, "level_code": code, "persisted": persisted,
            "error": error, "changed_fields": changed,
            "before": {k: before.get(k) for k in changed},
            "after": {k: after.get(k) for k in changed} or after,
            "source": "mysql" if persisted else "memory"}


def resolve(level, role="primary") -> dict:
    """按等级 + 角色取具体责任人（primary/deputy/escalate）。"""
    cfg = get(level)
    if role == "deputy":
        name, oid = cfg.get("deputy_owner") or cfg.get("primary_owner"), cfg.get("deputy_owner_id")
    elif role == "escalate":
        name, oid = cfg.get("escalate_to") or cfg.get("primary_owner"), cfg.get("escalate_to_id")
    else:
        name, oid = cfg.get("primary_owner"), cfg.get("primary_owner_id")
    return {"name": name, "user_id": oid or "", "role": role,
            "level_code": cfg.get("level_code"), "level_name": cfg.get("level_name"),
            "sla": {"review_sla_hours": cfg.get("review_sla_hours"),
                    "confirm_sla_hours": cfg.get("confirm_sla_hours"),
                    "urge_interval_hours": cfg.get("urge_interval_hours"),
                    "max_urge_times": cfg.get("max_urge_times")},
            "source": cfg.get("source")}

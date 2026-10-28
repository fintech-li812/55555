# -*- coding: utf-8 -*-
"""责任机制 · 模块二：事项状态机与责任台账。

状态流转（含超时催办与驳回重流转）：

  SUBMITTED ──assign──> ASSIGNED ──start_review──> IN_REVIEW
      │                    ▲                            │
      │                    │                       submit_review
      │                    │                            ↓
      │                    │                     PENDING_CONFIRM
      │                    │                       │    │    │
      │                    │            confirm ───┘    │    └── urge(超时)
      │                    │              ↓             │         ↓
      │                    │          CONFIRMED          │       URGING ──urge──> URGING
      │                    │                             │         │      └─超次数─> ESCALATED
      │                    │          reject ────────────┘         │
      │                    │              ↓                        │
      │                    └── REJECTED（round+1 重新分派）◄────────┘
      │                                                            │
      └──────────────── close ──────────────────────────────> CLOSED

设计要点：
1. 状态迁移必须走 TRANSITIONS 白名单，非法流转直接拒绝并说明原因；
2. 每一次状态变更都写留痕（field_name=state, before/after），形成可追溯责任链；
3. 核实记录按轮次留存（round），驳回后重流转 round+1，历史不覆盖；
4. 数据库不可用时回落内存，返回中如实标注 persisted=False，不假装已归档。
"""
from __future__ import annotations

import datetime as _dt
import uuid

from . import db as _db
from . import audit as _aud
from . import owners as _own

# ---- 状态常量 ----
SUBMITTED = "SUBMITTED"           # 已提交（未分派）
ASSIGNED = "ASSIGNED"             # 已分派（待核实）
IN_REVIEW = "IN_REVIEW"           # 核实中
PENDING_CONFIRM = "PENDING_CONFIRM"  # 已核实，待确认
URGING = "URGING"                 # 超时催办中
ESCALATED = "ESCALATED"           # 已升级（催办超限，转上级）
CONFIRMED = "CONFIRMED"           # 已确认（终态）
REJECTED = "REJECTED"             # 已驳回（过渡态，随即重新分派）
CLOSED = "CLOSED"                 # 已关闭（终态）

TRANSITIONS = {
    SUBMITTED:       [ASSIGNED, CLOSED],
    ASSIGNED:        [IN_REVIEW, CLOSED],
    IN_REVIEW:       [PENDING_CONFIRM, ASSIGNED, CLOSED],
    PENDING_CONFIRM: [CONFIRMED, REJECTED, URGING, CLOSED],
    URGING:          [CONFIRMED, REJECTED, URGING, ESCALATED, CLOSED],
    ESCALATED:       [CONFIRMED, REJECTED, CLOSED],
    CONFIRMED:       [],
    REJECTED:        [ASSIGNED],
    CLOSED:          [],
}

STATE_CN = {
    SUBMITTED: "已提交", ASSIGNED: "已分派", IN_REVIEW: "核实中",
    PENDING_CONFIRM: "待确认", URGING: "催办中", ESCALATED: "已升级",
    CONFIRMED: "已确认", REJECTED: "已驳回", CLOSED: "已关闭",
}

# 待确认及之后仍在等待回执的状态（超时扫描对象）
WAITING_STATES = (PENDING_CONFIRM, URGING, ESCALATED)

# 内存兜底
_MEM_MATTER = {}
_MEM_REVIEW = []
_MEM_CONFIRM = []
_MEM_URGE = []


def _now():
    return _dt.datetime.now()


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (dt.microsecond // 1000)


def _parse(s):
    if isinstance(s, _dt.datetime):
        return s
    if not s:
        return None
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(str(s), f)
        except Exception:
            continue
    return None


def _new_id(prefix):
    return "%s-%s" % (prefix, uuid.uuid4().hex[:12])


def _actor(a=None):
    return a or _db._cfg("CF_AUDIT_ACTOR", "system")


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------
def _save_matter(m: dict) -> dict:
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO %s (matter_id,title,description,risk_level,risk_level_name,"
                    " risk_score,lamp,source,source_ref,request_id,industry,amount,state,round,"
                    " current_owner,current_owner_id,owner_role,assign_reason,due_at,"
                    " confirm_due_at,urge_count,last_urge_at,confirmed_at,confirm_result,"
                    " created_by,closed_reason,payload_json) VALUES "
                    "(%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,"
                    " %%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s) ON DUPLICATE KEY UPDATE "
                    "state=VALUES(state),round=VALUES(round),current_owner=VALUES(current_owner),"
                    "current_owner_id=VALUES(current_owner_id),owner_role=VALUES(owner_role),"
                    "assign_reason=VALUES(assign_reason),due_at=VALUES(due_at),"
                    "confirm_due_at=VALUES(confirm_due_at),urge_count=VALUES(urge_count),"
                    "last_urge_at=VALUES(last_urge_at),confirmed_at=VALUES(confirmed_at),"
                    "confirm_result=VALUES(confirm_result),closed_reason=VALUES(closed_reason),"
                    "payload_json=VALUES(payload_json)" % _db.T_MATTER,
                    (m["matter_id"], m.get("title"), m.get("description"), m.get("risk_level"),
                     m.get("risk_level_name"), m.get("risk_score"), m.get("lamp"),
                     m.get("source"), m.get("source_ref"), m.get("request_id"),
                     m.get("industry"), m.get("amount"), m.get("state"), int(m.get("round", 1)),
                     m.get("current_owner"), m.get("current_owner_id"), m.get("owner_role"),
                     m.get("assign_reason"), m.get("due_at"), m.get("confirm_due_at"),
                     int(m.get("urge_count", 0)), m.get("last_urge_at"), m.get("confirmed_at"),
                     m.get("confirm_result"), m.get("created_by"), m.get("closed_reason"),
                     _db.jdumps(m.get("payload", {}))))
            c.commit()
            return {"persisted": True}
        finally:
            c.close()
    except Exception as e:
        return {"persisted": False, "error": "%s: %s" % (type(e).__name__, e)}


def _load_matter(matter_id: str):
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT matter_id,title,description,risk_level,risk_level_name,risk_score,"
                    " lamp,source,source_ref,request_id,industry,amount,state,round,"
                    " current_owner,current_owner_id,owner_role,assign_reason,due_at,"
                    " confirm_due_at,urge_count,last_urge_at,confirmed_at,confirm_result,"
                    " created_by,closed_reason,payload_json,created_at,updated_at "
                    "FROM %s WHERE matter_id=%%s" % _db.T_MATTER, (matter_id,))
                row = cur.fetchone()
            if not row:
                return None
            keys = ["matter_id", "title", "description", "risk_level", "risk_level_name",
                    "risk_score", "lamp", "source", "source_ref", "request_id", "industry",
                    "amount", "state", "round", "current_owner", "current_owner_id",
                    "owner_role", "assign_reason", "due_at", "confirm_due_at", "urge_count",
                    "last_urge_at", "confirmed_at", "confirm_result", "created_by",
                    "closed_reason", "payload_json", "created_at", "updated_at"]
            d = dict(zip(keys, row))
            try:
                import json
                d["payload"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
            except Exception:
                d["payload"] = {}
            for k in ("due_at", "confirm_due_at", "last_urge_at", "confirmed_at",
                      "created_at", "updated_at"):
                if d.get(k) is not None:
                    d[k] = str(d[k])
            d["amount"] = float(d["amount"]) if d.get("amount") is not None else None
            d["risk_score"] = float(d["risk_score"]) if d.get("risk_score") is not None else None
            return d
        finally:
            c.close()
    except Exception:
        return _MEM_MATTER.get(matter_id)


# ---------------------------------------------------------------------------
# 状态迁移
# ---------------------------------------------------------------------------
def _check(m: dict, to_state: str):
    frm = m.get("state")
    if to_state not in TRANSITIONS.get(frm, []):
        return {"ok": False, "error": "非法流转",
                "from": frm, "to": to_state,
                "allowed": TRANSITIONS.get(frm, []),
                "reason": "状态 %s 不允许迁移到 %s" % (STATE_CN.get(frm, frm),
                                                  STATE_CN.get(to_state, to_state))}
    return {"ok": True}


def _move(m: dict, to_state: str, actor: str, reason: str = None) -> dict:
    """执行状态迁移 + 留痕。"""
    ck = _check(m, to_state)
    if not ck["ok"]:
        return ck
    before = m.get("state")
    m["state"] = to_state
    _aud.write(action="matter.state_change", object_type="matter",
               object_id=m["matter_id"], actor=_actor(actor), actor_role="workflow",
               field_name="state", before_value=before, after_value=to_state,
               reason=reason or ("%s → %s" % (STATE_CN.get(before, before),
                                              STATE_CN.get(to_state, to_state))),
               request_id=m.get("request_id"))
    return {"ok": True, "before": before, "after": to_state}


# ---------------------------------------------------------------------------
# 对外操作
# ---------------------------------------------------------------------------
def submit(title, description=None, risk_level=None, lamp=None, risk_score=None,
           source="manual", source_ref=None, request_id=None, industry=None,
           amount=None, created_by=None, payload=None) -> dict:
    """事项提交：生成事项 → 立即按风险等级自动分派责任人。"""
    actor = _actor(created_by)
    level = _own.level_of(risk_level or lamp or "MID")
    cfg = _own.get(level)
    now = _now()
    mid = _new_id("MAT")
    m = {
        "matter_id": mid, "title": title, "description": description or "",
        "risk_level": level, "risk_level_name": _own.LEVEL_NAME.get(level, level),
        "risk_score": risk_score, "lamp": lamp or _own.LEVEL_TO_LAMP.get(level),
        "source": source, "source_ref": source_ref, "request_id": request_id,
        "industry": industry, "amount": amount, "state": SUBMITTED, "round": 1,
        "current_owner": None, "current_owner_id": None, "owner_role": None,
        "assign_reason": None, "due_at": None, "confirm_due_at": None,
        "urge_count": 0, "last_urge_at": None, "confirmed_at": None,
        "confirm_result": None, "created_by": actor, "closed_reason": None,
        "payload": payload or {}, "created_at": _fmt(now), "updated_at": _fmt(now),
    }
    m = assign(m, role="primary", actor=actor)
    r = _save_matter(m)
    _MEM_MATTER[mid] = m
    _aud.write(action="matter.submit", object_type="matter", object_id=mid,
               actor=actor, actor_role="submitter", field_name="state",
               before_value=None, after_value=m["state"],
               reason="事项提交并自动分派：%s → %s" % (
                   m["risk_level_name"], m.get("current_owner")),
               request_id=request_id)
    return {"matter_id": mid, "matter": m, "persisted": r.get("persisted", False),
            "error": r.get("error"), "assigned_to": {
                "owner": m.get("current_owner"), "role": m.get("owner_role"),
                "due_at": m.get("due_at"), "reason": m.get("assign_reason")}}


def assign(m, role="primary", actor=None, reason=None) -> dict:
    """按风险等级把事项分派给责任人（不改变 round）。"""
    level = m.get("risk_level")
    res = _own.resolve(level, role)
    now = _now()
    sla = int(res["sla"].get("review_sla_hours") or 24)
    before_owner = m.get("current_owner")
    m["current_owner"] = res["name"]
    m["current_owner_id"] = res["user_id"]
    m["owner_role"] = role
    m["assign_reason"] = reason or (
        "风险等级 %s（%s）→ 由%s %s 负责核实，%s 小时内完成" % (
            m.get("risk_level"), m.get("risk_level_name"),
            {"primary": "负责人", "deputy": "替补人", "escalate": "升级对象"}.get(role, "负责人"),
            res["name"], sla))
    m["due_at"] = _fmt(now + _dt.timedelta(hours=sla))
    if m["state"] in (SUBMITTED, REJECTED):
        _move(m, ASSIGNED, actor, reason=m["assign_reason"])
    elif m["state"] == IN_REVIEW:
        m["state"] = ASSIGNED  # 退回重派
    if before_owner and before_owner != res["name"]:
        _aud.write(action="matter.reassign", object_type="matter",
                   object_id=m.get("matter_id"), actor=_actor(actor),
                   field_name="current_owner", before_value=before_owner,
                   after_value=res["name"], reason=reason or "重新分派")
    return m


def start_review(matter_id, actor=None) -> dict:
    """核实人开始核实：ASSIGNED → IN_REVIEW。"""
    m = _load_matter(matter_id)
    if not m:
        return {"ok": False, "error": "事项不存在：%s" % matter_id}
    mv = _move(m, IN_REVIEW, actor, reason="核实人 %s 开始核实" % m.get("current_owner"))
    if not mv["ok"]:
        return mv
    r = _save_matter(m)
    _MEM_MATTER[matter_id] = m
    return {"ok": True, "matter_id": matter_id, "state": m["state"],
            "reviewer": m.get("current_owner"), "due_at": m.get("due_at"),
            "persisted": r.get("persisted", False)}


def submit_review(matter_id, reviewer=None, opinion=None, conclusion="属实",
                  next_action="通过", evidence=None, actor=None) -> dict:
    """提交核实结果：IN_REVIEW → PENDING_CONFIRM，写入核实记录（人/时间/意见/结论）。"""
    m = _load_matter(matter_id)
    if not m:
        return {"ok": False, "error": "事项不存在：%s" % matter_id}
    if m["state"] != IN_REVIEW:
        return {"ok": False, "error": "当前状态不允许提交核实结果",
                "state": m["state"], "allowed_from": [IN_REVIEW],
                "hint": "请先调用 start_review"}
    who = reviewer or m.get("current_owner")
    now = _now()
    rid = _new_id("REV")
    rec = {"review_id": rid, "matter_id": matter_id, "round": int(m.get("round", 1)),
           "reviewer": who, "reviewer_role": m.get("owner_role"),
           "review_at": _fmt(now), "opinion": opinion or "",
           "conclusion": conclusion, "next_action": next_action,
           "evidence": evidence or {}}
    persisted = False
    error = None
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO %s (review_id,matter_id,round,reviewer,reviewer_role,"
                    " review_at,opinion,conclusion,next_action,evidence_json) VALUES "
                    "(%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)" % _db.T_REVIEW,
                    (rid, matter_id, rec["round"], rec["reviewer"], rec["reviewer_role"],
                     rec["review_at"], rec["opinion"], rec["conclusion"],
                     rec["next_action"], _db.jdumps(rec["evidence"])))
            c.commit()
            persisted = True
        finally:
            c.close()
    except Exception as e:
        error = "%s: %s" % (type(e).__name__, e)
    _MEM_REVIEW.append(rec)

    cfg = _own.get(m["risk_level"])
    m["confirm_due_at"] = _fmt(now + _dt.timedelta(
        hours=int(cfg.get("confirm_sla_hours") or 24)))
    mv = _move(m, PENDING_CONFIRM, actor or who,
               reason="%s 完成核实，结论：%s" % (who, conclusion))
    r = _save_matter(m)
    _MEM_MATTER[matter_id] = m

    _aud.write(action="matter.review", object_type="matter", object_id=matter_id,
               actor=_actor(actor or who), actor_role=m.get("owner_role") or "reviewer",
               field_name="conclusion", before_value=None, after_value=conclusion,
               reason="核实意见：" + (opinion or "")[:200], request_id=m.get("request_id"))
    return {"ok": True, "review": rec, "persisted": persisted, "error": error,
            "matter_id": matter_id, "state": m["state"],
            "confirm_due_at": m.get("confirm_due_at"),
            "next": "已核实，待发送钉钉确认书" if mv["ok"] else mv}


def confirm(matter_id, actor=None, comment=None, by_id=None) -> dict:
    """接收人在线确认：→ CONFIRMED（终态），确认状态实时回写台账。"""
    m = _load_matter(matter_id)
    if not m:
        return {"ok": False, "error": "事项不存在：%s" % matter_id}
    mv = _move(m, CONFIRMED, actor, reason="确认人 %s 在线确认" % (actor or "接收人"))
    if not mv["ok"]:
        return mv
    now = _now()
    m["confirmed_at"] = _fmt(now)
    m["confirm_result"] = "confirmed"
    r = _save_matter(m)
    _MEM_MATTER[matter_id] = m
    _aud.write(action="matter.confirm", object_type="matter", object_id=matter_id,
               actor=_actor(actor), actor_role="confirmer", field_name="confirm_result",
               before_value="pending", after_value="confirmed",
               reason=comment or "确认通过", request_id=m.get("request_id"))
    return {"ok": True, "matter_id": matter_id, "state": m["state"],
            "confirmed_by": actor, "confirmed_at": m["confirmed_at"],
            "comment": comment, "persisted": r.get("persisted", False)}


def reject(matter_id, actor=None, comment=None) -> dict:
    """接收人驳回：→ REJECTED → 自动重新进入核实流转（round+1，改派替补人）。"""
    m = _load_matter(matter_id)
    if not m:
        return {"ok": False, "error": "事项不存在：%s" % matter_id}
    mv = _move(m, REJECTED, actor, reason="确认人驳回：" + (comment or "无意见"))
    if not mv["ok"]:
        return mv
    m["confirm_result"] = "rejected"
    r1 = _save_matter(m)
    # 驳回后立即重新分派：优先替补人，round+1
    old_round = int(m.get("round", 1))
    m["round"] = old_round + 1
    next_role = "deputy" if m.get("owner_role") == "primary" else "escalate"
    m = assign(m, role=next_role, actor=actor,
               reason="第%d轮核实被%s驳回（%s），第%d轮改派%s" % (
                   old_round, actor or "接收人", comment or "无意见",
                   old_round + 1,
                   {"deputy": "替补人", "escalate": "升级对象"}.get(next_role)))
    r2 = _save_matter(m)
    _MEM_MATTER[matter_id] = m
    _aud.write(action="matter.reject_reassign", object_type="matter",
               object_id=matter_id, actor=_actor(actor), actor_role="confirmer",
               field_name="round", before_value=old_round, after_value=m["round"],
               reason="驳回重流转，改派 %s（%s）" % (m.get("current_owner"), next_role),
               request_id=m.get("request_id"))
    return {"ok": True, "matter_id": matter_id, "rejected_by": actor,
            "comment": comment, "state": m["state"], "round": m["round"],
            "reassigned_to": m.get("current_owner"), "owner_role": m.get("owner_role"),
            "assign_reason": m.get("assign_reason"), "due_at": m.get("due_at"),
            "persisted": bool(r1.get("persisted") and r2.get("persisted"))}


def urge(matter_id, actor=None, reason="confirm_timeout") -> dict:
    """超时催办：PENDING_CONFIRM/URGING → URGING（次数+1，回执时限顺延一个催办间隔）。"""
    m = _load_matter(matter_id)
    if not m:
        return {"ok": False, "error": "事项不存在：%s" % matter_id}
    cfg = _own.get(m["risk_level"])
    max_urge = int(cfg.get("max_urge_times") or 3)
    interval = int(cfg.get("urge_interval_hours") or 12)
    cnt = int(m.get("urge_count") or 0)
    if cnt >= max_urge:
        return escalate(matter_id, actor=actor,
                        reason="催办已达上限 %d 次，自动升级" % max_urge)
    now = _now()
    to_state = URGING if m["state"] == PENDING_CONFIRM else m["state"]
    mv = _move(m, to_state, actor, reason="第%d次催办（%s）" % (cnt + 1, reason)) \
        if to_state != m["state"] else {"ok": True, "before": m["state"], "after": m["state"]}
    if not mv.get("ok"):
        return mv
    m["urge_count"] = cnt + 1
    m["last_urge_at"] = _fmt(now)
    m["confirm_due_at"] = _fmt(now + _dt.timedelta(hours=interval))
    r = _save_matter(m)
    _MEM_MATTER[matter_id] = m
    return {"ok": True, "matter_id": matter_id, "state": m["state"],
            "urge_count": m["urge_count"], "max_urge_times": max_urge,
            "last_urge_at": m["last_urge_at"], "next_due": m["confirm_due_at"],
            "owner": m.get("current_owner"), "persisted": r.get("persisted", False)}


def escalate(matter_id, actor=None, reason=None) -> dict:
    """催办超限升级：→ ESCALATED，责任转上级，重置催办计数。"""
    m = _load_matter(matter_id)
    if not m:
        return {"ok": False, "error": "事项不存在：%s" % matter_id}
    if m["state"] not in (URGING, ESCALATED, PENDING_CONFIRM):
        return {"ok": False, "error": "当前状态不允许升级", "state": m["state"],
                "allowed_from": [PENDING_CONFIRM, URGING, ESCALATED]}
    before_owner = m.get("current_owner")
    mv = _move(m, ESCALATED, actor, reason=reason or "催办超限自动升级")
    if not mv["ok"]:
        return mv
    m = assign(m, role="escalate", actor=actor, reason=reason or "催办超限自动升级至上级")
    m["urge_count"] = 0
    cfg = _own.get(m["risk_level"])
    m["confirm_due_at"] = _fmt(_now() + _dt.timedelta(
        hours=int(cfg.get("confirm_sla_hours") or 24)))
    r = _save_matter(m)
    _MEM_MATTER[matter_id] = m
    _aud.write(action="matter.escalate", object_type="matter", object_id=matter_id,
               actor=_actor(actor), actor_role="workflow", field_name="current_owner",
               before_value=before_owner, after_value=m.get("current_owner"),
               reason=reason or "催办超限自动升级", request_id=m.get("request_id"))
    return {"ok": True, "matter_id": matter_id, "state": m["state"],
            "escalated_to": m.get("current_owner"), "owner_role": "escalate",
            "confirm_due_at": m.get("confirm_due_at"),
            "persisted": r.get("persisted", False)}


def close(matter_id, actor=None, reason=None) -> dict:
    """人工关闭事项（终态）。"""
    m = _load_matter(matter_id)
    if not m:
        return {"ok": False, "error": "事项不存在：%s" % matter_id}
    mv = _move(m, CLOSED, actor, reason=reason or "人工关闭")
    if not mv["ok"]:
        return mv
    m["closed_reason"] = reason or "人工关闭"
    r = _save_matter(m)
    _MEM_MATTER[matter_id] = m
    return {"ok": True, "matter_id": matter_id, "state": m["state"],
            "reason": m["closed_reason"], "persisted": r.get("persisted", False)}


# ---------------------------------------------------------------------------
# 台账查询
# ---------------------------------------------------------------------------
def ledger(state=None, risk_level=None, owner=None, matter_id=None,
           since=None, keyword=None, limit=50) -> dict:
    """责任台账查询（事项 + 最新核实 + 确认状态）。"""
    where, args = [], []
    if matter_id:
        where.append("matter_id=%s"); args.append(matter_id)
    if state:
        where.append("state=%s"); args.append(state)
    if risk_level:
        where.append("risk_level=%s"); args.append(_own.level_of(risk_level))
    if owner:
        where.append("current_owner LIKE %s"); args.append("%%%s%%" % owner)
    if since:
        where.append("created_at >= %s"); args.append(since)
    if keyword:
        where.append("(title LIKE %s OR description LIKE %s OR matter_id LIKE %s)")
        args += ["%%%s%%" % keyword] * 3
    w = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM %s%s" % (_db.T_MATTER, w), args)
                total = cur.fetchone()[0]
                cur.execute(
                    "SELECT matter_id,title,risk_level,risk_level_name,lamp,state,round,"
                    " current_owner,owner_role,due_at,confirm_due_at,urge_count,"
                    " confirmed_at,confirm_result,created_by,created_at,industry,amount,"
                    " source,source_ref FROM %s%s ORDER BY created_at DESC LIMIT %%s"
                    % (_db.T_MATTER, w), args + [int(limit)])
                rows = cur.fetchall()
            items = []
            keys = ["matter_id", "title", "risk_level", "risk_level_name", "lamp", "state",
                    "round", "current_owner", "owner_role", "due_at", "confirm_due_at",
                    "urge_count", "confirmed_at", "confirm_result", "created_by",
                    "created_at", "industry", "amount", "source", "source_ref"]
            for r in rows:
                d = dict(zip(keys, r))
                for k in ("due_at", "confirm_due_at", "confirmed_at", "created_at"):
                    if d.get(k) is not None:
                        d[k] = str(d[k])
                d["amount"] = float(d["amount"]) if d.get("amount") is not None else None
                d["state_cn"] = STATE_CN.get(d["state"], d["state"])
                d.update(_overdue(d))
                items.append(d)
            return {"source": "mysql", "total": total, "items": items}
        finally:
            c.close()
    except Exception as e:
        items = [dict(m, state_cn=STATE_CN.get(m["state"], m["state"]))
                 for m in _MEM_MATTER.values()]
        return {"source": "memory", "total": len(items), "error": str(e),
                "items": items[:int(limit)]}


def _overdue(d: dict) -> dict:
    """计算是否超时 / 超时时长。"""
    due = _parse(d.get("confirm_due_at")) or _parse(d.get("due_at"))
    out = {"overdue": False, "overdue_hours": 0.0}
    if d.get("state") in WAITING_STATES and due:
        delta = (_now() - due).total_seconds() / 3600.0
        if delta > 0:
            out = {"overdue": True, "overdue_hours": round(delta, 2)}
    return out


def history(matter_id) -> dict:
    """某事项的完整责任链：事项 + 各轮核实 + 确认书 + 催办 + 状态流转留痕。"""
    m = _load_matter(matter_id)
    reviews, confirms, urges = [], [], []
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT review_id,round,reviewer,reviewer_role,review_at,"
                            " opinion,conclusion,next_action FROM %s WHERE matter_id=%%s"
                            " ORDER BY round, review_at" % _db.T_REVIEW, (matter_id,))
                for r in cur.fetchall():
                    reviews.append(dict(zip(
                        ["review_id", "round", "reviewer", "reviewer_role", "review_at",
                         "opinion", "conclusion", "next_action"],
                        (r[0], r[1], r[2], r[3], str(r[4]), r[5], r[6], r[7]))))
                cur.execute("SELECT confirm_id,round,channel,status,sent_at,confirmed_by,"
                            " confirmed_at,comment,recipients_json FROM %s WHERE matter_id=%%s"
                            " ORDER BY created_at" % _db.T_CONFIRM, (matter_id,))
                for r in cur.fetchall():
                    confirms.append(dict(zip(
                        ["confirm_id", "round", "channel", "status", "sent_at",
                         "confirmed_by", "confirmed_at", "comment", "recipients"],
                        (r[0], r[1], r[2], r[3], str(r[4]) if r[4] else None, r[5],
                         str(r[6]) if r[6] else None, r[7], r[8]))))
                cur.execute("SELECT urge_id,seq,sent_at,trigger_reason,status FROM %s"
                            " WHERE matter_id=%%s ORDER BY seq" % _db.T_URGE, (matter_id,))
                for r in cur.fetchall():
                    urges.append(dict(zip(
                        ["urge_id", "seq", "sent_at", "trigger_reason", "status"],
                        (r[0], r[1], str(r[2]) if r[2] else None, r[3], r[4]))))
        finally:
            c.close()
        src = "mysql"
    except Exception:
        reviews = [x for x in _MEM_REVIEW if x["matter_id"] == matter_id]
        confirms = [x for x in _MEM_CONFIRM if x["matter_id"] == matter_id]
        urges = [x for x in _MEM_URGE if x["matter_id"] == matter_id]
        src = "memory"
    flow = _aud.query(object_type="matter", object_id=matter_id, limit=200)
    return {"source": src, "matter": m, "reviews": reviews, "confirms": confirms,
            "urges": urges, "flow": flow.get("records", [])}


def stats() -> dict:
    """台账统计：按状态 / 按等级 / 超时数。"""
    r = ledger(limit=500)
    items = r.get("items", [])
    by_state, by_level = {}, {}
    overdue = 0
    for it in items:
        by_state[it["state"]] = by_state.get(it["state"], 0) + 1
        by_level[it["risk_level"]] = by_level.get(it["risk_level"], 0) + 1
        if it.get("overdue"):
            overdue += 1
    return {"total": len(items), "by_state": by_state, "by_level": by_level,
            "overdue": overdue, "source": r.get("source")}

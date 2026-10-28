# -*- coding: utf-8 -*-
"""责任机制 · 模块四：编排层（把责任人映射、状态机、钉钉通道串成完整链路）。

一次完整责任闭环：
  提交事项 → 按风险等级自动分派 → 核实人核实（人/时间/意见/结论）→ 发送钉钉确认书
  → 接收人在线确认（回写）或被驳回（round+1 重新流转）
  → 超时未确认自动催办 → 催办超限自动升级上级

本层只做编排与参数传递，不做金额计算、不做风险判定。
"""
from __future__ import annotations

import datetime as _dt

from . import db as _db
from . import matter as _mt
from . import owners as _own
from . import dingtalk as _dtk
from . import audit as _aud


# ---------------------------------------------------------------------------
# 1. 提交与分派
# ---------------------------------------------------------------------------
def submit_matter(title, description=None, risk_level=None, lamp=None, risk_score=None,
                  source="manual", source_ref=None, request_id=None, industry=None,
                  amount=None, created_by=None, payload=None) -> dict:
    """事项提交 + 自动分派（一步到位）。"""
    r = _mt.submit(title=title, description=description, risk_level=risk_level, lamp=lamp,
                   risk_score=risk_score, source=source, source_ref=source_ref,
                   request_id=request_id, industry=industry, amount=amount,
                   created_by=created_by, payload=payload)
    r["stage"] = "SUBMITTED→ASSIGNED"
    r["next"] = "等待 %s 核实，截止 %s" % (r["assigned_to"]["owner"], r["assigned_to"]["due_at"])
    return r


def auto_dispatch_from_assessment(assess_result: dict, title=None, created_by=None) -> dict:
    """由 v8 三层评估结果直接生成事项（灯色来自最终等级）。

    assess_result 取 risk.assess_v8 的返回，使用其「最终等级」与缓冲倍数。
    """
    final = (assess_result or {}).get("最终等级") or {}
    lamp = final.get("等级") or "黄灯"
    buf = ((assess_result or {}).get("缓冲判定") or {}).get("缓冲倍数")
    ind = (assess_result or {}).get("行业")
    amt = (assess_result or {}).get("申请金额")
    t = title or "风控评估触发：%s（%s）" % (ind or "未指定行业", lamp)
    desc = "由 risk.assess_v8 自动触发；三层融合=%s，缓冲判定=%s，取严后最终=%s" % (
        ((assess_result or {}).get("融合判定") or {}).get("等级"),
        ((assess_result or {}).get("缓冲判定") or {}).get("等级"), lamp)
    return submit_matter(title=t, description=desc, lamp=lamp,
                         risk_score=None if buf is None else float(buf),
                         source="risk.assess_v8", industry=ind, amount=amt,
                         created_by=created_by or "auto-risk-engine",
                         payload={"assess_snapshot": {
                             "Q": (assess_result.get("第一层") or {}).get("Q"),
                             "D": (assess_result.get("第二层") or {}).get("D"),
                             "P": (assess_result.get("第三层") or {}).get("P"),
                             "融合": ((assess_result.get("融合判定") or {}).get("等级")),
                             "缓冲倍数": buf}})


# ---------------------------------------------------------------------------
# 2. 核实
# ---------------------------------------------------------------------------
def begin_review(matter_id, actor=None) -> dict:
    return _mt.start_review(matter_id, actor=actor)


def complete_review(matter_id, reviewer=None, opinion=None, conclusion="属实",
                    next_action="通过", evidence=None, actor=None) -> dict:
    """核实完成 → 落核实记录 → 自动发送钉钉确认书。"""
    r = _mt.submit_review(matter_id, reviewer=reviewer, opinion=opinion,
                          conclusion=conclusion, next_action=next_action,
                          evidence=evidence, actor=actor)
    if not r.get("ok"):
        return r
    m = r.get("matter") or _mt._load_matter(matter_id)
    if m is None:
        m = _mt._MEM_MATTER.get(matter_id) or {}
    cfg = _own.get(m.get("risk_level"))
    recipients = [x for x in (m.get("current_owner"), cfg.get("deputy_owner"),
                              cfg.get("escalate_to")) if x]
    cf = _dtk.send_confirm(m, r["review"], recipients=recipients)
    _aud.write(action="confirm.send", object_type="matter", object_id=matter_id,
               actor=_db._cfg("CF_AUDIT_ACTOR", "system"), actor_role="workflow",
               field_name="confirm_id", before_value=None, after_value=cf["confirm_id"],
               reason="核实完成后发送钉钉确认书（mode=%s）" % cf["mode"])
    r["confirm"] = {"confirm_id": cf["confirm_id"], "mode": cf["mode"],
                    "sent": cf["sent"], "message_id": cf["message_id"],
                    "confirm_url": cf["confirm_url"], "recipients": cf["recipients"],
                    "persisted": cf["persisted"], "error": cf.get("error"),
                    "note": cf.get("note")}
    r["stage"] = "IN_REVIEW→PENDING_CONFIRM"
    return r


# ---------------------------------------------------------------------------
# 3. 确认 / 驳回（钉钉在线回写）
# ---------------------------------------------------------------------------
def resolve_confirm(confirm_id, action="confirm", actor=None, comment=None,
                    token=None, check_token=True) -> dict:
    """接收人在确认页点击「确认」或「驳回」。

    action: confirm | reject
    token:  钉钉链接带回的一次性令牌；check_token=False 可用于内部调用跳过校验（不建议）
    """
    cf = _dtk.load_confirm(confirm_id)
    if not cf:
        return {"ok": False, "error": "确认书不存在：%s" % confirm_id}
    if check_token and token is not None and not _dtk.verify_token(confirm_id, token):
        return {"ok": False, "error": "令牌校验失败，确认链接无效或已被篡改",
                "confirm_id": confirm_id}
    if cf.get("status") in ("confirmed", "rejected"):
        return {"ok": False, "error": "该确认书已处理，不可重复提交",
                "status": cf.get("status"), "confirmed_by": cf.get("confirmed_by"),
                "confirmed_at": cf.get("confirmed_at")}
    mid = cf.get("matter_id")
    if action == "confirm":
        r = _mt.confirm(mid, actor=actor, comment=comment)
        r["confirm_id"] = confirm_id
        r["stage"] = "PENDING_CONFIRM→CONFIRMED"
    elif action == "reject":
        r = _mt.reject(mid, actor=actor, comment=comment)
        r["confirm_id"] = confirm_id
        r["stage"] = "PENDING_CONFIRM→REJECTED→ASSIGNED(round+1)"
    else:
        return {"ok": False, "error": "未知动作：%s（允许 confirm|reject）" % action}
    if r.get("ok"):
        st = "confirmed" if action == "confirm" else "rejected"
        w = _dtk.update_confirm_status(confirm_id, st, by=actor, comment=comment)
        r["confirm_writeback"] = w
        r["next"] = ("事项已确认归档" if action == "confirm"
                     else "已驳回，第 %s 轮改派 %s" % (r.get("round"), r.get("reassigned_to")))
    return r


# ---------------------------------------------------------------------------
# 4. 超时扫描：催办 / 升级
# ---------------------------------------------------------------------------
def sweep_timeouts(as_of=None, limit=200) -> dict:
    """扫描所有待确认事项：超时即催办，催办超限即升级。

    as_of: 评估基准时间（用于测试；None 表示当前时间）
    """
    now = as_of or _dt.datetime.now()
    result = _mt.ledger(limit=limit)
    items = result.get("items", [])
    urged, escalated, skipped = [], [], []
    for it in items:
        if it.get("state") not in _mt.WAITING_STATES:
            continue
        due = _mt._parse(it.get("confirm_due_at")) or _mt._parse(it.get("due_at"))
        if not due or now <= due:
            skipped.append({"matter_id": it["matter_id"], "reason": "未超时"})
            continue
        cfg = _own.get(it.get("risk_level"))
        max_urge = int(cfg.get("max_urge_times") or 3)
        cnt = int(it.get("urge_count") or 0)
        if cnt >= max_urge:
            r = _mt.escalate(it["matter_id"], actor="timeout-sweeper",
                             reason="确认超时且催办已达 %d 次上限，自动升级" % max_urge)
            if r.get("ok"):
                seq = cnt + 1
                cf = _dtk.list_confirms(matter_id=it["matter_id"], limit=1)
                cid = (cf.get("items") or [{}])[0].get("confirm_id")
                u = _dtk.send_urge(_mt._load_matter(it["matter_id"]) or it, cid, seq,
                                   reason="urge_exhausted")
                escalated.append({"matter_id": it["matter_id"],
                                  "escalated_to": r.get("escalated_to"),
                                  "urge": {"urge_id": u.get("urge_id"), "sent": u.get("sent"),
                                           "mode": u.get("mode")}})
            else:
                skipped.append({"matter_id": it["matter_id"], "reason": r.get("error")})
        else:
            r = _mt.urge(it["matter_id"], actor="timeout-sweeper")
            if r.get("ok"):
                cf = _dtk.list_confirms(matter_id=it["matter_id"], limit=1)
                cid = (cf.get("items") or [{}])[0].get("confirm_id")
                u = _dtk.send_urge(_mt._load_matter(it["matter_id"]) or it, cid,
                                   r.get("urge_count"), reason="confirm_timeout")
                urged.append({"matter_id": it["matter_id"], "urge_count": r.get("urge_count"),
                              "owner": r.get("owner"), "next_due": r.get("next_due"),
                              "urge": {"urge_id": u.get("urge_id"), "sent": u.get("sent"),
                                       "mode": u.get("mode")}})
            else:
                skipped.append({"matter_id": it["matter_id"], "reason": r.get("error")})
    return {"scanned": len(items), "waiting": len([x for x in items
                                                   if x.get("state") in _mt.WAITING_STATES]),
            "urged": urged, "escalated": escalated, "skipped": skipped,
            "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "dingtalk_mode": _dtk.mode()["mode"]}


def urge_now(matter_id, actor=None) -> dict:
    """人工触发一次催办（不等待自动扫描）。"""
    r = _mt.urge(matter_id, actor=actor, reason="manual")
    if not r.get("ok"):
        return r
    cf = _dtk.list_confirms(matter_id=matter_id, limit=1)
    cid = (cf.get("items") or [{}])[0].get("confirm_id")
    u = _dtk.send_urge(_mt._load_matter(matter_id) or {}, cid, r.get("urge_count"),
                       reason="manual")
    r["dingtalk"] = {"urge_id": u.get("urge_id"), "sent": u.get("sent"),
                     "mode": u.get("mode"), "message_id": u.get("message_id")}
    return r


# ---------------------------------------------------------------------------
# 5. 查询与维护
# ---------------------------------------------------------------------------
def ledger(state=None, risk_level=None, owner=None, keyword=None, limit=50):
    return _mt.ledger(state=state, risk_level=risk_level, owner=owner,
                      keyword=keyword, limit=limit)


def history(matter_id):
    return _mt.history(matter_id)


def stats():
    return _mt.stats()


def owner_matrix():
    return _own.all_matrix()


def owner_upsert(level, actor=None, **kw):
    return _own.upsert(level, actor=actor, **kw)


def close_matter(matter_id, actor=None, reason=None):
    return _mt.close(matter_id, actor=actor, reason=reason)


def dingtalk_status():
    return _dtk.mode()

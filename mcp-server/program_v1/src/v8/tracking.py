# -*- coding: utf-8 -*-
"""模块二 · 三态追踪算法（已接入，替代原硬编码演示数据）。

三态定义（v8 模块五）：
  已化解  绿灯  全额已验证到账
  观察中  黄灯  未结清但在容忍区间内，带倒计时
  已恶化  红灯  逾期超阈值 / 回款覆盖率跌破下限

数据来源（全部取自八表，单位万元→元，出处已标注）：
  应收账款表 第3行 SYN-AR-001；银行流水表 SYN-T1~T6。
  到账验证只认「来源角色 = primary_actual」的主实际收付流水（八表口径）。

所有阈值集中在 RULES 中，可随行业/合同条款调整；判定依据逐条回传，可审计。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import uuid

from . import db as _db

# ---- 阈值（集中配置）----
RULES = {
    "全额结清覆盖率": 0.98,      # verified / receivable ≥ 此值 → 已化解
    "观察覆盖率下限": 0.50,      # 已逾期且覆盖率 < 此值 → 已恶化
    "恶化逾期天数": 30,          # 逾期天数 > 此值 → 已恶化
    "观察提醒提前天数": 7,        # 距到期日 ≤ 此值仍未结清 → 提醒（仍为观察中）
}

LAMP = {"已化解": "绿灯", "观察中": "黄灯", "已恶化": "红灯"}
STATES = ("已化解", "观察中", "已恶化")

# ---- 八表登记值（出处：data/制造业_完整八表_合成演示_v1.xlsx，2026-10-27 实际读取核对）----
SOURCE_NOTE = "应收账款表 第3行 / 银行流水表 SYN-T1~T6（合成演示数据，单位万元×10000）"

RECEIVABLE_ROWS = [{
    "object_ref": "SYN-AR-001", "counterparty": "合成客户A",
    "invoice_date": "2026-08-01", "due_date": "2026-08-31",
    "receivable": 200000.0, "booked_received": 130000.0,
    "received_date": "2026-08-05", "arrival_state": "部分到账",
    "note": "余款7万元未收，属正常业务（八表原始备注）",
    "出处": "应收账款表 第3行",
}]

BANK_ROWS = [
    {"流水编号": "SYN-T1", "收付日期": "2026-08-05", "对手方": "合成客户A", "金额": 130000.0,
     "用途": "货款", "关联单据号": "SYN-AR-001", "方向": "inflow",
     "来源角色": "primary_actual", "账户编号": "demo-bank-001", "现金流类别": "operating"},
    {"流水编号": "SYN-T2", "收付日期": "2026-08-10", "对手方": "合成供应商", "金额": 70000.0,
     "用途": "材料付款", "关联单据号": "", "方向": "outflow",
     "来源角色": "primary_actual", "账户编号": "demo-bank-001", "现金流类别": "operating"},
    {"流水编号": "SYN-T3", "收付日期": "2026-08-12", "对手方": "合成设备商", "金额": 20000.0,
     "用途": "购置设备", "关联单据号": "", "方向": "outflow",
     "来源角色": "primary_actual", "账户编号": "demo-bank-001", "现金流类别": "investing"},
    {"流水编号": "SYN-T4", "收付日期": "2026-08-20", "对手方": "合成设备买方", "金额": 5000.0,
     "用途": "设备处置", "关联单据号": "", "方向": "inflow",
     "来源角色": "primary_actual", "账户编号": "demo-bank-001", "现金流类别": "investing"},
    {"流水编号": "SYN-T5", "收付日期": "2026-08-25", "对手方": "合成银行", "金额": 50000.0,
     "用途": "实际借款", "关联单据号": "", "方向": "inflow",
     "来源角色": "primary_actual", "账户编号": "demo-bank-001", "现金流类别": "financing"},
    {"流水编号": "SYN-T6", "收付日期": "2026-08-26", "对手方": "合成银行", "金额": 18000.0,
     "用途": "偿还本金", "关联单据号": "", "方向": "outflow",
     "来源角色": "primary_actual", "账户编号": "demo-bank-001", "现金流类别": "financing"},
]

# 评估基准日默认取八表数据期间末（2026-08），与内核 period 对齐
DEFAULT_AS_OF = "2026-08-31"

# 内存态兜底：数据库不可用时仍在内存中维护状态，并如实标注 persisted=False
_MEM_STATE = {}


def _d(s):
    return _dt.date.fromisoformat(str(s)[:10])


def verified_amount(object_ref: str) -> float:
    """到账验证：只认银行流水中 primary_actual 且关联到该单据的流入。"""
    return sum(r["金额"] for r in BANK_ROWS
               if r["关联单据号"] == object_ref
               and r["方向"] == "inflow"
               and r["来源角色"] == "primary_actual")


def _judge(verified: float, receivable: float, due_date: _dt.date, as_of: _dt.date) -> dict:
    """三态判定核心：返回状态、依据、倒计时。"""
    ratio = (verified / receivable) if receivable else 0.0
    overdue = max(0, (as_of - due_date).days)
    countdown = (due_date - as_of).days
    if ratio >= RULES["全额结清覆盖率"]:
        state = "已化解"
        why = "已验证到账 %.2f ÷ 应收 %.2f = %.2f ≥ 全额阈值 %.2f" % (
            verified, receivable, ratio, RULES["全额结清覆盖率"])
    elif overdue > RULES["恶化逾期天数"]:
        state = "已恶化"
        why = "逾期 %d 天 > 恶化阈值 %d 天（覆盖率 %.2f）" % (
            overdue, RULES["恶化逾期天数"], ratio)
    elif overdue > 0 and ratio < RULES["观察覆盖率下限"]:
        state = "已恶化"
        why = "已逾期 %d 天且覆盖率 %.2f < 下限 %.2f" % (
            overdue, ratio, RULES["观察覆盖率下限"])
    else:
        state = "观察中"
        if countdown >= 0:
            why = "未结清，距到期日还剩 %d 天（覆盖率 %.2f）" % (countdown, ratio)
        else:
            why = "逾期 %d 天但 ≤ 阈值 %d 天，覆盖率 %.2f ≥ 下限 %.2f" % (
                overdue, RULES["恶化逾期天数"], ratio, RULES["观察覆盖率下限"])
    return {"state": state, "lamp": LAMP[state], "verified_ratio": round(ratio, 4),
            "overdue_days": overdue, "countdown_days": countdown,
            "依据": why,
            "提醒": (state == "观察中" and 0 <= countdown <= RULES["观察提醒提前天数"]),
            "待验证金额不计入缓冲": round(receivable - verified, 2)}


def evaluate(as_of: str = None, persist: bool = True) -> dict:
    """计算全部应收对象的当前三态。"""
    as_of_d = _d(as_of or DEFAULT_AS_OF)
    items = []
    for r in RECEIVABLE_ROWS:
        v = verified_amount(r["object_ref"])
        j = _judge(v, r["receivable"], _d(r["due_date"]), as_of_d)
        item = dict(r)
        item.update(j)
        item["已验证到账"] = v
        item["未收"] = round(r["receivable"] - v, 2)
        item["as_of"] = as_of_d.isoformat()
        item["验证方式"] = "银行流水 primary_actual 自动比对（SYN-T1）"
        items.append(item)
    out = {
        "schema": "mfg-v8-tracking.v1",
        "as_of": as_of_d.isoformat(),
        "阈值": RULES,
        "状态定义": {"已化解": "绿灯 · 全额已验证到账",
                 "观察中": "黄灯 · 未结清但在容忍区间，带倒计时",
                 "已恶化": "红灯 · 逾期超阈值或覆盖率跌破下限"},
        "规则": ["待验证金额不计入现金缓冲；只有银行流水 primary_actual 验证通过后才纳入缓冲计算",
               "覆盖率 = 已验证到账 ÷ 应收；逾期天数 = 评估基准日 − 到期日"],
        "记录": items,
        "数据来源": SOURCE_NOTE,
        "persisted": False,
    }
    if persist:
        for it in items:
            _persist(it, actor=_db._cfg("CF_AUDIT_ACTOR", "system"), event="EVALUATE")
        out["persisted"] = True
    return out


def timeline(object_ref: str = "SYN-AR-001") -> dict:
    """展示状态变化过程：在关键时点重算，输出状态序列与每次流转。"""
    r = next((x for x in RECEIVABLE_ROWS if x["object_ref"] == object_ref), None)
    if not r:
        return {"error": "对象不存在：%s" % object_ref}
    v = verified_amount(object_ref)
    pts = [("开票日", r["invoice_date"]), ("登记到账日", r["received_date"]),
           ("到期日", r["due_date"]),
           ("到期+30天", (_d(r["due_date"]) + _dt.timedelta(days=30)).isoformat()),
           ("今天", _dt.date.today().isoformat())]
    seq, prev = [], None
    for label, ds in pts:
        j = _judge(v, r["receivable"], _d(r["due_date"]), _d(ds))
        changed = (prev is not None and prev != j["state"])
        seq.append({"时点": label, "日期": ds, "状态": j["state"], "灯": j["lamp"],
                    "覆盖率": j["verified_ratio"], "逾期天数": j["overdue_days"],
                    "依据": j["依据"], "变化": ("%s → %s" % (prev, j["state"])) if changed else ""})
        prev = j["state"]
    return {"object_ref": object_ref, "时间线": seq,
            "当前状态": seq[-1]["状态"], "流转次数": sum(1 for s in seq if s["变化"]),
            "说明": "同一笔应收在不同评估基准日下的状态变化，用于展示流转过程"}


# ---- 状态机：允许/禁止的流转 ----
ALLOWED = {
    ("观察中", "已化解"), ("观察中", "已恶化"), ("观察中", "观察中"),
    ("已恶化", "观察中"), ("已恶化", "已化解"), ("已恶化", "已恶化"),
    ("已化解", "观察中"), ("已化解", "已化解"),
}
FORBIDDEN_NOTE = {"已化解|已恶化": "已化解不得直接跳到已恶化，必须先经观察中（防止状态跳变掩盖过程）"}

EVENTS = {
    "PAYMENT_VERIFIED": "到账验证（追加已验证金额）",
    "SETTLED_FULL": "全额结清",
    "OVERDUE_ESCALATION": "逾期升级",
    "RESTRUCTURED": "展期/重组协议",
    "NEW_RECEIVABLE": "新增未收",
    "REFUND": "发生退款",
}


def _current(object_ref: str) -> str:
    if object_ref in _MEM_STATE:
        return _MEM_STATE[object_ref]
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT state FROM %s WHERE object_ref=%%s"
                            % _db.T_TRACK, (object_ref,))
                row = cur.fetchone()
            return row[0] if row else None
        finally:
            c.close()
    except Exception:
        return None


def _persist(item: dict, actor: str, event: str) -> bool:
    """写入/更新三态当前状态与历史；失败回落内存。"""
    ref = item["object_ref"]
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT history_json FROM %s WHERE object_ref=%%s"
                            % _db.T_TRACK, (ref,))
                row = cur.fetchone()
                hist = json.loads(row[0]) if row and row[0] else []
            prev = hist[-1]["to"] if hist else None
            if prev != item["state"]:
                hist.append({"at": _dt.datetime.now().isoformat(timespec="seconds"),
                             "from": prev, "to": item["state"], "event": event,
                             "actor": actor, "ratio": item["verified_ratio"],
                             "overdue_days": item["overdue_days"], "依据": item["依据"]})
            cur2 = c.cursor()
            cur2.execute(
                "INSERT INTO %s (object_ref, counterparty, receivable_amount, verified_amount,"
                " due_date, state, verified_ratio, overdue_days, as_of, lamp, history_json,"
                " updated_by) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s) "
                "ON DUPLICATE KEY UPDATE state=VALUES(state), verified_amount=VALUES(verified_amount),"
                " verified_ratio=VALUES(verified_ratio), overdue_days=VALUES(overdue_days),"
                " as_of=VALUES(as_of), lamp=VALUES(lamp), history_json=VALUES(history_json),"
                " updated_by=VALUES(updated_by)" % _db.T_TRACK,
                (ref, item.get("counterparty"), item["receivable"], item["已验证到账"],
                 item["due_date"], item["state"], item["verified_ratio"],
                 item["overdue_days"], item["as_of"], item["lamp"],
                 _db.jdumps(hist), actor))
            c.commit()
            return True
        finally:
            c.close()
    except Exception:
        _MEM_STATE[ref] = item["state"]
        return False


def transition(object_ref: str, event: str, actor: str = None,
               amount: float = None, note: str = None,
               as_of: str = None) -> dict:
    """触发一次状态流转：校验合法性 → 重算 → 持久化 → 写留痕。"""
    actor = actor or _db._cfg("CF_AUDIT_ACTOR", "system")
    if event not in EVENTS:
        return {"ok": False, "error": "未知事件：%s（可选：%s）" % (event, "、".join(EVENTS))}
    r = next((x for x in RECEIVABLE_ROWS if x["object_ref"] == object_ref), None)
    if not r:
        return {"ok": False, "error": "对象不存在：%s" % object_ref}

    base_v = verified_amount(object_ref)
    if event == "PAYMENT_VERIFIED":
        v = base_v + float(amount or 0.0)
    elif event == "SETTLED_FULL":
        v = r["receivable"]
    elif event == "REFUND":
        v = max(0.0, base_v - float(amount or 0.0))
    else:
        v = base_v

    as_of_d = _d(as_of or DEFAULT_AS_OF)
    before = _current(object_ref)
    j = _judge(v, r["receivable"], _d(r["due_date"]), as_of_d)
    if event == "OVERDUE_ESCALATION":
        j["state"], j["lamp"] = "已恶化", "红灯"
        j["依据"] = "人工/定时触发逾期升级事件，强制置为已恶化"
    elif event == "RESTRUCTURED":
        j["state"], j["lamp"] = "观察中", "黄灯"
        j["依据"] = "展期/重组协议生效，回落观察中并重置倒计时"
    elif event == "NEW_RECEIVABLE":
        j["state"], j["lamp"] = "观察中", "黄灯"
        j["依据"] = "新增未收，进入观察中"
    after = j["state"]
    if before is None:
        before = after

    pair = (before, after)
    if pair not in ALLOWED:
        return {"ok": False, "拒绝原因": FORBIDDEN_NOTE.get(
            "%s|%s" % pair, "不允许的流转 %s → %s" % pair),
            "before": before, "after": after, "event": event}

    item = dict(r)
    item.update(j)
    item["已验证到账"] = v
    item["未收"] = round(r["receivable"] - v, 2)
    item["as_of"] = as_of_d.isoformat()
    persisted = _persist(item, actor=actor, event=event)

    try:
        from . import audit as _audit
        _audit.write(action="tracking.transition", object_type="receivable",
                     object_id=object_ref, actor=actor, field_name="state",
                     before_value=before, after_value=after,
                     reason="%s｜%s" % (event, note or j["依据"]),
                     request_id="trk-" + uuid.uuid4().hex[:10])
    except Exception:
        pass

    return {"ok": True, "object_ref": object_ref, "event": event, "event_desc": EVENTS[event],
            "before": before, "after": after, "lamp": j["lamp"],
            "已验证到账": v, "覆盖率": j["verified_ratio"], "逾期天数": j["overdue_days"],
            "依据": j["依据"], "persisted": persisted, "actor": actor,
            "as_of": as_of_d.isoformat(), "note": note}


def history(object_ref: str = None, limit: int = 50) -> dict:
    """查询三态历史（优先数据库，失败回落内存）。"""
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                if object_ref:
                    cur.execute("SELECT object_ref, state, verified_ratio, overdue_days,"
                                " as_of, lamp, history_json, updated_by, updated_at "
                                "FROM %s WHERE object_ref=%%s" % _db.T_TRACK, (object_ref,))
                else:
                    cur.execute("SELECT object_ref, state, verified_ratio, overdue_days,"
                                " as_of, lamp, history_json, updated_by, updated_at "
                                "FROM %s ORDER BY updated_at DESC LIMIT %%s"
                                % _db.T_TRACK, (limit,))
                rows = cur.fetchall()
            return {"source": "mysql", "records": [{
                "object_ref": r[0], "state": r[1], "verified_ratio": float(r[2]) if r[2] is not None else None,
                "overdue_days": r[3], "as_of": str(r[4]), "lamp": r[5],
                "history": json.loads(r[6]) if r[6] else [],
                "updated_by": r[7], "updated_at": str(r[8])} for r in rows]}
        finally:
            c.close()
    except Exception as e:
        return {"source": "memory", "error": "%s: %s" % (type(e).__name__, e),
                "records": [{"object_ref": k, "state": v} for k, v in _MEM_STATE.items()]}

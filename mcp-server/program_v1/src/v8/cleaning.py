# -*- coding: utf-8 -*-
"""模块六 · 数据清洗：批次清洗 + 结果自动写入 MySQL。

输入只取八表中已存在的行（银行流水 / 应收账款），不引入任何八表外数据。
规则集版本化（ruleset_ver），每条命中都留明细（mfg_v8_clean_item_v1）。
批次结论四要素：batch_id / passed / blocked(卡住) / anomaly_count。

blocked 判定：存在 severity=ERROR 的命中 → blocked=1，该批次不得进入计算环节。
warn 级命中计入 anomaly_count 但不阻断（例如"部分到账"属正常业务事实）。
"""
from __future__ import annotations

import datetime as _dt
import uuid

from . import db as _db
from . import tracking as _trk
from . import scope as _scope

RULESET_VER = "v1.0-20261027"
PERIOD = "2026-08"

# severity: ERROR=阻断级，WARN=提示级
RULES = [
    {"code": "R001", "name": "必需字段非空", "severity": "ERROR",
     "check": "收付日期 / 金额 / 方向 / 流水编号 均不得为空"},
    {"code": "R002", "name": "金额可解析且为正", "severity": "ERROR",
     "check": "金额必须为可解析数字且 > 0"},
    {"code": "R003", "name": "方向枚举合法", "severity": "ERROR",
     "check": "方向仅允许 inflow / outflow"},
    {"code": "R004", "name": "日期可解析且落在期间内", "severity": "ERROR",
     "check": "日期须可解析，且落在数据期间内"},
    {"code": "R005", "name": "流水编号唯一", "severity": "ERROR",
     "check": "同一批次内流水编号不得重复（重复视为重复记账）"},
    {"code": "R006", "name": "来源角色合法", "severity": "ERROR",
     "check": "来源角色须为 primary_actual / secondary_evidence 等已登记枚举"},
    {"code": "R007", "name": "现金流类别合法", "severity": "ERROR",
     "check": "现金流类别须为 operating / investing / financing"},
    {"code": "R008", "name": "应收与银行流水交叉核对", "severity": "WARN",
     "check": "应收账款已收金额应等于银行流水中关联该单据的流入合计"},
    {"code": "R009", "name": "数据闸结果为终态", "severity": "WARN",
     "check": "数据闸结果须回填终态值；'由程序检查'视为未终态"},
]

VALID_DIRECTION = {"inflow", "outflow"}
VALID_CATEGORY = {"operating", "investing", "financing"}
VALID_ROLE = {"primary_actual", "secondary_evidence", "supporting"}


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def _in_period(date_str: str) -> bool:
    try:
        return _dt.date.fromisoformat(str(date_str)[:10]).strftime("%Y-%m") == PERIOD
    except Exception:
        return False


def _check_row(row: dict, sheet: str, seq: int, row_no: int,
               seen_ids: set, bank_by_ref: dict) -> list:
    """对单行执行全部规则，返回命中明细（空列表=本行无问题）。"""
    hits = []
    ref = row.get("流水编号") or row.get("单据编号")

    def hit(code, msg, before=None, after=None):
        r = next(x for x in RULES if x["code"] == code)
        hits.append({"sheet": sheet, "row_no": row_no, "seq": seq, "object_ref": ref,
                     "rule_code": code, "rule_name": r["name"], "severity": r["severity"],
                     "message": msg, "before_value": before, "after_value": after})

    # R001 必需字段非空
    required = (["收付日期", "金额", "方向", "流水编号"] if sheet == "银行流水"
                else ["单据编号", "应收金额", "已收金额", "到期日"])
    missing = [f for f in required if row.get(f) in (None, "")]
    if missing:
        hit("R001", "缺少必需字段：%s" % "、".join(missing), before=",".join(missing))

    # R002 金额可解析且为正
    amt = _num(row.get("金额", row.get("应收金额")))
    if amt is None:
        hit("R002", "金额不可解析：%r" % row.get("金额", row.get("应收金额")),
            before=str(row.get("金额", row.get("应收金额"))))
    elif amt <= 0:
        hit("R002", "金额必须为正数，实际 %s" % amt, before=str(amt))

    if sheet == "银行流水":
        # R003 方向
        if row.get("方向") not in VALID_DIRECTION:
            hit("R003", "方向非法：%r" % row.get("方向"), before=str(row.get("方向")),
                after="inflow|outflow")
        # R004 日期
        if not _in_period(row.get("收付日期")):
            hit("R004", "日期不可解析或不在期间 %s 内：%r" % (PERIOD, row.get("收付日期")),
                before=str(row.get("收付日期")))
        # R005 唯一
        if ref in seen_ids:
            hit("R005", "流水编号重复：%s（疑似重复记账）" % ref, before=ref)
        else:
            seen_ids.add(ref)
        # R006 来源角色
        if row.get("来源角色") not in VALID_ROLE:
            hit("R006", "来源角色非法：%r" % row.get("来源角色"), before=str(row.get("来源角色")))
        # R007 类别
        if row.get("现金流类别") not in VALID_CATEGORY:
            hit("R007", "现金流类别非法：%r" % row.get("现金流类别"),
                before=str(row.get("现金流类别")))
    else:
        # 应收账款：交叉核对 + 数据闸
        ref_ = row.get("单据编号")
        bank_sum = bank_by_ref.get(ref_, 0.0)
        booked = _num(row.get("已收金额")) or 0.0
        if abs(booked - bank_sum) > 0.005:
            hit("R008", "应收账款已收 %.2f 与银行流水关联流入 %.2f 不一致（差 %.2f）"
                % (booked, bank_sum, booked - bank_sum),
                before=str(booked), after=str(bank_sum))
        if str(row.get("数据闸结果", "")).strip() in ("", "由程序检查"):
            hit("R009", "数据闸结果未回填终态：%r" % row.get("数据闸结果"),
                before=str(row.get("数据闸结果")), after="待程序回填")
    return hits


def run_batch(source_ref: str = "synthetic_eight_sheet_v1", operator: str = None,
              extra_rows: list = None, remark: str = None,
              persist: bool = True) -> dict:
    """执行一次清洗批次。

    extra_rows: 仅用于自检（必须自带 `_selftest=True` 标记），
                八表外数据不得进入正常批次，调用方需自行保证。
    """
    _scope.gate("clean.run_batch")           # 数据范围闸门：八表未覆盖则拒绝
    operator = operator or _db._cfg("CF_AUDIT_ACTOR", "system")
    batch_id = "cln-" + _dt.datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    t0 = _dt.datetime.now()

    bank_rows = [dict(r) for r in _trk.BANK_ROWS]
    ar_rows = [{
        "单据编号": r["object_ref"], "客户名称": r["counterparty"],
        "到期日": r["due_date"], "应收金额": r["receivable"],
        "已收金额": r["booked_received"], "到账状态": r["arrival_state"],
        "数据闸结果": "由程序检查",
    } for r in _trk.RECEIVABLE_ROWS]

    if extra_rows:
        for r in extra_rows:
            (bank_rows if r.get("_sheet") == "银行流水" else ar_rows).append(r)

    bank_by_ref = {}
    for r in bank_rows:
        if r.get("关联单据号") and r.get("方向") == "inflow":
            bank_by_ref[r["关联单据号"]] = bank_by_ref.get(r["关联单据号"], 0.0) + (_num(r.get("金额")) or 0.0)

    items, seen = [], set()
    seq = 0
    for i, row in enumerate(bank_rows, start=3):     # 工作簿中数据自第 3 行起
        seq += 1
        items += _check_row(row, "银行流水", seq, i, seen, bank_by_ref)
    for i, row in enumerate(ar_rows, start=3):
        seq += 1
        items += _check_row(row, "应收账款", seq, i, seen, bank_by_ref)

    total_items = len(bank_rows) + len(ar_rows)
    error_count = sum(1 for x in items if x["severity"] == "ERROR")
    anomaly_count = len(items)
    blocked = error_count > 0
    blocked_rows = sorted({x["row_no"] for x in items if x["severity"] == "ERROR"})
    passed_items = total_items - len(blocked_rows)
    t1 = _dt.datetime.now()
    summary = {
        "batch_id": batch_id, "source_ref": source_ref, "period": PERIOD,
        "ruleset_ver": RULESET_VER, "total_items": total_items,
        "passed_items": passed_items, "anomaly_count": anomaly_count,
        "error_count": error_count, "blocked": blocked,
        "passed": not blocked, "blocked_rows": blocked_rows,
        "duration_ms": int((t1 - t0).total_seconds() * 1000),
        "operator": operator, "remark": remark or "",
        "started_at": t0.isoformat(timespec="milliseconds"),
        "finished_at": t1.isoformat(timespec="milliseconds"),
        "items": items,
        "规则集": RULES,
        "说明": "blocked=1 时该批次不得进入计算环节；WARN 计异常但不阻断。",
        "persisted": False,
    }
    if persist:
        summary["persisted"] = _persist(summary)
    return summary


def _persist(s: dict) -> bool:
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO %s (batch_id, source_ref, period, ruleset_ver, total_items,"
                    " passed_items, anomaly_count, error_count, blocked, duration_ms,"
                    " operator, remark, started_at, finished_at) VALUES"
                    " (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)"
                    % _db.T_CLEAN_BATCH,
                    (s["batch_id"], s["source_ref"], s["period"], s["ruleset_ver"],
                     s["total_items"], s["passed_items"], s["anomaly_count"],
                     s["error_count"], 1 if s["blocked"] else 0, s["duration_ms"],
                     s["operator"], s["remark"], s["started_at"], s["finished_at"]))
                for it in s["items"]:
                    cur.execute(
                        "INSERT INTO %s (batch_id, seq, sheet_name, row_no, object_ref,"
                        " rule_code, rule_name, severity, passed, blocked, message,"
                        " before_value, after_value) VALUES"
                        " (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)"
                        % _db.T_CLEAN_ITEM,
                        (s["batch_id"], it["seq"], it["sheet"], it["row_no"], it["object_ref"],
                         it["rule_code"], it["rule_name"], it["severity"], 0,
                         1 if it["severity"] == "ERROR" else 0, it["message"],
                         it["before_value"], it["after_value"]))
            c.commit()
            return True
        finally:
            c.close()
    except Exception:
        return False


def query_batch(batch_id: str = None, limit: int = 20) -> dict:
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                if batch_id:
                    cur.execute("SELECT batch_id, source_ref, period, ruleset_ver,"
                                " total_items, passed_items, anomaly_count, error_count,"
                                " blocked, duration_ms, operator, remark, started_at,"
                                " finished_at FROM %s WHERE batch_id=%%s"
                                % _db.T_CLEAN_BATCH, (batch_id,))
                else:
                    cur.execute("SELECT batch_id, source_ref, period, ruleset_ver,"
                                " total_items, passed_items, anomaly_count, error_count,"
                                " blocked, duration_ms, operator, remark, started_at,"
                                " finished_at FROM %s ORDER BY created_at DESC LIMIT %%s"
                                % _db.T_CLEAN_BATCH, (limit,))
                rows = cur.fetchall()
            keys = ("batch_id", "source_ref", "period", "ruleset_ver", "total_items",
                    "passed_items", "anomaly_count", "error_count", "blocked",
                    "duration_ms", "operator", "remark", "started_at", "finished_at")
            out = []
            for r in rows:
                d = dict(zip(keys, r))
                d["blocked"] = bool(d["blocked"])
                d["started_at"] = str(d["started_at"]); d["finished_at"] = str(d["finished_at"])
                if batch_id:
                    d["items"] = _items(c, batch_id)
                out.append(d)
            return {"source": "mysql", "batches": out}
        finally:
            c.close()
    except Exception as e:
        return {"source": "none", "error": "%s: %s" % (type(e).__name__, e), "batches": []}


def _items(c, batch_id: str) -> list:
    with c.cursor() as cur:
        cur.execute("SELECT seq, sheet_name, row_no, object_ref, rule_code, rule_name,"
                    " severity, passed, blocked, message, before_value, after_value"
                    " FROM %s WHERE batch_id=%%s ORDER BY seq" % _db.T_CLEAN_ITEM,
                    (batch_id,))
        return [{"seq": r[0], "sheet": r[1], "row_no": r[2], "object_ref": r[3],
                 "rule_code": r[4], "rule_name": r[5], "severity": r[6],
                 "passed": bool(r[7]), "blocked": bool(r[8]), "message": r[9],
                 "before_value": r[10], "after_value": r[11]} for r in cur.fetchall()]

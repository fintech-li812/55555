# -*- coding: utf-8 -*-
"""三层模型留痕：把 Q / D / P 三层的计算结果写入 sql/init.sql 的三张日志表。

对应 V8 差距报告 v8 新增验收项 第 9 条：
    「MySQL 新增 3 张表（quality_log / deada_score_log / rf_predict_log）」

设计原则
--------
1. **不静默降级**：写入失败一律如实返回 persisted=False + error 原文，
   调用方（工具层）把落库状态一并返回给前端，不假装已归档。
2. **幂等**：request_id 由调用方给定或本模块生成；同一 request_id 重复写入
   命中唯一键时返回 duplicated=True，而不是抛异常后丢失结果。
3. **口径统一**：tenant_id / actor_id / occurred_at(UTC) 全部由服务端补齐，
   不采信调用方自报的操作人与时间（与 init.sql 头部口径第 2 条一致）。
4. **一行一指标**：quality_log 按 init.sql 的唯一键
   uk_qual_day_ind (tenant_id, check_date, indicator_code, formula_version) 设计，
   一次门禁写 15 行（15 个财务比率各一行），request_id 以「基号:指标码」派生。

表结构唯一权威定义在 sql/init.sql，本文件只做字段映射，不重复定义表。
"""
from __future__ import annotations

import datetime as _dt
import uuid

from . import db as _db

# 公式/模型版本：改动计算口径时必须同步递增，否则历史留痕与新结果不可比
FORMULA_VERSION_QUALITY = "bm-bp-quality-gate.v1"
FORMULA_VERSION_DEADA = "improved-deada-bcc.v1"
FORMULA_VERSION_RF = "benford-rf.v1"
MODEL_VERSION_RF = "benford-rf-cart-d4-t60-seed20261027.v1"
COEFFICIENT_VERSION = "offline-not-solved.v1"   # 尚未离线求解，见表 da_coefficient

# 训练标签口径（init.sql 注释：如实记 ST/风险，不写「舞弊」）
TRAINING_LABEL = "ST/风险"


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _tenant() -> str:
    return _db._cfg("CF_TENANT_ID", "fixture-tenant")


def _actor(actor: str | None) -> str:
    return actor or _db._cfg("CF_AUDIT_ACTOR", "system")


def _new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:16])


_CMP_TOL = 1e-9

# 判定「同一天同一指标的重新评估是否与已有留痕等价」时比较的字段。
# 只比结果值，不比 request_id / occurred_at（后者每次都不同，比了就永远不等价）。
QUALITY_CMP_KEYS = ("benford_chi2", "myer_index", "benford_factor",
                    "myer_factor", "quality_score", "risk_flag")


def _same(a, b) -> bool:
    """MySQL DECIMAL 读回是 Decimal、写入侧是 float，直接 == 会误判为不同。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= _CMP_TOL
    except (TypeError, ValueError):
        return a == b


def _find_latest(table: str, where: dict):
    """按 where 取最新一行（id 最大）；无则返回 None。"""
    import pymysql.cursors
    cond = " AND ".join("`%s`=%%s" % k for k in where)
    sql = "SELECT * FROM `%s` WHERE %s ORDER BY `id` DESC LIMIT 1" % (table, cond)
    c = _db.connect()
    try:
        with c.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, tuple(where.values()))
            return cur.fetchone()
    finally:
        c.close()


def _insert_versioned(table: str, row: dict, dedup: dict, cmp_keys: tuple) -> dict:
    """追加式写入（遵循 init.sql 头部「纠错不原地改写」的口径）：

    1. 无同键历史行        → 直接插入；
    2. 有历史行且结果等价  → 返回 duplicated，不重复写入（幂等）；
    3. 有历史行但结果不同  → 插入新行，并用 supersedes_id 指向被替代的旧行，
                            旧行因此不再出现在 v_record_active 之外的统计口径里。

    返回 {persisted, duplicated, superseded}。
    """
    prev = _find_latest(table, dedup)
    if prev is None:
        r = _insert_one(table, row)
        r["superseded"] = None
        return r
    if all(_same(prev.get(k), row.get(k)) for k in cmp_keys):
        return {"persisted": False, "duplicated": True, "superseded": None,
                "error": "duplicate：同日同指标已有等价留痕，未重复写入"}
    row = dict(row)
    row["supersedes_id"] = prev["id"]
    r = _insert_one(table, row)
    r["superseded"] = prev["id"]
    return r


def _insert_one(table: str, row: dict) -> dict:
    """插入一行。命中唯一键返回 duplicated=True，其他异常如实上抛。"""
    cols = ",".join("`%s`" % k for k in row)
    ph = ",".join(["%s"] * len(row))
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute("INSERT INTO `%s` (%s) VALUES (%s)" % (table, cols, ph),
                            tuple(row.values()))
            c.commit()
            return {"persisted": True, "duplicated": False}
        finally:
            c.close()
    except Exception as e:
        code = getattr(e, "args", [None])[0]
        if code in (1062, "1062"):          # ER_DUP_ENTRY
            return {"persisted": False, "duplicated": True,
                    "error": "duplicate_request_id：该 request_id 已留痕，未重复写入"}
        return {"persisted": False, "duplicated": False,
                "error": "%s: %s" % (type(e).__name__, e)}


def _base_row(source_ref: str, request_id: str, occurred_at: _dt.datetime,
              actor: str) -> dict:
    return {
        "tenant_id": _tenant(),
        "request_id": request_id,
        "source_ref": source_ref,
        "occurred_at": occurred_at,
        "actor_id": _actor(actor),
        "supersedes_id": None,
    }


def _dig2(v) -> str | None:
    """风险数字落库前统一成两位以内字符串（表列为 VARCHAR(2)）。"""
    if v is None:
        return None
    s = str(v).strip()
    return s[:2] if s else None


# ---------------------------------------------------------------- 第一层
def log_quality(gate: dict, pool_size: int, check_date: _dt.date,
                source_ref: str = "synthetic_eight_sheet_v1",
                request_id: str | None = None, actor: str | None = None,
                occurred_at: _dt.datetime | None = None,
                evaluation_version: str = FORMULA_VERSION_QUALITY) -> dict:
    """把第一层 quality_gate 的逐指标结果写入 quality_log（每个指标一行）。"""
    base = request_id or _new_id("qlt")
    now = occurred_at or _now_utc()
    detail = gate.get("明细") or {}

    written, dup, superseded, failed = [], [], [], []
    for code, item in detail.items():
        row = _base_row(source_ref, "%s:%s" % (base, code), now, actor)
        row.update({
            "check_date": check_date,
            "indicator_code": code,
            "indicator_name": item.get("名称") or code,
            "sample_n": int(pool_size),
            "benford_chi2": item.get("benford_chi2"),
            "benford_risk_digit": _dig2(item.get("benford_风险数字")),
            "myer_index": item.get("myer指数"),
            "myer_risk_digit": _dig2(item.get("myer_风险数字")),
            "benford_factor": int(item.get("B因子") or 0),
            "myer_factor": int(item.get("M因子") or 0),
            "quality_score": gate.get("Q"),
            "risk_flag": 1 if (item.get("B因子") or item.get("M因子")) else 0,
            "formula_version": evaluation_version,
        })
        # quality_log 的唯一键是 (tenant_id, check_date, indicator_code, formula_version)，
        # 不是 request_id：同一天重复评估会撞键。故用追加式写入，结果变化时
        # 用 supersedes_id 指向旧行，而不是把重复调用误报成失败。
        dedup = {
            "tenant_id": row["tenant_id"],
            "check_date": check_date,
            "indicator_code": code,
            "formula_version": evaluation_version,
        }
        r = _insert_versioned("quality_log", row, dedup, QUALITY_CMP_KEYS)
        if r["persisted"]:
            written.append(code)
            if r.get("superseded"):
                superseded.append(code)
        elif r.get("duplicated"):
            dup.append(code)
        else:
            failed.append({code: r.get("error")})

    return {
        "request_id": base,
        "table": "quality_log",
        "rows_written": len(written),
        "rows_duplicated": len(dup),
        "rows_superseded": superseded,
        "rows_failed": failed,
        # 全部命中「已有等价留痕」也算成功：幂等重放不应让调用方以为落库失败
        "persisted": (not failed) and bool(written or dup),
        "idempotent_replay": bool(dup) and not written,
        "note": "Q 为全企业合成值，逐行冗余存储以便按指标追溯",
    }


# ---------------------------------------------------------------- 第二层
def log_deada(da: dict, eval_date: _dt.date,
              source_ref: str = "synthetic_eight_sheet_v1",
              request_id: str | None = None, actor: str | None = None,
              occurred_at: _dt.datetime | None = None,
              formula_version: str = FORMULA_VERSION_DEADA,
              coefficient_version: str = COEFFICIENT_VERSION) -> dict:
    """把第二层 deada 的判别得分与权重写入 deada_score_log。"""
    rid = request_id or _new_id("dda")
    now = occurred_at or _now_utc()
    weights = da.get("权重") or {}
    inputs = da.get("投入指标") or []
    outputs = da.get("产出指标") or []

    row = _base_row(source_ref, rid, now, actor)
    row.update({
        "eval_date": eval_date,
        "input_weights": _db.jdumps({k: weights.get(k) for k in inputs}),
        "output_weights": _db.jdumps({k: weights.get(k) for k in outputs}),
        "nonzero_indicators": _db.jdumps(
            {k: v for k, v in weights.items() if v}),
        "discriminant_score": da.get("D"),
        "threshold_value": da.get("临界值"),
        "is_risk": 1 if da.get("判为风险") else 0,
        "coefficient_version": coefficient_version,
        "formula_version": formula_version,
    })
    r = _insert_one("deada_score_log", row)
    r.update({"request_id": rid, "table": "deada_score_log",
              "rows_written": 1 if r["persisted"] else 0})
    return r


# ---------------------------------------------------------------- 第三层
def log_rf(rf: dict, features: list, eval_date: _dt.date,
           source_ref: str = "synthetic_eight_sheet_v1",
           request_id: str | None = None, actor: str | None = None,
           occurred_at: _dt.datetime | None = None,
           model_version: str = MODEL_VERSION_RF,
           inference_ms: int | None = None) -> dict:
    """把第三层 benford_rf 的推理结果写入 rf_predict_log。

    注意：init.sql 中本表**没有** formula_version 列，口径版本统一记在
    model_version 里（含树数、深度、随机种子），与 deada_score_log 不同。
    """
    rid = request_id or _new_id("rfp")
    now = occurred_at or _now_utc()

    row = _base_row(source_ref, rid, now, actor)
    row.update({
        "eval_date": eval_date,
        "features": _db.jdumps(features),
        "fraud_probability": rf.get("P"),
        "n_estimators": int(rf.get("树数") or 0),
        "model_version": model_version,
        "training_label": TRAINING_LABEL,
        "inference_ms": inference_ms,
    })
    r = _insert_one("rf_predict_log", row)
    r.update({"request_id": rid, "table": "rf_predict_log",
              "rows_written": 1 if r["persisted"] else 0})
    return r

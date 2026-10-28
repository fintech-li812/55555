#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V8 定时任务统一执行器（systemd timer → oneshot service → 本脚本）。

对应 V8 差距报告 P1-1「5 个定时任务落地」与第十八章闭环验证矩阵的
「常态化压力测试 / 参数反馈修正 / 跨企业经验迁移 / 例外审批回溯」。

用法：
    python run_v8_job.py stress            # 每月 1 号 00:30 常态化压力测试
    python run_v8_job.py assess            # 每月 1 号 08:00 三层模型全量评估
    python run_v8_job.py exception_review  # 每季度 1 号 06:20 例外审批回溯
    python run_v8_job.py param_feedback    # 每月 1 号 06:00 参数反馈修正（P2）
    python run_v8_job.py cross_migrate     # 每季度 1 号 08:20 跨企业经验迁移（P2）

退出码（供 systemd 判定成败，禁止把未实现的任务伪装成成功）：
    0  任务成功完成
    2  未知任务名
    3  任务执行失败（异常，stderr 有原文）
    4  任务尚未实现（如实标注，不静默成功）
    5  数据库不可用（结果未落库）

设计红线：
    1. 不静默降级：任何"算不出来"都如实返回，绝不返回兜底数字充数；
    2. 未实现的任务返回退出码 4 并在 stderr 写明原因，让 systemctl --failed 可见；
    3. 落库失败返回 5，结果不假装已归档。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import traceback
import uuid

def _locate_src() -> str:
    """向上逐级查找 mcp-server/program_v1/src，避免把目录层级写死。

    本脚本可放在 <ROOT>/deploy/v8/ 下，也可能被临时拷到别处执行；
    逐级上溯比写死「往上三级」更不容易在目录调整后悄悄失效。
    """
    p = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        cand = os.path.join(p, "mcp-server", "program_v1", "src")
        if os.path.isdir(cand):
            return cand
        p = os.path.dirname(p)
    raise RuntimeError(
        "未能定位 mcp-server/program_v1/src（已从 %s 向上查找 6 层）"
        % os.path.dirname(os.path.abspath(__file__)))


SRC = _locate_src()
if SRC not in sys.path:
    sys.path.insert(0, SRC)

EXIT_OK, EXIT_UNKNOWN, EXIT_FAILED = 0, 2, 3
EXIT_NOT_IMPLEMENTED, EXIT_DB_UNAVAILABLE = 4, 5

# engine.stress_test 的中文场景名 → init.sql 约定的 scenario 白名单英文码
SCENARIO_CODE = {
    "回款冲击": "receivable_shock",
    "融资冲击": "financing_shock",
    "成本冲击": "cost_shock",
    "联合冲击": "combined_shock",
}

T_STRESS = "stress_test_log"
T_EXCEPTION = "exception_log"


def _log(msg: str) -> None:
    sys.stdout.write("[v8-job] %s\n" % msg)
    sys.stdout.flush()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _db():
    from v8 import db
    return db


def _cfg(key: str, default: str = "") -> str:
    return _db()._cfg(key, default)


def _insert(table: str, row: dict) -> None:
    cols = ",".join("`%s`" % k for k in row)
    ph = ",".join(["%s"] * len(row))
    db = _db()
    c = db.connect()
    try:
        with c.cursor() as cur:
            cur.execute("INSERT INTO `%s` (%s) VALUES (%s)" % (table, cols, ph),
                        tuple(row.values()))
        c.commit()
    finally:
        c.close()


def _base(source_ref: str, request_id: str, now: _dt.datetime) -> dict:
    return {
        "tenant_id": _cfg("CF_TENANT_ID", "fixture-tenant"),
        "request_id": request_id,
        "source_ref": source_ref,
        "occurred_at": now,
        "actor_id": _cfg("CF_AUDIT_ACTOR", "system"),
        "supersedes_id": None,
    }


# ---------------------------------------------------------------------------
# 任务 1：常态化压力测试（每月 1 号 00:30）
# ---------------------------------------------------------------------------
def job_stress() -> int:
    from v8 import engine
    db = _db()
    facts = engine.cashflow_facts()
    if not facts.get("ok"):
        _log("现金流事实获取失败：%s" % facts.get("error"))
        return EXIT_FAILED

    industry = _cfg("CF_V8_INDUSTRY", "制造业")
    M = facts["月均经营性流出M"]
    buffer = facts["期末现金"]
    res = engine.stress_test(industry, M, buffer)

    now = _now()
    run_id = "str-%s" % uuid.uuid4().hex[:12]
    input_snap = {"M": M, "期末现金": buffer, "行业": industry,
                  "输入出处": res.get("输入出处")}
    written = 0
    for s in res["场景结果"]:
        code = SCENARIO_CODE.get(s["场景"])
        if not code:
            # 未知场景不猜映射，如实跳过并记账
            _log("未知场景名，未落库：%s" % s["场景"])
            continue
        row = _base("synthetic_eight_sheet_v1", "%s:%s" % (run_id, code), now)
        row.update({
            "run_id": run_id,
            "scenario": code,
            "scenario_version": "stress-test.v1",
            "input_snapshot": db.jdumps(input_snap),
            "result_snapshot": db.jdumps(s),
            "risk_level": {"红灯": "red", "黄灯": "yellow",
                           "绿灯": "green"}.get(s["等级"], "unknown"),
        })
        _insert(T_STRESS, row)
        written += 1

    _log("压力测试完成：run_id=%s，写入 %d/%d 个场景，最坏=%s(%s)" % (
        run_id, written, len(res["场景结果"]),
        res["最坏情况"]["场景"], res["最坏情况"]["等级"]))
    return EXIT_OK if written else EXIT_FAILED


# ---------------------------------------------------------------------------
# 任务 2：三层模型全量评估（每月 1 号 08:00）
# ---------------------------------------------------------------------------
def job_assess() -> int:
    from v8 import indicators as ind, layers as L, layer_log as llog, fusion
    now = _now()
    today = now.date()
    n = int(_cfg("CF_V8_N_PER_GROUP", "20") or 20)
    pool = ind.build_sample_pool(n)

    ok = True
    for scenario in ("healthy", "edge", "risk"):
        target = ind.TARGET_SCENARIOS.get(scenario)
        if not target:
            continue
        values = target["values"]
        gate = L.quality_gate(pool)
        da = L.deada(pool, values)
        rf = L.benford_rf(pool, values, gate)
        fused = fusion.fuse(gate["Q"], da["D"], rf["P"],
                            d_reliable=not da.get("降级", False),
                            p_reliable=rf.get("树数", 0) > 0)

        tag = "asr-%s-%s" % (scenario, uuid.uuid4().hex[:8])
        r1 = llog.log_quality(gate, len(pool), today, request_id=tag + "-q")
        r2 = llog.log_deada(da, today, request_id=tag + "-d")
        r3 = llog.log_rf(rf, [values[k] for k, _, _ in ind.INDICATORS]
                         + L.benford_factors(values, gate), today,
                         request_id=tag + "-p")
        ok = ok and r1["persisted"] and r2["persisted"] and r3["persisted"]
        _log("%s：Q=%s D=%s P=%s → %s（规则 %s，落库 %s）" % (
            scenario, gate["Q"], da["D"], rf["P"], fused["等级"],
            fused["规则ID"],
            "成功" if (r1["persisted"] and r2["persisted"] and r3["persisted"])
            else "失败"))

    return EXIT_OK if ok else EXIT_DB_UNAVAILABLE


# ---------------------------------------------------------------------------
# 任务 3：例外审批回溯（每季度 1 号 06:20）
# ---------------------------------------------------------------------------
def job_exception_review() -> int:
    """季度例外审批回溯：按部门统计例外率，超阈值的触发审计任务。

    数据来源：exception_log 表。当前八表未接入真实审批流水，表里没有数据，
    因此本任务如实返回「无数据可回溯」，不编造部门统计。
    （v8/engine.py:exception_review 里的部门数字是硬编码演示值，不可当作
      真实审计结论使用，故此处不复用它。）
    """
    db = _db()
    try:
        c = db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT dept, COUNT(*) AS cnt, "
                    "SUM(CASE WHEN approval_status='verified' THEN 1 ELSE 0 END) AS verified "
                    "FROM %s WHERE tenant_id=%%s AND occurred_at >= %%s "
                    "GROUP BY dept" % T_EXCEPTION,
                    (_cfg("CF_TENANT_ID", "fixture-tenant"),
                     _now() - _dt.timedelta(days=95)))
                rows = cur.fetchall()
        finally:
            c.close()
    except Exception as e:
        _log("查询 exception_log 失败：%s: %s" % (type(e).__name__, e))
        return EXIT_DB_UNAVAILABLE

    if not rows:
        _log("近 95 天无例外放行记录，无可回溯数据。"
             "（真实审批流水尚未接入 exception_log，故不输出任何部门统计，"
             "也不复用 engine.exception_review 的硬编码演示数字。）")
        return EXIT_OK

    flagged = []
    for dept, cnt, verified in rows:
        rate = (verified / float(cnt)) if cnt else 0.0
        if cnt >= 5 and (1.0 - rate) > 0.2:
            flagged.append({"部门": dept, "例外次数": cnt,
                            "未核实率": round(1.0 - rate, 4)})
    _log("回溯 %d 个部门，触发审计 %d 个：%s" % (
        len(rows), len(flagged), json.dumps(flagged, ensure_ascii=False)))
    return EXIT_OK


# ---------------------------------------------------------------------------
# 任务 4/5：P2 未实现任务 —— 如实返回，不伪装成功
# ---------------------------------------------------------------------------
def _not_implemented(name: str, reason: str) -> int:
    sys.stderr.write("[v8-job] %s 尚未实现：%s\n"
                     "        本任务不会以「空跑成功」充数，故返回退出码 %d，"
                     "可通过 systemctl --failed 看到。\n"
                     % (name, reason, EXIT_NOT_IMPLEMENTED))
    return EXIT_NOT_IMPLEMENTED


def job_param_feedback() -> int:
    return _not_implemented(
        "参数反馈修正",
        "需要先落地 feedback_log 的预测-实际对照写入链路与修正规则"
        "（差距报告 P2-1）。当前 feedback_log 表已建，但无任何写入方，"
        "缺少实际值来源与 evaluation_version 口径，故不做空跑。")


def job_cross_migrate() -> int:
    return _not_implemented(
        "跨企业经验迁移",
        "需要多企业 tenant 数据与模板迁移规则（差距报告 P2-2）。"
        "当前只有单一 fixture-tenant 的合成八表，无可迁移样本。")


JOBS = {
    "stress": job_stress,
    "assess": job_assess,
    "exception_review": job_exception_review,
    "param_feedback": job_param_feedback,
    "cross_migrate": job_cross_migrate,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        sys.stderr.write("用法：run_v8_job.py {%s}\n" % "|".join(JOBS))
        return EXIT_UNKNOWN
    name = sys.argv[1]
    _log("开始执行任务：%s" % name)
    try:
        code = JOBS[name]()
    except Exception:
        traceback.print_exc()
        _log("任务 %s 执行失败" % name)
        return EXIT_FAILED
    _log("任务 %s 结束，退出码=%d" % (name, code))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

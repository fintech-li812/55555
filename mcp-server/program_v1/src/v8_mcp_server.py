# -*- coding: utf-8 -*-
"""模块一 · v8 风控 MCP Server（工具层）。

协议：MCP over JSON-RPC 2.0（协议版本 2025-06-18）
传输：
  * stdio   —— 默认，供千问办公桌面端 / 本地客户端直接拉起（同机可信）
  * http    —— `--http --port 8090`，POST /mcp，供远程调用（Nginx 反代 + HTTPS 后对外）

鉴权：
  * stdio：同机子进程，视为可信（不鉴权）
  * http ：`Authorization: Bearer <CF_MCP_TOKEN>`；未配置 token 时拒绝启动（防裸奔）

工具清单见 TOOLS（tools/list 原样输出）。

【待替换项 TODO】
  CF_MCP_TOKEN    —— 必填，HTTP 传输的 Bearer 令牌
  CF_MCP_PORT     —— 默认 8090
  CF_MCP_ALLOWED_ORIGINS —— HTTP 模式允许的 Origin，逗号分隔（默认只放行回环）
  CF_DB_*         —— 见 v8/db.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SRC = os.path.dirname(os.path.abspath(__file__))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from v8 import db as _db            # noqa: E402
from v8 import scope as _scope      # noqa: E402
from v8 import tracking as _trk     # noqa: E402
from v8 import cleaning as _cln     # noqa: E402
from v8 import audit as _aud        # noqa: E402
from v8 import engine as _eng       # noqa: E402
from v8 import industry as _ins     # noqa: E402
from v8 import indicators as _ind   # noqa: E402
from v8 import workflow as _wf      # noqa: E402 责任机制编排层
from v8 import dingtalk as _dtk     # noqa: E402 钉钉确认书通道
from v8 import layers as _lyr       # noqa: E402 三层模型（拆分为独立工具用）
from v8 import layer_log as _llog   # noqa: E402 三层留痕（quality/deada/rf 三张日志表）

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "manufacturing-risk-workbench-v8", "version": "v8.0.0"}

# ---------------------------------------------------------------------------
# 工具定义（inputSchema 为标准 JSON Schema；out_schema 为出参说明，供调用方参考）
# ---------------------------------------------------------------------------
T = lambda t, d: {"type": t, "description": d}

TOOLS = [
    {
        "name": "cashflow.calculate",
        "description": "计算制造业现金流与余额核对（v1.2 冻结内核，Decimal 运算，唯一允许产出金额的入口）",
        "inputSchema": {"type": "object", "properties": {"source_id": T("string", "来源编号")},
                        "required": ["source_id"]},
        "out_schema": {"status": "calculated|blocked", "amounts": "期初/流入/流出/净额/期末/差异",
                       "cashflow_groups": "经营/投资/筹资三类", "reconciliation_state": "matched|difference"},
    },
    {
        "name": "risk.assess_v8",
        "description": "v8 三层风控评估：Benford 质量门禁 Q → DEA-DA 判别 D → Benford-随机森林 P → FUSE 融合，并与缓冲判定取较严格者",
        "inputSchema": {"type": "object", "properties": {
            "industry": T("string", "制造业|零售业|建筑业|科技服务业"),
            "scenario": T("string", "healthy|edge|risk"),
            "amount": T("number", "本次申请金额（元，用于分级审批）"),
            "n_per_group": T("integer", "每组样本量，默认 20")}, "required": []},
        "out_schema": {"Q": "数据质量得分", "D": "判别得分", "P": "舞弊概率",
                       "融合判定": "FUSE 结果", "最终等级": "绿灯|黄灯|红灯",
                       "缓冲判定": "缓冲倍数与等级", "压力测试": "四场景结果"},
    },
    {
        "name": "quality.check",
        "description": "第一层·数据质量门禁：对 15 个财务比率逐一做 Benford χ² 与末位均匀分布检验，"
                       "输出质量得分 Q 与逐指标命中明细，并写入 quality_log",
        "inputSchema": {"type": "object", "properties": {
            "n_per_group": T("integer", "每组样本量，默认 20"),
            "as_of": T("string", "检查日期 YYYY-MM-DD，默认今天"),
            "source_ref": T("string", "来源标识"), "request_id": T("string", "幂等键，重复调用不会重复落库"),
            "actor": T("string", "操作人，缺省取 CF_AUDIT_ACTOR"),
            "persist": T("boolean", "是否落库，默认 true")}, "required": []},
        "out_schema": {"Q": "数据质量得分 [0,1]", "命中Benford数": "", "命中Myer数": "",
                       "明细": "逐指标的 χ²、风险数字、B/M 因子", "留痕": "quality_log 写入结果"},
    },
    {
        "name": "risk.deada_score",
        "description": "第二层·改进 DEA-DA 判别：两阶段单纯形法求权重，输出判别得分 D 与稀疏权重，"
                       "并写入 deada_score_log（兼容别名 deada_score）",
        "inputSchema": {"type": "object", "properties": {
            "scenario": T("string", "healthy|edge|risk，默认 edge"),
            "n_per_group": T("integer", "每组样本量，默认 20"),
            "as_of": T("string", "评估日期 YYYY-MM-DD"),
            "source_ref": T("string", "来源标识"), "request_id": T("string", "幂等键"),
            "actor": T("string", "操作人"), "persist": T("boolean", "是否落库，默认 true")}, "required": []},
        "out_schema": {"D": "D<0 判为财务风险", "权重": "投入/产出指标权重（稀疏）",
                       "降级": "线性规划未求得最优解时为 true", "留痕": "deada_score_log 写入结果"},
    },
    {
        "name": "risk.rf_predict",
        "description": "第三层·Benford-随机森林：30 维特征（15 财务比率 + 15 Benford 因子）"
                       "推理舞弊概率 P，并写入 rf_predict_log（兼容别名 rf_predict）",
        "inputSchema": {"type": "object", "properties": {
            "scenario": T("string", "healthy|edge|risk，默认 edge"),
            "n_per_group": T("integer", "每组样本量，默认 20"),
            "n_trees": T("integer", "树数，默认 60"),
            "as_of": T("string", "评估日期 YYYY-MM-DD"),
            "source_ref": T("string", "来源标识"), "request_id": T("string", "幂等键"),
            "actor": T("string", "操作人"), "persist": T("boolean", "是否落库，默认 true")}, "required": []},
        "out_schema": {"P": "舞弊概率 [0,1]，P≥0.7 升级红灯", "树数": "", "特征维度": 30,
                       "留痕": "rf_predict_log 写入结果"},
    },
    {
        "name": "risk.stress_test",
        "description": "常态化压力测试四场景（回款延迟30天/融资缩减50%/成本上涨20%/联合）",
        "inputSchema": {"type": "object", "properties": {"industry": T("string", "行业")}, "required": []},
        "out_schema": {"场景结果": "各场景缺口/冲击后缓冲/缓冲倍数/等级", "最坏情况": "最坏场景"},
    },
    {
        "name": "tracking.evaluate",
        "description": "三态追踪：计算应收对象的已化解/观察中/已恶化状态与到账验证覆盖率",
        "inputSchema": {"type": "object", "properties": {
            "as_of": T("string", "评估基准日 YYYY-MM-DD，默认取数据期间末")}, "required": []},
        "out_schema": {"记录": [{ "object_ref": "单据编号", "state": "已化解|观察中|已恶化",
                              "lamp": "绿灯|黄灯|红灯", "verified_ratio": "覆盖率",
                              "overdue_days": "逾期天数", "依据": "判定依据"}]},
    },
    {
        "name": "tracking.timeline",
        "description": "展示某笔应收在关键时点的三态变化过程（状态流转时间线）",
        "inputSchema": {"type": "object", "properties": {"object_ref": T("string", "单据编号")}, "required": []},
        "out_schema": {"时间线": [{"时点": "", "日期": "", "状态": "", "变化": "from → to"}]},
    },
    {
        "name": "tracking.transition",
        "description": "触发一次三态流转（校验合法后置新状态、持久化并写留痕）",
        "inputSchema": {"type": "object", "properties": {
            "object_ref": T("string", "单据编号"),
            "event": T("string", "PAYMENT_VERIFIED|SETTLED_FULL|OVERDUE_ESCALATION|RESTRUCTURED|NEW_RECEIVABLE|REFUND"),
            "actor": T("string", "操作人"), "amount": T("number", "本次验证/退款金额（元）"),
            "note": T("string", "备注"), "as_of": T("string", "基准日")},
            "required": ["object_ref", "event"]},
        "out_schema": {"ok": "是否成功", "before": "变更前状态", "after": "变更后状态",
                       "lamp": "灯色", "拒绝原因": "不合法时返回"},
    },
    {
        "name": "tracking.history",
        "description": "查询三态历史与流转记录",
        "inputSchema": {"type": "object", "properties": {
            "object_ref": T("string", "单据编号，留空查全部"), "limit": T("integer", "条数")}, "required": []},
        "out_schema": {"records": [{"object_ref": "", "state": "", "history": "流转数组"}]},
    },
    {
        "name": "data.scope_check",
        "description": "数据范围约束检查：列出能力清单及其在八表中的覆盖情况（未覆盖的不部署算法）",
        "inputSchema": {"type": "object", "properties": {"capability": T("string", "能力名，留空返回全部")}, "required": []},
        "out_schema": {"能力": {"<cap>": {"enabled": "bool", "reason": "", "missing_sheets": []}}},
    },
    {
        "name": "clean.run_batch",
        "description": "执行一次数据清洗批次，输出批次ID/是否通过/是否卡住/异常条数并写入 MySQL",
        "inputSchema": {"type": "object", "properties": {
            "source_ref": T("string", "来源标识"), "operator": T("string", "操作人"),
            "remark": T("string", "备注")}, "required": []},
        "out_schema": {"batch_id": "", "total_items": "总条数", "passed_items": "通过条数",
                       "anomaly_count": "异常条数", "blocked": "是否卡住", "items": "命中明细"},
    },
    {
        "name": "clean.query_batch",
        "description": "查询清洗批次与明细",
        "inputSchema": {"type": "object", "properties": {
            "batch_id": T("string", "批次ID，留空查最近"), "limit": T("integer", "条数")}, "required": []},
        "out_schema": {"batches": [{"batch_id": "", "blocked": "bool", "anomaly_count": 0}]},
    },
    {
        "name": "audit.query",
        "description": "按条件查询留痕归档（操作时间/操作人/变更前后的值）",
        "inputSchema": {"type": "object", "properties": {
            "object_type": T("string", "对象类型"), "object_id": T("string", "对象标识"),
            "actor": T("string", "操作人"), "action": T("string", "动作"),
            "since": T("string", "起始时间"), "until": T("string", "截止时间"),
            "keyword": T("string", "关键字"), "limit": T("integer", "条数")}, "required": []},
        "out_schema": {"total": 0, "records": [{"event_id": "", "actor": "", "action": "",
                                            "field_name": "", "before_value": "", "after_value": ""}]},
    },
    {
        "name": "acc.submit",
        "description": "责任机制：提交事项并按风险等级自动分派核实负责人",
        "inputSchema": {"type": "object", "properties": {
            "title": T("string", "事项标题"), "description": T("string", "事项描述"),
            "lamp": T("string", "红灯|黄灯|绿灯"), "risk_level": T("string", "HIGH|MID|LOW"),
            "source": T("string", "来源"), "source_ref": T("string", "来源对象编号"),
            "industry": T("string", "行业"), "amount": T("number", "金额"),
            "created_by": T("string", "提交人")}, "required": ["title"]},
        "out_schema": {"matter_id": "", "assigned_to": {"owner": "", "due_at": ""}, "state": ""},
    },
    {
        "name": "acc.review",
        "description": "责任机制：核实流程（step=begin 开始核实 / complete 提交核实结论并发送钉钉确认书）",
        "inputSchema": {"type": "object", "properties": {
            "matter_id": T("string", "事项编号"), "step": T("string", "begin|complete"),
            "reviewer": T("string", "核实人"), "opinion": T("string", "核实意见"),
            "conclusion": T("string", "属实|不属实|部分属实|需补充材料"),
            "next_action": T("string", "通过|驳回重核实|补充后重申"),
            "evidence": T("object", "核实依据")}, "required": ["matter_id", "step"]},
        "out_schema": {"review": {"reviewer": "", "review_at": "", "conclusion": ""},
                       "confirm": {"confirm_id": "", "confirm_url": "", "mode": ""}},
    },
    {
        "name": "acc.confirm",
        "description": "责任机制：钉钉确认书在线确认或驳回（驳回后自动 round+1 改派替补人重新流转）",
        "inputSchema": {"type": "object", "properties": {
            "confirm_id": T("string", "确认书编号"),
            "action": T("string", "confirm|reject"),
            "actor": T("string", "操作人"), "comment": T("string", "意见"),
            "token": T("string", "确认链接携带的一次性令牌")},
            "required": ["confirm_id", "action"]},
        "out_schema": {"ok": "bool", "state": "", "round": "", "reassigned_to": ""},
    },
    {
        "name": "acc.urge",
        "description": "责任机制：对超时未确认事项发送钉钉催办提醒（人工触发一次）",
        "inputSchema": {"type": "object", "properties": {
            "matter_id": T("string", "事项编号"), "actor": T("string", "操作人")},
            "required": ["matter_id"]},
        "out_schema": {"urge_count": "", "owner": "", "dingtalk": {"mode": "", "sent": ""}},
    },
    {
        "name": "acc.sweep",
        "description": "责任机制：超时扫描，对超时未确认事项自动催办，催办超限自动升级上级",
        "inputSchema": {"type": "object", "properties": {
            "as_of": T("string", "评估基准时间 YYYY-MM-DD HH:MM:SS，默认当前")}, "required": []},
        "out_schema": {"urged": "催办列表", "escalated": "升级列表", "dingtalk_mode": ""},
    },
    {
        "name": "acc.ledger",
        "description": "责任机制：责任台账查询（事项/状态/责任人/是否超时）",
        "inputSchema": {"type": "object", "properties": {
            "state": T("string", "状态"), "risk_level": T("string", "HIGH|MID|LOW"),
            "owner": T("string", "责任人"), "keyword": T("string", "关键字"),
            "limit": T("integer", "条数")}, "required": []},
        "out_schema": {"total": 0, "items": [{"matter_id": "", "state": "", "current_owner": ""}]},
    },
    {
        "name": "acc.history",
        "description": "责任机制：某事项的完整责任链（各轮核实+确认书+催办+状态流转留痕）",
        "inputSchema": {"type": "object", "properties": {
            "matter_id": T("string", "事项编号")}, "required": ["matter_id"]},
        "out_schema": {"matter": {}, "reviews": [], "confirms": [], "urges": [], "flow": []},
    },
    {
        "name": "acc.owners",
        "description": "责任机制：风险分级×责任人映射。do=list 查询；do=upsert 维护调整（改负责人/替补/升级对象/SLA）",
        "inputSchema": {"type": "object", "properties": {
            "do": T("string", "list|upsert"), "level": T("string", "HIGH|MID|LOW"),
            "primary_owner": T("string", "核实负责人"), "deputy_owner": T("string", "替补人"),
            "escalate_to": T("string", "升级对象"),
            "review_sla_hours": T("integer", "核实时限"),
            "confirm_sla_hours": T("integer", "确认时限"),
            "urge_interval_hours": T("integer", "催办间隔"),
            "max_urge_times": T("integer", "最多催办次数"),
            "actor": T("string", "维护人")}, "required": []},
        "out_schema": {"matrix": {}, "changed_fields": [], "source": ""},
    },
    {
        "name": "acc.dingtalk_status",
        "description": "责任机制：查询钉钉通道当前模式（real 真实发送 / dry-run 仅落库不送达）",
        "inputSchema": {"type": "object", "properties": {}},
        "out_schema": {"mode": "", "channel": "", "note": ""},
    },
    {
        "name": "audit.log",
        "description": "写一条留痕（操作人/动作/对象/字段/变更前后的值）",
        "inputSchema": {"type": "object", "properties": {
            "action": T("string", "动作名"), "object_type": T("string", "对象类型"),
            "object_id": T("string", "对象标识"), "actor": T("string", "操作人"),
            "field_name": T("string", "字段名"), "before_value": T("string", "变更前"),
            "after_value": T("string", "变更后"), "reason": T("string", "原因")},
            "required": ["action", "object_type"]},
        "out_schema": {"ok": "bool", "event_id": "", "persisted": "bool"},
    },
]


def _err(msg, data=None):
    e = {"code": -32000, "message": str(msg)}
    if data is not None:
        e["data"] = data
    return e


# ---------------------------------------------------------------------------
# 三层模型独立工具的实现（与 risk.assess_v8 共用同一套 layers 与样本池，
# 保证「单独调用某一层」与「整包评估」得到的 Q/D/P 完全一致）
# ---------------------------------------------------------------------------
def _layer_common(a: dict):
    """三層共用的入参归一：样本池 / 目标样本 / 评估日期 / 每组样本量。"""
    n = int(a.get("n_per_group", 20) or 20)
    pool = _ind.build_sample_pool(n)
    scenario = a.get("scenario", "edge")
    target = _ind.TARGET_SCENARIOS.get(scenario) or _ind.TARGET_SCENARIOS["edge"]
    as_of = a.get("as_of")
    d = (datetime.datetime.strptime(as_of, "%Y-%m-%d").date()
         if as_of else datetime.date.today())
    return pool, target["values"], d, n, target


def _persist_meta(result: dict, log: dict) -> None:
    """把落库结果并入返回，落库失败时如实标注，不伪装成功。"""
    result["留痕"] = log
    if not log.get("persisted"):
        result["留痕告警"] = "本次结果未落库（%s），结果仅在本次返回中有效" % (
            log.get("error") or "未请求落库")


def _layer_quality(a: dict):
    pool, values, d, n, _ = _layer_common(a)
    gate = _lyr.quality_gate(pool)
    out = {
        "schema": "mfg-v8-layer-quality.v1",
        "层": "第一层·数据质量门禁（BM-BP 双质量因子）",
        "检查日期": d.isoformat(), "每组样本量": n, "样本总数": len(pool),
    }
    out.update(gate)
    if a.get("persist", True) is False:
        out["留痕"] = {"persisted": False, "note": "调用方指定 persist=false"}
    else:
        _persist_meta(out, _llog.log_quality(
            gate, len(pool), d,
            source_ref=a.get("source_ref", "synthetic_eight_sheet_v1"),
            request_id=a.get("request_id"), actor=a.get("actor")))
    return out


def _layer_deada(a: dict):
    pool, values, d, n, _ = _layer_common(a)
    da = _lyr.deada(pool, values)
    out = {
        "schema": "mfg-v8-layer-deada.v1",
        "层": "第二层·改进 DEA-DA 判别（BCC 规模收益可变）",
        "评估日期": d.isoformat(), "每组样本量": n,
        "受评场景": a.get("scenario", "edge"),
    }
    out.update(da)
    if a.get("persist", True) is False:
        out["留痕"] = {"persisted": False, "note": "调用方指定 persist=false"}
    else:
        _persist_meta(out, _llog.log_deada(
            da, d, source_ref=a.get("source_ref", "synthetic_eight_sheet_v1"),
            request_id=a.get("request_id"), actor=a.get("actor")))
    return out


def _layer_rf(a: dict):
    pool, values, d, n, _ = _layer_common(a)
    # 第三层需要第一层的风险数字构造 Benford 因子，故先跑门禁（与 assess 同源）
    gate = _lyr.quality_gate(pool)
    t0 = time.time()
    rf = _lyr.benford_rf(pool, values, gate, n_trees=int(a.get("n_trees", 60) or 60))
    ms = int((time.time() - t0) * 1000)
    feats = ([values[k] for k, _, _ in _ind.INDICATORS]
             + _lyr.benford_factors(values, gate))
    out = {
        "schema": "mfg-v8-layer-rf.v1",
        "层": "第三层·Benford-随机森林舞弊概率",
        "评估日期": d.isoformat(), "每组样本量": n,
        "受评场景": a.get("scenario", "edge"),
        "推理耗时_ms": ms,
    }
    out.update(rf)
    if a.get("persist", True) is False:
        out["留痕"] = {"persisted": False, "note": "调用方指定 persist=false"}
    else:
        _persist_meta(out, _llog.log_rf(
            rf, feats, d, source_ref=a.get("source_ref", "synthetic_eight_sheet_v1"),
            request_id=a.get("request_id"), actor=a.get("actor"), inference_ms=ms))
    return out


def call_tool(name: str, args: dict):
    """工具分发。任何能力调用前先过数据范围闸门。"""
    a = args or {}
    if name == "cashflow.calculate":
        _scope.gate("cashflow.calculate")
        import program_workbench as pw
        return pw.calculate(a.get("source_id", "synthetic_eight_sheet_v1"))
    if name == "risk.assess_v8":
        _scope.gate("risk.assess_v8")
        return _eng.assess(industry=a.get("industry", "制造业"),
                           scenario=a.get("scenario", "edge"),
                           amount=float(a.get("amount", 120000) or 120000),
                           n_per_group=int(a.get("n_per_group", 20) or 20))
    # ---------------- 三层模型（独立工具，与 risk.assess_v8 同源）----------------
    # 三个工具共享同一次样本池构造逻辑，保证单独调用与整体评估的结果完全一致。
    if name in ("quality.check", "quality_check"):
        _scope.gate("quality.check")
        return _layer_quality(a)
    if name in ("risk.deada_score", "deada_score"):
        _scope.gate("risk.deada_score")
        return _layer_deada(a)
    if name in ("risk.rf_predict", "rf_predict"):
        _scope.gate("risk.rf_predict")
        return _layer_rf(a)
    if name == "risk.stress_test":
        _scope.gate("risk.stress_test")
        f = _eng.cashflow_facts()
        return _eng.stress_test(a.get("industry", "制造业"),
                                f.get("月均经营性流出M", 0.0), f.get("期末现金", 0.0))
    if name == "tracking.evaluate":
        _scope.gate("tracking.evaluate")
        return _trk.evaluate(as_of=a.get("as_of"))
    if name == "tracking.timeline":
        _scope.gate("tracking.evaluate")
        return _trk.timeline(a.get("object_ref", "SYN-AR-001"))
    if name == "tracking.transition":
        _scope.gate("tracking.evaluate")
        return _trk.transition(object_ref=a["object_ref"], event=a["event"],
                               actor=a.get("actor"), amount=a.get("amount"),
                               note=a.get("note"), as_of=a.get("as_of"))
    if name == "tracking.history":
        return _trk.history(object_ref=a.get("object_ref"), limit=int(a.get("limit", 50) or 50))
    if name == "data.scope_check":
        cap = a.get("capability")
        return _scope.placeholder(cap) if (cap and not _scope.coverage(cap)["enabled"]) \
            else (_scope.coverage(cap) if cap else _scope.all_capabilities())
    if name == "clean.run_batch":
        return _cln.run_batch(source_ref=a.get("source_ref", "synthetic_eight_sheet_v1"),
                              operator=a.get("operator"), remark=a.get("remark"))
    if name == "clean.query_batch":
        return _cln.query_batch(batch_id=a.get("batch_id"), limit=int(a.get("limit", 20) or 20))
    if name == "audit.query":
        return _aud.query(object_type=a.get("object_type"), object_id=a.get("object_id"),
                          actor=a.get("actor"), action=a.get("action"), since=a.get("since"),
                          until=a.get("until"), keyword=a.get("keyword"),
                          limit=int(a.get("limit", 50) or 50))
    # ---------------- 责任机制（v11）----------------
    if name == "acc.submit":
        _scope.gate("acc.submit")
        return _wf.submit_matter(title=a["title"], description=a.get("description"),
                                 lamp=a.get("lamp"), risk_level=a.get("risk_level"),
                                 source=a.get("source", "mcp"), source_ref=a.get("source_ref"),
                                 industry=a.get("industry"), amount=a.get("amount"),
                                 created_by=a.get("created_by"))
    if name == "acc.review":
        _scope.gate("acc.review")
        if a.get("step") == "complete":
            return _wf.complete_review(a["matter_id"], reviewer=a.get("reviewer"),
                                       opinion=a.get("opinion"),
                                       conclusion=a.get("conclusion", "属实"),
                                       next_action=a.get("next_action", "通过"),
                                       evidence=a.get("evidence"))
        return _wf.begin_review(a["matter_id"], actor=a.get("reviewer"))
    if name == "acc.confirm":
        _scope.gate("acc.confirm")
        return _wf.resolve_confirm(a["confirm_id"], action=a.get("action", "confirm"),
                                   actor=a.get("actor"), comment=a.get("comment"),
                                   token=a.get("token"),
                                   check_token=a.get("token") is not None)
    if name == "acc.urge":
        _scope.gate("acc.urge")
        return _wf.urge_now(a["matter_id"], actor=a.get("actor"))
    if name == "acc.sweep":
        _scope.gate("acc.sweep")
        return _wf.sweep_timeouts(
            as_of=__import__("datetime").datetime.strptime(
                a["as_of"], "%Y-%m-%d %H:%M:%S") if a.get("as_of") else None)
    if name == "acc.ledger":
        return _wf.ledger(state=a.get("state"), risk_level=a.get("risk_level"),
                          owner=a.get("owner"), keyword=a.get("keyword"),
                          limit=int(a.get("limit", 50) or 50))
    if name == "acc.history":
        return _wf.history(a["matter_id"])
    if name == "acc.owners":
        if a.get("do") == "upsert":
            return _wf.owner_upsert(
                a.get("level"), actor=a.get("actor"),
                primary_owner=a.get("primary_owner"), deputy_owner=a.get("deputy_owner"),
                escalate_to=a.get("escalate_to"),
                review_sla_hours=a.get("review_sla_hours"),
                confirm_sla_hours=a.get("confirm_sla_hours"),
                urge_interval_hours=a.get("urge_interval_hours"),
                max_urge_times=a.get("max_urge_times"))
        return _wf.owner_matrix()
    if name == "acc.dingtalk_status":
        return _wf.dingtalk_status()
    if name == "audit.log":
        return _aud.write(action=a["action"], object_type=a["object_type"],
                          object_id=a.get("object_id"), actor=a.get("actor"),
                          field_name=a.get("field_name"), before_value=a.get("before_value"),
                          after_value=a.get("after_value"), reason=a.get("reason"))
    raise KeyError("未知工具：%s" % name)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 分发
# ---------------------------------------------------------------------------
def handle(msg: dict):
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def fail(e):
        return {"jsonrpc": "2.0", "id": mid, "error": e}

    if method == "initialize":
        return ok({"protocolVersion": PROTOCOL_VERSION,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": SERVER_INFO})
    if method == "notifications/initialized" or (method or "").startswith("notifications/"):
        return None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = call_tool(name, args)
            return ok({"content": [{"type": "text",
                                    "text": json.dumps(result, ensure_ascii=False, default=str)}],
                       "structuredContent": result, "isError": False})
        except _scope.NotCoveredError as e:
            return ok({"content": [{"type": "text", "text": str(e)}],
                       "structuredContent": {"error": "not_covered", "detail": str(e)},
                       "isError": True})
        except Exception as e:
            return fail(_err("%s: %s" % (type(e).__name__, e)))
    if mid is None:
        return None
    return fail({"code": -32601, "message": "Method not found: %s" % method})


# ---------------------------------------------------------------------------
# stdio 传输
# ---------------------------------------------------------------------------
def serve_stdio():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# HTTP 传输（Streamable HTTP，单端点 POST /mcp）
# ---------------------------------------------------------------------------
class MCPHandler(BaseHTTPRequestHandler):
    token = ""

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def _auth_ok(self):
        if not self.token:
            return False
        return (self.headers.get("Authorization") or "") == "Bearer " + self.token

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, json.dumps({"ok": True, "server": SERVER_INFO,
                                        "protocol": PROTOCOL_VERSION,
                                        "db": _db.db_status()}, ensure_ascii=False).encode("utf-8"))
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
            self._send(404, b'{"error":"not found"}')
            return
        if not self._auth_ok():
            self._send(401, b'{"error":"unauthorized: Bearer token required"}')
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                        "error": _err("bad request: %s" % e)}).encode("utf-8"))
            return
        resp = handle(msg)
        if resp is None:
            self._send(202, b"")
            return
        self._send(200, json.dumps(resp, ensure_ascii=False, default=str).encode("utf-8"))


def serve_http(port: int, token: str):
    MCPHandler.token = token
    srv = ThreadingHTTPServer(("127.0.0.1", port), MCPHandler)   # 只听回环，公网由 Nginx 反代
    print("[v8-mcp] HTTP 已启动 http://127.0.0.1:%d/mcp  （Nginx 反代为 https://<域名>/mcp）" % port)
    srv.serve_forever()


def main():
    args = sys.argv[1:]
    mode = "http" if "--http" in args else "stdio"
    port = int(_db._cfg("CF_MCP_PORT", "8090") or 8090)
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    if mode == "stdio":
        sys.stderr.write("[v8-mcp] stdio 就绪 %s %s\n" % (SERVER_INFO["name"], SERVER_INFO["version"]))
        serve_stdio()
        return
    token = _db._cfg("CF_MCP_TOKEN", "")
    if not token:
        sys.stderr.write("拒绝启动 HTTP：未配置 CF_MCP_TOKEN（不允许无鉴权暴露工具层）\n")
        sys.exit(2)
    serve_http(port, token)


if __name__ == "__main__":
    main()

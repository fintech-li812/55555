# -*- coding: utf-8 -*-
"""模块三 · 数据范围约束：只对「八表」中已存在的数据启用对应算法。

规则（硬性）：
1. 每个能力（capability）必须声明它依赖的八表分表与字段；
2. 八表未覆盖的能力一律 enabled=false，调用时返回占位结果 not_covered，
   **绝不降级为"用近似数据算一个"**，也绝不编造缺失分表；
3. 开关文件 capability_switch.json 可人工关闭任一能力；人工打开仅在
   数据覆盖满足时生效（防止误开未覆盖能力）。
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SWITCH_PATH = os.path.join(HERE, "capability_switch.json")

# ---- 八表清单（出处：data/制造业_完整八表_合成演示_v1.xlsx，2026-10-27 实际读取核对）----
EIGHT_SHEETS = [
    "企业基本信息", "现金流水", "应收账款", "筹资信息",
    "投资现金流", "银行流水", "行业参数", "数据字典",
]

# 每张表当前登记的可信字段（只登记真实存在的列，不登记"将来可能有"的列）
SHEET_FIELDS = {
    "企业基本信息": ["企业名称", "行业代码", "所属行业", "企业规模", "在职人数", "营业收入", "缓冲"],
    "现金流水": ["期间", "经营性流入", "经营性流出", "投资性流入", "投资性流出", "筹资性流入", "筹资性流出"],
    "应收账款": ["单据编号", "客户名称", "开票日期", "到期日", "应收金额（万元）",
             "已收金额（万元）", "实际收款日期", "到账状态", "备注", "数据闸结果"],
    "筹资信息": ["筹资类型", "金额（万元）", "融资性质", "是否计入现金流", "期限"],
    "投资现金流": ["项目", "金额（万元）", "现金流类别", "是否非现金"],
    "银行流水": ["收付日期", "对手方", "金额（万元）", "用途", "关联单据号", "证据性质",
             "方向", "账户编号", "现金流类别", "流水编号", "来源角色", "是否内部转账"],
    "行业参数": ["行业", "K黄", "K红", "备注"],
    "数据字典": ["字段", "含义", "口径", "示例", "是否必填"],
}

# ---- 能力注册表 ----
# requires: 依赖的八表分表；fields: 依赖字段（可选，用于更细的覆盖校验）
CAPABILITIES = {
    "cashflow.calculate": {
        "name": "现金流计算与核对（v1.2 内核）",
        "requires": ["现金流水", "银行流水", "应收账款", "筹资信息", "投资现金流", "企业基本信息"],
        "fields": {"银行流水": ["收付日期", "金额（万元）", "方向", "现金流类别", "来源角色"]},
        "impl": "program_workbench.calculate('synthetic_eight_sheet_v1')",
        "note": "已在 v1.2 冻结，真算。",
    },
    "risk.assess_v8": {
        "name": "v8 三层风控评估（Q / D / P → FUSE）",
        "requires": ["企业基本信息", "行业参数", "现金流水", "银行流水"],
        "fields": {"企业基本信息": ["所属行业"], "行业参数": ["K黄", "K红"]},
        "impl": "v8.engine.assess(...)",
        "note": "现金流事实来自内核；15 个财务比率为独立登记的合成样本（八表未启用评级），已在页面声明。",
    },
    "risk.stress_test": {
        "name": "常态化压力测试（四场景）",
        "requires": ["应收账款", "筹资信息", "现金流水"],
        "fields": {"应收账款": ["应收金额（万元）", "已收金额（万元）"], "筹资信息": ["金额（万元）"]},
        "impl": "v8.engine.stress_test(...)",
        "note": "输入全部取自八表登记值，出处已标注。",
    },
    "tracking.evaluate": {
        "name": "三态追踪与到账验证",
        "requires": ["应收账款", "银行流水"],
        "fields": {"应收账款": ["单据编号", "到期日", "应收金额（万元）", "已收金额（万元）", "到账状态"],
               "银行流水": ["关联单据号", "金额（万元）", "方向", "来源角色"]},
        "impl": "v8.tracking.evaluate(...)",
        "note": "到账验证以银行流水（primary_actual，主实际收付来源）为准。",
    },
    # ---- 责任机制（v11）：流程编排能力，不直接对八表做算法 ----
    # 事项的风险等级/灯色必须来自上述已覆盖能力（tracking.evaluate / risk.assess_v8 /
    # clean.run_batch）的产出，责任机制本身不重新计算风险、不产出金额。
    "acc.submit": {
        "name": "责任机制·事项提交与自动分派",
        "requires": ["企业基本信息", "应收账款", "银行流水"],
        "fields": {"企业基本信息": ["所属行业"], "应收账款": ["单据编号"]},
        "impl": "v8.workflow.submit_matter(...)",
        "note": "分派依据为责任人矩阵（配置数据）+ 事项风险等级（来自已覆盖能力的产出）。",
    },
    "acc.review": {
        "name": "责任机制·核实流程（人/时间/意见/结论）",
        "requires": ["企业基本信息", "应收账款", "银行流水"],
        "impl": "v8.workflow.begin_review / complete_review(...)",
        "note": "核实结论为人工录入，系统不代判，不产出金额。",
    },
    "acc.confirm": {
        "name": "责任机制·钉钉确认书在线确认/驳回",
        "requires": ["企业基本信息", "应收账款", "银行流水"],
        "impl": "v8.workflow.resolve_confirm(...)",
        "note": "驳回后 round+1 改派替补人重新流转；确认状态实时回写。",
    },
    "acc.urge": {
        "name": "责任机制·超时催办",
        "requires": ["企业基本信息", "应收账款", "银行流水"],
        "impl": "v8.workflow.urge_now(...)",
        "note": "钉钉未配置凭据时为 dry-run，仅落库不送达，如实标注。",
    },
    "acc.sweep": {
        "name": "责任机制·超时扫描与自动升级",
        "requires": ["企业基本信息", "应收账款", "银行流水"],
        "impl": "v8.workflow.sweep_timeouts(...)",
        "note": "催办达上限自动升级至 escalate_to，责任转移全程留痕。",
    },
    # ---- 三层模型拆分为独立工具（差距报告 v8 新增验收项 第 8 条）----
    # 拆分动机：risk.assess_v8 一次返回 Q/D/P，无法单独复盘某一层。拆开后
    # 每层可独立调用、独立留痕，且落库到 quality_log / deada_score_log /
    # rf_predict_log 三张表。三者的输入与 assess_v8 完全同源，结果一致。
    "quality.check": {
        "name": "第一层·数据质量门禁（Benford χ² + Myer 末位堆积 → Q）",
        "requires": ["企业基本信息", "数据字典"],
        "impl": "v8.layers.quality_gate(...)",
        "note": "15 个财务比率为独立登记的合成样本（八表未启用评级），已在页面声明；"
                "Myer 指数照实展示但在小样本下易虚高，不作为唯一判据。",
    },
    "risk.deada_score": {
        "name": "第二层·改进 DEA-DA 判别得分 D",
        "requires": ["企业基本信息", "行业参数"],
        "impl": "v8.layers.deada(...)",
        "note": "线性规划为自带两阶段单纯形实现，非 scipy；求解失败时如实标注降级，不伪装最优解。",
    },
    "risk.rf_predict": {
        "name": "第三层·Benford-随机森林舞弊概率 P",
        "requires": ["企业基本信息", "行业参数"],
        "impl": "v8.layers.benford_rf(...)",
        "note": "CART 为自带实现，非 scikit-learn；训练标签为 ST/风险，与「舞弊」不等同。",
    },
    "clean.run_batch": {
        "name": "数据清洗与入库",
        "requires": ["银行流水", "应收账款", "数据字典"],
        "fields": {"银行流水": ["收付日期", "金额（万元）", "方向", "流水编号"],
               "应收账款": ["单据编号", "应收金额（万元）", "已收金额（万元）"]},
        "impl": "v8.cleaning.run_batch(...)",
        "note": "清洗规则以数据字典的口径列为依据。",
    },
    # ---- 以下为 v8 构想中提到、但八表未覆盖的能力：不部署算法，仅占位 ----
    "credit.rating": {
        "name": "信用评级（外部征信）",
        "requires": ["企业征信报告", "税务申报表", "司法涉诉记录"],
        "fields": {},
        "impl": None,
        "note": "需外部征信/税务数据，八表未覆盖 → 不部署算法，保留占位。",
    },
    "supply_chain.risk": {
        "name": "供应链集中度风险",
        "requires": ["供应商台账", "采购订单表", "客户集中度表"],
        "fields": {},
        "impl": None,
        "note": "需供应商/采购台账，八表未覆盖 → 不部署算法，保留占位。",
    },
    "inventory.risk": {
        "name": "存货跌价与周转风险",
        "requires": ["存货明细表", "库龄表"],
        "fields": {},
        "impl": None,
        "note": "需存货明细与库龄，八表未覆盖 → 不部署算法，保留占位。",
    },
}


class NotCoveredError(Exception):
    """能力所需数据未被八表覆盖，或被人工关闭。"""


def _load_switch() -> dict:
    if not os.path.exists(SWITCH_PATH):
        return {}
    try:
        return json.load(open(SWITCH_PATH, encoding="utf-8")).get("switches", {})
    except Exception:
        return {}


def coverage(cap_key: str) -> dict:
    """校验某个能力的数据覆盖情况。"""
    cap = CAPABILITIES.get(cap_key)
    if not cap:
        return {"capability": cap_key, "known": False, "enabled": False,
                "reason": "未登记的能力", "missing_sheets": []}
    missing = [s for s in cap["requires"] if s not in EIGHT_SHEETS]
    missing_fields = []
    for sheet, fields in (cap.get("fields") or {}).items():
        if sheet in EIGHT_SHEETS:
            for f in fields:
                if f not in SHEET_FIELDS.get(sheet, []):
                    missing_fields.append("%s.%s" % (sheet, f))
    sw = _load_switch().get(cap_key)
    covered = (not missing) and (not missing_fields)
    if sw is False:
        enabled, reason = False, "已被开关文件人工关闭"
    elif sw is True and not covered:
        enabled, reason = False, "开关置为 true，但数据覆盖不满足，拒绝启用（防止误开）"
    elif not covered:
        enabled, reason = False, "八表未覆盖所需数据，未部署算法（占位）"
    else:
        enabled, reason = (True, "数据覆盖满足") if sw is None else (True, "数据覆盖满足且已人工确认开启")
    return {
        "capability": cap_key, "known": True, "name": cap["name"],
        "enabled": enabled, "reason": reason,
        "requires": cap["requires"], "missing_sheets": missing,
        "missing_fields": missing_fields, "impl": cap["impl"],
        "note": cap["note"], "switch": sw,
    }


def all_capabilities() -> dict:
    return {"八表清单": EIGHT_SHEETS,
            "八表字段": SHEET_FIELDS,
            "能力": {k: coverage(k) for k in CAPABILITIES}}


def gate(cap_key: str) -> dict:
    """调用任何能力前的闸门：不满足直接抛错，绝不静默降级。"""
    c = coverage(cap_key)
    if not c["enabled"]:
        raise NotCoveredError("[%s] %s：%s" % (cap_key, c.get("name", ""), c["reason"]))
    return c


def placeholder(cap_key: str) -> dict:
    """未覆盖能力的占位返回（供前端展示"待接入"，不产出任何业务数字）。"""
    c = coverage(cap_key)
    return {
        "capability": cap_key, "name": c.get("name"),
        "status": "not_covered",
        "enabled": False,
        "reason": c["reason"],
        "missing_sheets": c["missing_sheets"],
        "missing_fields": c["missing_fields"],
        "扩展方式": "在八表中补齐 %s 后，把 capability_switch.json 对应开关置为 true 即可启用"
                   % ("、".join(c["missing_sheets"]) or "对应字段"),
        "note": c["note"],
        "产出": None,
    }

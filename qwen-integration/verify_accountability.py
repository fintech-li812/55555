# -*- coding: utf-8 -*-
"""责任机制一键验证：责任人映射 / 核实流程 / 钉钉确认书 / 驳回重流转 / 超时催办升级。

运行（进程内执行，不额外拉起服务）：
  D:/AI_Command_Workspace/runtime/cashflow_v1/Scripts/python.exe verify_accountability.py
"""
import datetime
import os
import sys

SRC = r"D:/CashflowRiskWorkbench/mcp-server/program_v1/src"
sys.path.insert(0, SRC)

from v8 import workflow as WF          # noqa: E402
from v8 import matter as MT            # noqa: E402
from v8 import owners as OW            # noqa: E402
from v8 import dingtalk as DT          # noqa: E402

fails = []


def check(name, cond, got=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  -> %s" % (got,)))
    if not cond:
        fails.append(name)


print("=== 1. 风险分级 × 责任人映射 ===")
mx = OW.all_matrix()
check("三档等级齐备", set(mx["matrix"].keys()) >= {"HIGH", "MID", "LOW"}, list(mx["matrix"]))
check("数据来源为 mysql", mx["source"].startswith("mysql"), mx["source"])
for lv in ("HIGH", "MID", "LOW"):
    r = OW.resolve(lv)
    print("       %s → 负责人 %s / 替补 %s / 升级 %s / SLA %sh" % (
        lv, r["name"], OW.get(lv).get("deputy_owner"), OW.get(lv).get("escalate_to"),
        r["sla"]["review_sla_hours"]))

print("=== 2. 事项提交 → 自动分派 ===")
s = WF.submit_matter(title="应收 SYN-AR-001 逾期 57 天未回款",
                     description="三态判定已恶化，覆盖率 0.65，逾期 57 天",
                     lamp="红灯", source="tracking.evaluate",
                     source_ref="SYN-AR-001", industry="制造业", amount=120000,
                     created_by="系统自动")
mid = s["matter_id"]
check("红灯 → 高风险", s["matter"]["risk_level"] == "HIGH", s["matter"]["risk_level"])
check("分派给 财务总监-张伟", s["assigned_to"]["owner"] == "财务总监-张伟", s["assigned_to"])
check("状态为 ASSIGNED", s["matter"]["state"] == "ASSIGNED", s["matter"]["state"])
print("       %s | %s | 负责人 %s | 核实截止 %s" % (
    mid, s["matter"]["risk_level_name"], s["assigned_to"]["owner"], s["assigned_to"]["due_at"]))

print("=== 3. 核实流程（人 / 时间 / 意见 / 结论）===")
b = WF.begin_review(mid, actor="财务总监-张伟")
check("进入 IN_REVIEW", b.get("ok") and b["state"] == "IN_REVIEW", b)
rv = WF.complete_review(mid, reviewer="财务总监-张伟",
                        opinion="已与客户对账，确认 12 万货款逾期，客户承诺本月底前付 50%",
                        conclusion="属实", next_action="通过",
                        evidence={"单据": "SYN-AR-001", "覆盖率": 0.65, "逾期天数": 57})
check("进入 PENDING_CONFIRM", rv.get("ok") and rv["state"] == "PENDING_CONFIRM", rv.get("state"))
check("核实记录含结论", rv["review"]["conclusion"] == "属实", rv["review"])
check("核实人记录正确", rv["review"]["reviewer"] == "财务总监-张伟", rv["review"])
print("       核实人 %s @ %s | 结论 %s" % (rv["review"]["reviewer"],
                                       rv["review"]["review_at"], rv["review"]["conclusion"]))

print("=== 4. 钉钉确认书 ===")
cf = rv["confirm"]
check("生成 confirm_id", bool(cf.get("confirm_id")), cf)
check("确认书已落库", cf.get("persisted") is True, cf)
check("含确认入口", cf.get("confirm_url", "").startswith("http"), cf.get("confirm_url"))
check("接收人含核实人与替补", len(cf.get("recipients", [])) >= 2, cf.get("recipients"))
check("如实标注通道模式", cf.get("mode") in ("real", "dry-run"), cf.get("mode"))
print("       %s | mode=%s | message_id=%s | 接收人 %s" % (
    cf["confirm_id"], cf["mode"], cf["message_id"], cf["recipients"]))
print("       URL:", cf["confirm_url"])

print("=== 5. 在线驳回 → 重新进入核实流转 ===")
rj = WF.resolve_confirm(cf["confirm_id"], action="reject", actor="总经理-王强",
                        comment="需补充客户书面还款承诺后再确认",
                        token=DT.sign_token(cf["confirm_id"]))
check("驳回成功", rj.get("ok"), rj)
check("轮次 +1", rj.get("round") == 2, rj.get("round"))
check("改派替补人 风控经理-李娜", rj.get("reassigned_to") == "风控经理-李娜", rj.get("reassigned_to"))
check("状态回到 ASSIGNED", rj.get("state") == "ASSIGNED", rj.get("state"))
print("       第2轮 → %s（%s）| %s" % (rj.get("reassigned_to"), rj.get("owner_role"),
                                   rj.get("assign_reason")))

print("=== 6. 令牌校验（防篡改）===")
bad = WF.resolve_confirm(cf["confirm_id"], action="confirm", actor="陌生人", token="wrong-token")
check("错误令牌被拒", (not bad.get("ok")) and "令牌" in bad.get("error", ""), bad)
dup = WF.resolve_confirm(cf["confirm_id"], action="confirm", actor="总经理-王强",
                         token=DT.sign_token(cf["confirm_id"]))
check("已处理确认书不可重复提交", (not dup.get("ok")), dup)

print("=== 7. 第2轮核实 → 在线确认 ===")
WF.begin_review(mid, actor="风控经理-李娜")
rv2 = WF.complete_review(mid, reviewer="风控经理-李娜",
                         opinion="已取得客户书面还款承诺，同意通过",
                         conclusion="属实", next_action="通过")
cf2 = rv2["confirm"]
cfm = WF.resolve_confirm(cf2["confirm_id"], action="confirm", actor="总经理-王强",
                         comment="同意，按承诺跟踪回款",
                         token=DT.sign_token(cf2["confirm_id"]))
check("确认成功", cfm.get("ok"), cfm)
check("状态 CONFIRMED", cfm.get("state") == "CONFIRMED", cfm.get("state"))
check("确认书状态回写 confirmed", cfm.get("confirm_writeback", {}).get("status") == "confirmed", cfm)
print("       %s @ %s | 意见：%s" % (cfm.get("confirmed_by"), cfm.get("confirmed_at"),
                                 cfm.get("comment")))

print("=== 8. 超时催办与自动升级 ===")
s2 = WF.submit_matter(title="建筑业缓冲倍数低于黄灯线，需核实", lamp="黄灯",
                      industry="建筑业", amount=80000, created_by="系统自动")
mid2 = s2["matter_id"]
WF.begin_review(mid2, actor="风控经理-李娜")
rv3 = WF.complete_review(mid2, reviewer="风控经理-李娜", opinion="建议压缩付款周期",
                         conclusion="属实")
u1 = WF.urge_now(mid2, actor="超时巡检")
check("人工催办 1 次", u1.get("urge_count") == 1, u1.get("urge_count"))
check("催办记录写库", u1.get("dingtalk", {}).get("sent") is True, u1.get("dingtalk"))
print("       第1次催办 → %s | %s" % (u1.get("owner"), u1.get("next_due")))
sw = WF.sweep_timeouts(as_of=datetime.datetime.now() + datetime.timedelta(days=10))
check("自动扫描命中超时事项", sw["waiting"] >= 1, sw)
check("扫描产生催办或升级", len(sw["urged"]) + len(sw["escalated"]) >= 1,
      {"urged": len(sw["urged"]), "escalated": len(sw["escalated"])})
print("       扫描 %d 项，等待中 %d，催办 %d，升级 %d" % (
    sw["scanned"], sw["waiting"], len(sw["urged"]), len(sw["escalated"])))
for e in sw["escalated"]:
    print("       升级 → %s" % e.get("escalated_to"))

# 连续扫描至催办上限，验证自动升级分支
esc_seen, rounds = None, 0
for _ in range(6):
    swx = WF.sweep_timeouts(as_of=datetime.datetime.now() + datetime.timedelta(days=10))
    rounds += 1
    if swx["escalated"]:
        esc_seen = swx["escalated"][0]
        break
check("催办超上限后自动升级", esc_seen is not None,
      {"rounds": rounds, "last": swx.get("urged")})
if esc_seen:
    esc_mid = esc_seen.get("matter_id")
    me = MT._load_matter(esc_mid)
    print("       第 %d 轮扫描触发升级：%s → %s" % (
        rounds, esc_mid, esc_seen.get("escalated_to")))
    check("升级后责任人转为上级", me.get("current_owner") == esc_seen.get("escalated_to"),
          {"matter": esc_mid, "owner": me.get("current_owner")})
    check("升级后状态 ESCALATED", me.get("state") == "ESCALATED",
          {"matter": esc_mid, "state": me.get("state")})

print("=== 9. 责任台账与追溯 ===")
lg = WF.ledger(limit=20)
check("台账可读", lg.get("total", 0) >= 2, lg.get("total"))
check("台账来源 mysql", lg.get("source") == "mysql", lg.get("source"))
h = WF.history(mid)
check("完整责任链：核实记录 ≥2 轮", len(h["reviews"]) >= 2, len(h["reviews"]))
check("确认书记录 ≥2", len(h["confirms"]) >= 2, len(h["confirms"]))
check("状态流转留痕 ≥5", len(h["flow"]) >= 5, len(h["flow"]))
print("       事项 %s：%d 轮核实 / %d 份确认书 / %d 条流转留痕 / %d 条催办" % (
    mid, len(h["reviews"]), len(h["confirms"]), len(h["flow"]), len(h["urges"])))
st = WF.stats()
print("       统计：总数 %d，按状态 %s，超时 %d" % (st["total"], st["by_state"], st["overdue"]))

print("=== 10. 责任人映射维护调整 ===")
up = OW.upsert("HIGH", primary_owner="财务总监-赵敏", actor="管理员",
               remark="人员变动：张伟离职，改为赵敏")
check("维护生效", up.get("after", {}).get("primary_owner") == "财务总监-赵敏" or
      up.get("after", {}).get("primary_owner") == "财务总监-赵敏", up.get("after"))
check("变化字段被记录", "primary_owner" in up.get("changed_fields", []), up.get("changed_fields"))
print("       变更：%s → %s（字段 %s）" % (
    up.get("before", {}).get("primary_owner"), up.get("after", {}).get("primary_owner"),
    up.get("changed_fields")))
OW.upsert("HIGH", primary_owner="财务总监-张伟", actor="管理员", remark="验证后还原")

print()
print("结果：%s" % ("全部通过" if not fails else "失败 %d 项：%s" % (len(fails), fails)))
sys.exit(1 if fails else 0)

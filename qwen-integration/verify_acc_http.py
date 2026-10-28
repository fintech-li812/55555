# -*- coding: utf-8 -*-
"""责任机制 HTTP 接口验证（8020）：提交→核实→确认书→在线确认/驳回→催办→扫描→台账。

运行前需先启动：python v8_acc_server.py
"""
import json
import sys
import urllib.request
import urllib.error

B = "http://127.0.0.1:8020"
fails = []


def check(name, cond, got=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  -> %s" % (got,)))
    if not cond:
        fails.append(name)


def get(p):
    with urllib.request.urlopen(B + p, timeout=30) as r:
        return json.load(r)


def post(p, payload):
    req = urllib.request.Request(B + p, data=json.dumps(payload).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode("utf-8")[:300]}


print("=== 1. 健康检查 ===")
h = get("/api/acc/health")
check("服务健康", h.get("ok") is True, h.get("error"))
check("数据库已连接", h["data"]["db"].get("ok") is True, h["data"]["db"].get("error"))
check("新表齐备", not [t for t in ("mfg_v8_matter_v1", "mfg_v8_review_v1", "mfg_v8_confirm_v1",
                                   "mfg_v8_urge_v1", "mfg_v8_owner_matrix_v1")
                       if t in h["data"]["db"].get("missing_tables", [])],
      h["data"]["db"].get("missing_tables"))
print("       钉钉通道：%s（%s）" % (h["data"]["dingtalk"]["mode"], h["data"]["dingtalk"]["note"]))

print("=== 2. 提交事项 → 自动分派 ===")
r = post("/api/acc/submit", {"title": "HTTP 接口验证：制造业红灯事项", "lamp": "红灯",
                             "industry": "制造业", "amount": 150000, "created_by": "验证脚本"})
check("提交成功", r.get("ok") is True, r.get("error"))
mid = r["data"]["matter_id"]
check("红灯分派给 财务总监-张伟", r["data"]["assigned_to"]["owner"] == "财务总监-张伟", r["data"])
print("       %s → %s（第 %s 轮，截止 %s）" % (
    mid, r["data"]["assigned_to"]["owner"], r["data"]["matter"]["round"],
    r["data"]["assigned_to"]["due_at"]))

print("=== 3. 核实流程 ===")
b1 = post("/api/acc/review", {"matter_id": mid, "step": "begin", "reviewer": "财务总监-张伟"})
check("开始核实", b1["data"].get("state") == "IN_REVIEW", b1.get("data"))
b2 = post("/api/acc/review", {"matter_id": mid, "step": "complete", "reviewer": "财务总监-张伟",
                              "opinion": "已核对银行流水，确认该笔应收逾期属实",
                              "conclusion": "属实", "next_action": "通过"})
check("核实完成进入待确认", b2["data"].get("state") == "PENDING_CONFIRM", b2.get("data"))
cf = b2["data"]["confirm"]
check("确认书已生成", bool(cf.get("confirm_id")), cf)
print("       %s | mode=%s | URL=%s" % (cf["confirm_id"], cf["mode"], cf["confirm_url"]))

print("=== 4. 钉钉确认页可访问（令牌校验）===")
cid, tk = cf["confirm_id"], cf["confirm_url"].split("token=")[-1]
v = get("/api/acc/confirm/view?confirm_id=%s&token=%s" % (cid, tk))
check("合法令牌可查看确认书", v.get("ok") is True, v.get("error"))
bad = get("/api/acc/confirm/view?confirm_id=%s&token=wrong" % cid)
check("错误令牌被拒", bad.get("ok") is False, bad.get("error"))
print("       确认书内容：%s / %s / 结论 %s" % (
    v["data"]["payload"]["risk_level_name"], v["data"]["payload"]["lamp"],
    v["data"]["payload"]["review"]["conclusion"]))

print("=== 5. 在线驳回 → 自动改派重流转 ===")
rj = post("/api/acc/confirm", {"confirm_id": cid, "token": tk, "action": "reject",
                               "actor": "总经理-王强", "comment": "需补充客户对账单"})
check("驳回成功", rj.get("ok") is True, rj.get("error"))
check("轮次 +1", rj["data"].get("round") == 2, rj["data"].get("round"))
check("改派替补人", rj["data"].get("reassigned_to") == "风控经理-李娜", rj["data"])
print("       第2轮 → %s | %s" % (rj["data"].get("reassigned_to"), rj["data"].get("state")))

print("=== 6. 第2轮核实 → 在线确认 ===")
post("/api/acc/review", {"matter_id": mid, "step": "begin", "reviewer": "风控经理-李娜"})
b3 = post("/api/acc/review", {"matter_id": mid, "step": "complete", "reviewer": "风控经理-李娜",
                              "opinion": "已补充客户对账单，确认无误", "conclusion": "属实"})
cf2 = b3["data"]["confirm"]
tk2 = cf2["confirm_url"].split("token=")[-1]
cfm = post("/api/acc/confirm", {"confirm_id": cf2["confirm_id"], "token": tk2,
                                "action": "confirm", "actor": "总经理-王强", "comment": "同意结案"})
check("确认成功", cfm.get("ok") is True, cfm.get("error"))
check("状态 CONFIRMED", cfm["data"].get("state") == "CONFIRMED", cfm["data"].get("state"))
print("       %s @ %s | %s" % (cfm["data"].get("confirmed_by"), cfm["data"].get("confirmed_at"),
                               cfm["data"].get("comment")))

print("=== 7. 催办与超时扫描 ===")
s2 = post("/api/acc/submit", {"title": "HTTP 接口验证：建筑业黄灯事项", "lamp": "黄灯",
                              "industry": "建筑业", "amount": 60000, "created_by": "验证脚本"})
mid2 = s2["data"]["matter_id"]
post("/api/acc/review", {"matter_id": mid2, "step": "begin", "reviewer": "风控经理-李娜"})
post("/api/acc/review", {"matter_id": mid2, "step": "complete", "reviewer": "风控经理-李娜",
                         "opinion": "建议压缩付款周期", "conclusion": "属实"})
u = post("/api/acc/urge", {"matter_id": mid2, "actor": "验证脚本"})
check("人工催办生效", u["data"].get("urge_count") >= 1, u.get("data"))
print("       催办 %s 次 → %s" % (u["data"].get("urge_count"), u["data"].get("owner")))
sw = post("/api/acc/sweep", {})
check("扫描返回统计", sw.get("ok") and "urged" in sw["data"], sw.get("error"))
print("       扫描：等待中 %d，催办 %d，升级 %d" % (
    sw["data"]["waiting"], len(sw["data"]["urged"]), len(sw["data"]["escalated"])))

print("=== 8. 台账与责任链追溯 ===")
lg = get("/api/acc/ledger?limit=20")
check("台账可读", lg["data"].get("total", 0) >= 2, lg.get("error"))
his = get("/api/acc/history?matter_id=" + mid)
check("核实记录 ≥2 轮", len(his["data"]["reviews"]) >= 2, len(his["data"]["reviews"]))
check("确认书 ≥2 份", len(his["data"]["confirms"]) >= 2, len(his["data"]["confirms"]))
check("流转留痕 ≥5 条", len(his["data"]["flow"]) >= 5, len(his["data"]["flow"]))
print("       %s：%d 轮核实 / %d 份确认书 / %d 条留痕 / %d 条催办" % (
    mid, len(his["data"]["reviews"]), len(his["data"]["confirms"]),
    len(his["data"]["flow"]), len(his["data"]["urges"])))

print("=== 9. 责任人映射维护 ===")
ow = get("/api/acc/owners")
check("映射可读", len(ow["data"]["matrix"]) >= 3, ow.get("error"))
up = post("/api/acc/owners", {"do": "upsert", "level": "MID", "primary_owner": "风控经理-孙丽",
                              "actor": "管理员"})
check("维护生效", up.get("ok") is True, up.get("error"))
print("       MID 负责人已改为：%s（变更字段 %s）" % (
    up["data"]["after"].get("primary_owner"), up["data"]["changed_fields"]))
post("/api/acc/owners", {"do": "upsert", "level": "MID", "primary_owner": "风控经理-李娜",
                         "actor": "管理员"})

print("=== 10. 页面可访问 ===")
for p in ("/", "/dingtalk/confirm"):
    try:
        with urllib.request.urlopen(B + p, timeout=15) as r:
            html = r.read().decode("utf-8")
        check("页面 %s 返回 HTML" % p, "<html" in html.lower(), len(html))
    except Exception as e:
        check("页面 %s 返回 HTML" % p, False, e)

print()
print("结果：%s" % ("全部通过" if not fails else "失败 %d 项：%s" % (len(fails), fails)))
sys.exit(1 if fails else 0)

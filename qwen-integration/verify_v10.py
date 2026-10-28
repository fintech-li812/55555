# -*- coding: utf-8 -*-
"""v10 架构一键验证：工具层 / 编排层 / 前端链路 逐项检查并给出 PASS / FAIL。

用法：
    cd qwen-integration && python verify_v10.py            # 默认只测工具层 + 编排层单元
    cd qwen-integration && python verify_v10.py --gateway  # 额外测 HTTP + SSE（需网关已启动）

退出码：全部 PASS = 0，有 FAIL = 1。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request

HERE = sys.path[0] or "."
sys.path.insert(0, HERE)

PASS, FAIL = [], []


def check(name: str, ok: bool, expect: str, actual):
    line = "%-46s %s  预期[%s] 实际[%s]" % (
        name, "PASS" if ok else "FAIL", expect, str(actual)[:70])
    print(line)
    (PASS if ok else FAIL).append(name)


def main():
    from orchestrator import mcp_client as MC
    from orchestrator import planner, qwen_adapter as QA, protocol as P

    c = MC.MCPClient("inprocess")

    # ---------- 1. 工具层 ----------
    tools = c.list_tools()
    names = [t["name"] for t in tools]
    check("1.1 MCP 工具层 tools/list", len(tools) >= 12, ">=12 个工具", len(tools))
    check("1.2 关键工具齐备",
          all(k in names for k in ("cashflow.calculate", "risk.assess_v8",
                                   "tracking.transition", "clean.run_batch",
                                   "audit.query", "data.scope_check")),
          "6 个关键工具", [k for k in ("cashflow.calculate", "risk.assess_v8",
                                  "tracking.transition", "clean.run_batch",
                                  "audit.query", "data.scope_check") if k not in names] or "齐备")

    # ---------- 2. 三层风控 ----------
    for sc, exp in (("healthy", "绿灯"), ("risk", "红灯")):
        r = c.call("risk.assess_v8", {"industry": "制造业", "scenario": sc, "amount": 120000})
        lv = r["最终等级"]["等级"]
        check("2. 三层风控 [%s]" % sc, lv == exp, exp,
              "Q=%s D=%s P=%s → %s" % (r["第一层"]["Q"], round(r["第二层"]["D"], 3),
                                        r["第三层"]["P"], lv))
    r = c.call("risk.assess_v8", {"industry": "建筑业", "scenario": "healthy", "amount": 80000})
    check("2.3 缓冲取严（建筑业 healthy）", r["最终等级"]["等级"] == "黄灯", "黄灯（取严）",
          "融合=%s 缓冲=%s 最终=%s" % (r["融合判定"]["等级"], r["缓冲判定"]["等级"],
                                  r["最终等级"]["等级"]))

    # ---------- 3. 三态 ----------
    t = c.call("tracking.evaluate", {})
    check("3.1 三态计算", t["记录"][0]["state"] in ("已化解", "观察中", "已恶化"), "三态之一",
          "%s/%s 覆盖率%s" % (t["记录"][0]["state"], t["记录"][0]["lamp"],
                            t["记录"][0]["verified_ratio"]))
    check("3.2 三态落库", t.get("persisted") is True, "persisted=True", t.get("persisted"))
    tl = c.call("tracking.timeline", {"object_ref": "SYN-AR-001"})
    check("3.3 状态变化过程", tl["流转次数"] >= 1, ">=1 次流转",
          " → ".join(s["状态"] for s in tl["时间线"]))
    tr = c.call("tracking.transition", {"object_ref": "SYN-AR-001", "event": "SETTLED_FULL",
                                        "actor": "verify-bot"})
    check("3.4 状态流转", tr.get("ok") and tr.get("after") == "已化解", "已化解",
          "%s → %s" % (tr.get("before"), tr.get("after")))
    bad = c.call("tracking.transition", {"object_ref": "SYN-AR-001", "event": "OVERDUE_ESCALATION",
                                         "actor": "verify-bot"})
    check("3.5 非法流转被拒", bad.get("ok") is False, "拒绝（已化解⇥已恶化）",
          bad.get("拒绝原因") or bad.get("after"))
    c.call("tracking.transition", {"object_ref": "SYN-AR-001", "event": "RESTRUCTURED",
                                   "actor": "verify-bot"})   # 复位

    # ---------- 4. 清洗 + 入库 ----------
    cb = c.call("clean.run_batch", {"operator": "verify-bot", "remark": "架构验证"})
    check("4.1 清洗批次产出", bool(cb.get("batch_id")) and cb["total_items"] == 7,
          "batch_id 且总条数=7", "%s 总%s 异常%s 卡住=%s" % (
              cb.get("batch_id"), cb.get("total_items"), cb.get("anomaly_count"), cb.get("blocked")))
    check("4.2 清洗结果落库", cb.get("persisted") is True, "persisted=True", cb.get("persisted"))
    q = c.call("clean.query_batch", {"batch_id": cb["batch_id"]})
    check("4.3 批次回查", q.get("source") == "mysql" and len(q.get("batches", [])) == 1,
          "mysql 命中 1 条", "%s/%s" % (q.get("source"), len(q.get("batches", []))))

    # ---------- 5. 留痕 ----------
    a = c.call("audit.log", {"action": "verify.run", "object_type": "verify",
                             "object_id": "v10", "actor": "verify-bot",
                             "field_name": "state", "before_value": "none",
                             "after_value": "done", "reason": "架构验证"})
    check("5.1 留痕写入", a.get("ok") and a.get("persisted"), "ok 且已落库", a.get("persisted"))
    aq = c.call("audit.query", {"actor": "verify-bot", "limit": 5})
    check("5.2 留痕条件查询", aq.get("source") == "mysql" and aq.get("total", 0) >= 1,
          "mysql 且 ≥1 条", "%s/%s" % (aq.get("source"), aq.get("total")))

    # ---------- 6. 数据范围闸门 ----------
    sc_ = c.call("data.scope_check", {"capability": "credit.rating"})
    check("6.1 未覆盖能力不部署", sc_.get("status") == "not_covered" and sc_.get("产出") is None,
          "not_covered 且无产出", "%s/%s" % (sc_.get("status"), sc_.get("reason")))
    sc2 = c.call("data.scope_check", {})
    enabled = [k for k, v in sc2["能力"].items() if v["enabled"]]
    check("6.2 已覆盖能力启用", len(enabled) >= 5, ">=5 个启用", enabled)

    # ---------- 7. 编排层 ----------
    ad = QA.QwenAdapter()
    plan = ad.plan("跑一遍三层风控评估", {"industry": "制造业"}, tools)
    check("7.1 意图解析产出计划", bool(plan.get("steps")), "有步骤",
          "%s/%s → %s" % (plan.get("source"), plan.get("intent"),
                          [s["tool"] for s in plan["steps"]]))
    v = planner.validate_plan({"steps": [{"tool": "not.exist", "args": {}}]}, names)
    check("7.2 幻觉工具计划被拒", v["ok"] is False, "校验失败", v.get("error"))
    p = P.normalize_params({"industry": "火星", "amount": -5})
    check("7.3 非法参数回落默认值", p["industry"] == "制造业" and p["amount"] == 120000.0,
          "制造业 / 120000", "%s / %s" % (p["industry"], p["amount"]))

    # ---------- 8. 网关 HTTP + SSE ----------
    if "--gateway" in sys.argv:
        B = "http://127.0.0.1:8100"
        try:
            h = json.load(urllib.request.urlopen(B + "/api/health", timeout=20))
            check("8.1 网关健康", h.get("ok") and h.get("tools", 0) >= 12, "ok 且 ≥12 工具",
                  "%s/%s 工具" % (h.get("ok"), h.get("tools")))
            check("8.2 数据库连通", (h.get("db") or {}).get("ok"), "ok",
                  (h.get("db") or {}).get("database"))
            check("8.3 缺表为空", not (h.get("db") or {}).get("missing_tables"), "[]",
                  (h.get("db") or {}).get("missing_tables"))
        except Exception as e:
            check("8.1 网关健康", False, "可达", "%s: %s" % (type(e).__name__, e))
            return

        SID = "s-verify-" + str(int(time.time()))
        evs = []

        def read():
            try:
                with urllib.request.urlopen(B + "/api/events?session_id=" + SID, timeout=30) as r:
                    buf = b""
                    while len(evs) < 60:      # 预留足够额度：一次任务约 10 个事件，重跑再来一轮
                        ch = r.read(1)
                        if not ch:
                            break
                        buf += ch
                        if buf.endswith(b"\n\n"):
                            ln = buf.decode("utf-8").strip()
                            if ln.startswith("data: "):
                                evs.append(json.loads(ln[6:]))
                            buf = b""
            except Exception as e:
                evs.append({"type": "_err", "error": str(e)})

        threading.Thread(target=read, daemon=True).start()
        time.sleep(1)
        req = urllib.request.Request(B + "/api/chat", method="POST",
                                     data=json.dumps({"session_id": SID,
                                                      "text": "跑一遍三层风控评估",
                                                      "params": {"industry": "制造业",
                                                                 "scenario": "risk"}}).encode(),
                                     headers={"Content-Type": "application/json"})
        chat = json.load(urllib.request.urlopen(req, timeout=30))
        check("8.4 指令下发", bool(chat.get("task_id")), "返回 task_id", chat.get("task_id"))
        time.sleep(8)
        types = [e.get("type") for e in evs]
        need = ("task.created", "plan.ready", "step.started", "step.done", "task.done")
        check("8.5 SSE 事件序列完整", all(n in types for n in need), "含 %s" % "/".join(need),
              types)
        lamp = next((e.get("lamp") for e in evs if e.get("type") == "task.done"), None)
        check("8.6 结果灯色回传", lamp in ("绿灯", "黄灯", "红灯"), "三色之一", lamp)

        req2 = urllib.request.Request(B + "/api/params", method="POST",
                                      data=json.dumps({"session_id": SID,
                                                       "params": {"industry": "科技服务业",
                                                                  "scenario": "risk"},
                                                       "rerun": True,
                                                       "text": "按新参数重新评估"}).encode(),
                                      headers={"Content-Type": "application/json"})
        pr = json.load(urllib.request.urlopen(req2, timeout=30))
        check("8.7 参数热调整触发重跑", bool(pr.get("task_id")), "新 task_id",
              "%s/%s" % (pr["params"]["industry"], pr.get("task_id")))
        time.sleep(8)
        types2 = [e.get("type") for e in evs]          # 重新取快照，含重跑产生的事件
        check("8.8 重跑事件入流",
              types2.count("task.created") >= 2, "≥2 次 task.created",
              types2.count("task.created"))

    print("\n" + "=" * 60)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：%s" % "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

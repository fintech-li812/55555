# -*- coding: utf-8 -*-
"""任务中枢：任务状态机 + 事件总线（SSE 数据源）+ 执行器。

一个 session 一条事件流（前端 SSE 订阅），一个 task 一次「意图 → 步骤 → 结果」的执行。
所有状态变化都会发事件，前端据此实时渲染，不做轮询假实时。
"""
from __future__ import annotations

import queue
import threading

from . import protocol as P


class Task:
    def __init__(self, task_id, session_id, request_id, plan, text):
        self.id = task_id
        self.session_id = session_id
        self.request_id = request_id
        self.text = text
        self.intent = plan.get("intent")
        self.intent_name = plan.get("intent_name") or plan.get("intent")
        self.agent = plan.get("source") or plan.get("agent")
        self.params = plan.get("params") or {}
        self.steps = [dict(s, status="pending") for s in plan.get("steps", [])]
        self.state = "created"
        self.result = None
        self.error = None
        self.created_at = P.now()
        self.updated_at = self.created_at
        self.cancel_flag = False

    def to_dict(self):
        return {"task_id": self.id, "session_id": self.session_id, "request_id": self.request_id,
                "text": self.text, "intent": self.intent, "intent_name": self.intent_name,
                "agent": self.agent, "params": self.params, "steps": self.steps,
                "state": self.state, "state_cn": P.STATE.get(self.state),
                "result": self.result, "error": self.error,
                "created_at": self.created_at, "updated_at": self.updated_at}


class Hub:
    def __init__(self, mcp, adapter):
        self.mcp = mcp
        self.adapter = adapter
        self.sessions = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------ 会话与事件
    def session(self, sid: str):
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                s = {"queue": queue.Queue(maxsize=500), "params": dict(P.DEFAULT_PARAMS),
                     "tasks": {}, "active_task": None}
                self.sessions[sid] = s
            return s

    def publish(self, sid: str, ev: dict):
        self.session(sid)["queue"].put(ev)

    def subscribe(self, sid: str):
        return self.session(sid)["queue"]

    # ------------------------------------------------------------ 参数（策略实时调整）
    def set_params(self, sid: str, params: dict):
        s = self.session(sid)
        s["params"] = P.normalize_params(params)
        return s["params"]

    def get_params(self, sid: str):
        return self.session(sid)["params"]

    # ------------------------------------------------------------ 任务
    def create_task(self, sid, request_id, plan, text) -> Task:
        t = Task(P.new_id("t"), sid, request_id, plan, text)
        s = self.session(sid)
        s["tasks"][t.id] = t
        s["active_task"] = t.id
        self.publish(sid, P.event("task.created", sid, t.id, intent=t.intent,
                                  intent_name=t.intent_name, agent=t.agent,
                                  params=t.params, text=text))
        return t

    def get_task(self, sid, task_id):
        return self.session(sid)["tasks"].get(task_id)

    def cancel(self, sid, task_id=None, reason="用户取消"):
        s = self.session(sid)
        tid = task_id or s.get("active_task")
        t = s["tasks"].get(tid)
        if t and t.state not in P.TERMINAL:
            t.cancel_flag = True
            t.state = "cancelled"
            t.error = reason
            self.publish(sid, P.event("task.state", sid, tid, state="cancelled",
                                      state_cn=P.STATE["cancelled"], reason=reason))
            return {"ok": True, "task_id": tid}
        return {"ok": False, "error": "任务不存在或已结束"}

    def _set_state(self, t, state):
        t.state = state
        t.updated_at = P.now()
        self.publish(t.session_id, P.event("task.state", t.session_id, t.id,
                                           state=state, state_cn=P.STATE.get(state)))

    # ------------------------------------------------------------ 执行
    def run(self, t: Task):
        sid = t.session_id
        try:
            self._set_state(t, "planning")
            self.publish(sid, P.event("plan.ready", sid, t.id, steps=[
                {"id": s["id"], "tool": s["tool"], "desc": s["desc"]} for s in t.steps],
                agent=t.agent, intent_name=t.intent_name))
            self._set_state(t, "running")
            last = None
            for st in t.steps:
                if t.cancel_flag:
                    break
                st["status"] = "running"
                st["started_at"] = P.now()
                self.publish(sid, P.event("step.started", sid, t.id, step_id=st["id"],
                                          tool=st["tool"], desc=st["desc"]))
                try:
                    args = dict(st.get("args") or {})
                    # 三态/评估类步骤合并当前会话参数，保证"策略实时调整"生效
                    if st["tool"] in ("risk.assess_v8", "risk.stress_test", "tracking.evaluate"):
                        args.update({k: v for k, v in t.params.items()
                                     if k in ("industry", "scenario", "amount",
                                              "n_per_group", "as_of") and v is not None})
                    res = self.mcp.call(st["tool"], args)
                    st["status"] = "done"
                    st["finished_at"] = P.now()
                    last = res
                    self.publish(sid, P.event("step.done", sid, t.id, step_id=st["id"],
                                              tool=st["tool"], result=res))
                except Exception as e:
                    st["status"] = "failed"
                    st["error"] = "%s: %s" % (type(e).__name__, e)
                    self.publish(sid, P.event("step.done", sid, t.id, step_id=st["id"],
                                              tool=st["tool"], error=st["error"]))
                    t.error = st["error"]
            if t.cancel_flag:
                return
            if t.error and last is None:
                t.state = "failed"
                self.publish(sid, P.event("task.failed", sid, t.id, error=t.error))
                return
            t.result = last
            # 千问（或本地模板）生成结论解释
            exp = self.adapter.explain({"intent": t.intent}, last or {}, t.params)
            self.publish(sid, P.event("agent.message", sid, t.id, text=exp["text"],
                                      source=exp["source"],
                                      fallback_reason=exp.get("fallback_reason")))
            self._set_state(t, "succeeded")
            self.publish(sid, P.event("task.done", sid, t.id, result=last,
                                      lamp=_lamp_of(last), intent=t.intent,
                                      intent_name=t.intent_name))
            # 关键操作留痕（模块四）
            try:
                import sys, os
                sys.path.insert(0, os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "mcp-server", "program_v1", "src"))
                from v8 import audit as _aud
                _aud.write(action="agent.task_run", object_type="task", object_id=t.id,
                           actor="qwen-orchestrator", field_name="state",
                           before_value="created", after_value="succeeded",
                           reason="%s｜%s" % (t.intent_name, (t.text or "")[:60]),
                           request_id=t.request_id)
            except Exception:
                pass
        except Exception as e:
            t.error = "%s: %s" % (type(e).__name__, e)
            self.publish(sid, P.event("task.failed", sid, t.id, error=t.error))

    def start(self, t: Task):
        threading.Thread(target=self.run, args=(t,), daemon=True).start()
        return t


def _lamp_of(result: dict):
    """从结果中提取预警灯色（不计算，只读取工具层已判定的等级）。"""
    if not isinstance(result, dict):
        return None
    fin = result.get("最终等级")
    if isinstance(fin, dict) and fin.get("等级"):
        return fin["等级"]
    for k in ("等级", "lamp"):
        if result.get(k) in ("绿灯", "黄灯", "红灯"):
            return result[k]
    recs = result.get("记录")
    if isinstance(recs, list) and recs:
        lamps = [r.get("lamp") for r in recs if r.get("lamp")]
        for lv in ("红灯", "黄灯", "绿灯"):
            if lv in lamps:
                return lv
    return None

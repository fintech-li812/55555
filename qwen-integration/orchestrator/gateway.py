# -*- coding: utf-8 -*-
"""编排网关：前端唯一后端入口（HTTP + SSE 双向）。

职责边界（对应四层架构）：
  前端  ⇄ 本网关        ：HTTPS + SSE，指令下发 / 实时状态与结果回传（双向）
  本网关 ⇄ 千问办公      ：意图解析与结果解释委托（QwenAdapter；未配置则本地编排兜底）
  本网关 ⇄ MCP 工具层    ：JSON-RPC 2.0（inprocess / stdio / http），算钱与判定全在这里
本网关自身**不做任何金额计算**，也不改写工具层返回的任何数字。

【待替换项 TODO】
  CF_GATEWAY_PORT   默认 8100
  QWEN_API_BASE / QWEN_API_KEY —— 千问对外 API（未验证是否提供，留空即降级本地编排）
  MCP_TRANSPORT     inprocess（默认） / stdio / http
  MCP_HTTP_URL / CF_MCP_TOKEN —— http 传输时必填
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # CashflowRiskWorkbench
sys.path.insert(0, HERE)

from . import protocol as P                            # noqa: E402
from . import mcp_client as MC                         # noqa: E402
from . import qwen_adapter as QA                       # noqa: E402
from . import taskhub as TH                            # noqa: E402

PORT = int(os.environ.get("CF_GATEWAY_PORT", "8100"))
HTML = os.path.join(ROOT, "web", "v10_工作台.html")

MCP = MC.MCPClient()
ADAPTER = QA.QwenAdapter()
HUB = TH.Hub(MCP, ADAPTER)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ------------------------------------------------------------ 基础
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _html(self, text):
        b = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        if u.path in ("/", "/index.html"):
            try:
                self._html(open(HTML, encoding="utf-8").read())
            except Exception as e:
                self._json({"error": "缺少前端页面 %s：%s" % (HTML, e)}, 500)
            return

        if u.path == "/api/health":
            try:
                tools = MCP.list_tools()
                mcp_ok, tools_n, srv = True, len(tools), MCP.server_info
            except Exception as e:
                mcp_ok, tools_n, srv = False, 0, {"error": str(e)}
            try:
                sys.path.insert(0, os.path.join(ROOT, "mcp-server", "program_v1", "src"))
                from v8 import db as _db
                dbst = _db.db_status()
            except Exception as e:
                dbst = {"ok": False, "error": str(e)}
            self._json({"ok": mcp_ok, "gateway": "qwen-orchestrator-v10",
                        "mcp_server": srv, "mcp_transport": MCP.transport,
                        "tools": tools_n, "qwen": ADAPTER.status(), "db": dbst,
                        "params_schema": P.PARAM_SCHEMA})
            return

        if u.path == "/api/tools":
            try:
                self._json({"tools": MCP.list_tools()})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        if u.path == "/api/task":
            t = HUB.get_task(q.get("session_id", ""), q.get("task_id", ""))
            self._json(t.to_dict() if t else {"error": "任务不存在"})
            return

        if u.path == "/api/events":
            self._sse(q.get("session_id") or "default")
            return

        self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------ SSE
    def _sse(self, sid: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q_ = HUB.subscribe(sid)

        def chunk(b: bytes):
            try:
                self.wfile.write(b"%X\r\n" % len(b) + b + b"\r\n")
                self.wfile.flush()
            except Exception:
                raise

        chunk(P.sse_frame(P.event("hello", sid, message="事件流已建立",
                                  qwen=ADAPTER.status())))
        last_hb = time.time()
        try:
            while True:
                try:
                    ev = q_.get(timeout=1.0)
                    chunk(P.sse_frame(ev))
                except Exception:
                    pass
                if time.time() - last_hb > 15:
                    chunk(P.sse_frame(P.event("heartbeat", sid)))
                    last_hb = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ------------------------------------------------------------ POST
    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()

        if u.path == "/api/chat":
            sid = b.get("session_id") or P.new_id("s")
            params = HUB.set_params(sid, b.get("params") or HUB.get_params(sid))
            text = b.get("text", "")
            try:
                tools = MCP.list_tools()
            except Exception as e:
                self._json({"error": "MCP 工具层未就绪：%s" % e}, 503)
                return
            plan = ADAPTER.plan(text, params, tools)
            if not plan.get("steps"):
                self._json({"error": plan.get("error") or "未能生成执行计划"}, 400)
                return
            t = HUB.create_task(sid, b.get("request_id") or P.new_id("r"), plan, text)
            HUB.start(t)
            self._json({"session_id": sid, "task_id": t.id, "plan": {
                "intent": t.intent, "intent_name": t.intent_name, "agent": t.agent,
                "steps": [{"id": s["id"], "tool": s["tool"], "desc": s["desc"]} for s in t.steps],
                "reply": plan.get("reply"), "source": plan.get("source"),
                "fallback_reason": plan.get("fallback_reason")}, "params": params})
            return

        if u.path == "/api/params":
            """策略实时调整：更新会话参数；若当前有任务在跑，取消它并按新参数重跑。"""
            sid = b.get("session_id") or "default"
            params = HUB.set_params(sid, b.get("params") or {})
            HUB.publish(sid, P.event("params.accepted", sid, params=params,
                                     message="参数已更新"))
            rerun = bool(b.get("rerun", True))
            task_id = None
            s = HUB.session(sid)
            active = s.get("active_task")
            t = s["tasks"].get(active) if active else None
            if rerun and t and t.state not in P.TERMINAL:
                HUB.cancel(sid, active, reason="参数已变更，按新参数重跑")
            if rerun:
                text = b.get("text") or (t.text if t else "重新评估")
                try:
                    tools = MCP.list_tools()
                    plan = ADAPTER.plan(text, params, tools)
                    nt = HUB.create_task(sid, P.new_id("r"), plan, text)
                    nt.params = params
                    HUB.start(nt)
                    task_id = nt.id
                except Exception as e:
                    self._json({"params": params, "error": "重跑失败：%s" % e})
                    return
            self._json({"params": params, "task_id": task_id, "rerun": rerun})
            return

        if u.path == "/api/cancel":
            r = HUB.cancel(b.get("session_id", "default"), b.get("task_id"),
                           reason=b.get("reason", "用户取消"))
            self._json(r)
            return

        if u.path == "/api/tool":
            """直接调用某个 MCP 工具（供前端高级面板 / 联调用）。"""
            try:
                res = MCP.call(b["tool"], b.get("arguments") or {})
                self._json({"ok": True, "result": res})
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
            return

        self._json({"error": "not found"}, 404)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write("[orchestrator] 前端入口 http://127.0.0.1:%d/\n" % PORT)
    sys.stderr.write("[orchestrator] MCP 传输=%s　千问=%s\n" % (
        MCP.transport, "已配置" if ADAPTER.enabled else "未配置（本地编排兜底）"))
    srv.serve_forever()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""HTTP 接口层（仅标准库 http.server，2G 机器上不引 Web 框架）。

- 只监听 127.0.0.1，公网访问必须由 Nginx 反代并带鉴权；
- 除 /healthz 外全部需要 Bearer 令牌，常量时间比较；
- /v1/chat 走 SSE 流式返回，前端可实时渲染。
"""
from __future__ import annotations

import json
import hmac
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Tuple
from urllib.parse import urlparse, parse_qs

from .errors import (AppError, ERR_AUTH, ERR_BAD_REQUEST, ERR_INTERNAL,
                     ERR_NOT_FOUND, OK)


class ApiContext:
    """把业务组件注入 HTTP 层。"""

    def __init__(self, cfg, logger, db, datasource, inspector, reporter,
                 cards, chat, on_stop: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.log = logger
        self.db = db
        self.ds = datasource
        self.inspector = inspector
        self.reporter = reporter
        self.cards = cards
        self.chat = chat
        self.on_stop = on_stop
        self.started_at = time.time()


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "CashflowAgent/1.0"
    ctx: ApiContext = None  # 由 make_server 注入

    # ---------- 基础 ----------
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _ok(self, data: Any, code: int = 200):
        self._send(code, _json_bytes({"ok": True, "code": OK, "data": data}))

    def _err(self, exc: AppError, http_code: int = 400):
        self._send(http_code, _json_bytes(exc.to_dict()))

    def _fatal(self, exc: BaseException):
        self.ctx.log.exception("未预期错误")
        self._send(500, _json_bytes({"ok": False, "code": ERR_INTERNAL,
                                     "error": "内部错误", "detail": str(exc)}))

    def log_message(self, fmt, *args):  # 用我们自己的日志，不打默认 access log
        self.ctx.log.info("HTTP %s", fmt % args)

    # ---------- 鉴权 ----------
    def _auth(self) -> bool:
        if self.path.split("?")[0].rstrip("/") in ("/healthz",):
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        given = header[len("Bearer "):].strip()
        expected = self.ctx.cfg.api_token
        return hmac.compare_digest(given, expected)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            raise AppError(ERR_BAD_REQUEST, "请求体不是合法 JSON")

    # ---------- 路由 ----------
    def do_GET(self):
        try:
            if not self._auth():
                return self._err(AppError(ERR_AUTH, "缺少或错误的访问令牌"), 401)
            u = urlparse(self.path)
            path = u.path.rstrip("/")
            q = parse_qs(u.query)

            if path == "/healthz":
                dbh = self.ctx.db.health()
                return self._ok({
                    "status": "ok" if dbh.get("ok") else "degraded",
                    "uptime_seconds": int(time.time() - self.ctx.started_at),
                    "database": dbh,
                    "config": self.ctx.cfg.public_view(),
                })
            if path == "/v1/config":
                return self._ok(self.ctx.cfg.public_view())
            if path == "/v1/overview":
                return self._ok(self.ctx.ds.overview(int(q.get("limit", ["20"])[0])))
            if path == "/v1/receivables":
                return self._ok(self.ctx.ds.receivables())
            if path == "/v1/metrics":
                return self._ok(self.ctx.ds.metrics())
            if path == "/v1/card":
                return self._ok(self.ctx.cards.run(trigger="api"))
            if path == "/v1/inspections":
                limit = int(q.get("limit", ["20"])[0])
                t = self.ctx.cfg.table
                rows = self.ctx.db.query(
                    f"SELECT * FROM `{t['inspection']}` ORDER BY id DESC LIMIT %s", (limit,))
                return self._ok({"items": rows})
            return self._err(AppError(ERR_NOT_FOUND, "接口不存在", {"path": path}), 404)
        except AppError as exc:
            return self._err(exc, 400 if exc.code != ERR_NOT_FOUND else 404)
        except BaseException as exc:  # noqa: BLE001
            return self._fatal(exc)

    def do_POST(self):
        try:
            if not self._auth():
                return self._err(AppError(ERR_AUTH, "缺少或错误的访问令牌"), 401)
            u = urlparse(self.path)
            path = u.path.rstrip("/")

            if path == "/v1/chat":
                return self._chat_sse()
            if path == "/v1/inspect":
                return self._ok(self.ctx.inspector.run(trigger="api"))
            if path == "/v1/report/weekly":
                body = self._body()
                days = int(body.get("days", 7))
                push = bool(body.get("push", False))
                return self._ok(self.ctx.reporter.run(days=days, push=push))
            if path == "/v1/ops/restart":
                return self._restart()
            return self._err(AppError(ERR_NOT_FOUND, "接口不存在", {"path": path}), 404)
        except AppError as exc:
            return self._err(exc, 400)
        except BaseException as exc:  # noqa: BLE001
            return self._fatal(exc)

    # ---------- 对话（SSE）----------
    def _chat_sse(self):
        body = self._body()
        message = (body.get("message") or "").strip()
        if not message:
            raise AppError(ERR_BAD_REQUEST, "message 不能为空")
        session_id = body.get("session_id") or ""

        text, meta = self.ctx.chat.answer(message, session_id)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # 让 Nginx 不缓冲 SSE
        self.end_headers()
        try:
            self._sse("meta", meta)
            # 按句切块，模拟真实流式输出
            chunks = _split(text)
            for c in chunks:
                self._sse("delta", {"text": c})
                time.sleep(0.02)
            self._sse("done", {"session_id": meta.get("session_id")})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse(self, event: str, data: Any):
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    # ---------- 高风险操作 ----------
    def _restart(self):
        from .ops_guard import require_confirm
        body = self._body()
        require_confirm("restart", body, self.ctx.log)
        self.ctx.log.warning("收到重启指令，进程将退出，由 systemd 依据 Restart=always 拉起")
        self._ok({"accepted": True, "action": "restart",
                  "note": "进程退出后由 systemd 自动重启"})
        # 交给主线程优雅退出
        if self.ctx.on_stop:
            threading.Thread(target=self.ctx.on_stop, args=("restart",), daemon=True).start()


def _split(text: str, size: int = 24):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def make_server(ctx: ApiContext) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    srv = ThreadingHTTPServer((ctx.cfg.host, int(ctx.cfg.port)), handler)
    srv.daemon_threads = True
    return srv

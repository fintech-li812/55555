# -*- coding: utf-8 -*-
"""MCP 客户端：网关 → 工具层（MCP Server）的调用通道。

三种传输，同一个 MCP 协议（JSON-RPC 2.0 / 2025-06-18）：
  1. inprocess（默认）：直接 import 工具层并调用其 JSON-RPC 分发函数，
     零进程开销、Windows 友好；**协议与 stdio/http 完全一致**（同一 handle()）。
  2. stdio：拉起 `v8_mcp_server.py` 子进程，走标准 MCP stdio 传输（与千问桌面端一致）。
  3. http：POST https://<域名>/mcp，Bearer 鉴权（远程部署后使用）。

启动时按 MCP_TRANSPORT 选择；任一传输握手失败会如实报错，不静默降级。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "mcp-server", "program_v1", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

PY = os.environ.get("CF_PYTHON") or sys.executable


class McpError(Exception):
    pass


class _Stdio:
    """stdio 传输：拉起子进程，一问一答（行分隔 JSON-RPC）。"""

    def __init__(self):
        self.p = subprocess.Popen(
            [PY, "-B", "-u", os.path.join(SRC, "v8_mcp_server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self.lock = threading.Lock()

    def call(self, msg: dict):
        with self.lock:
            self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.p.stdin.flush()
            while True:
                line = self.p.stdout.readline()
                if not line:
                    raise McpError("MCP stdio 子进程已退出")
                try:
                    return json.loads(line)
                except Exception:
                    continue

    def close(self):
        try:
            self.p.terminate()
        except Exception:
            pass


class _Http:
    """Streamable HTTP 传输：POST /mcp + Bearer。"""

    def __init__(self, url: str, token: str):
        self.url, self.token = url, token

    def call(self, msg: dict):
        import urllib.request
        req = urllib.request.Request(
            self.url, data=json.dumps(msg, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    def close(self):
        pass


class _InProcess:
    """进程内传输：调用工具层同一个 handle()，协议一致、零开销。"""

    def __init__(self):
        import v8_mcp_server as _srv
        self.srv = _srv

    def call(self, msg: dict):
        return self.srv.handle(msg)

    def close(self):
        pass


class MCPClient:
    """统一入口：initialize → tools/list → tools/call。"""

    def __init__(self, transport: str = None):
        self.transport = transport or os.environ.get("MCP_TRANSPORT", "inprocess")
        self.server_info = None
        self.tools = []
        self._t = None
        self._ready = False

    def _ensure(self):
        if self._ready:
            return
        if self.transport == "stdio":
            self._t = _Stdio()
        elif self.transport == "http":
            url = os.environ.get("MCP_HTTP_URL", "http://127.0.0.1:8090/mcp")
            tok = os.environ.get("CF_MCP_TOKEN", "")
            if not tok:
                raise McpError("HTTP 传输需要 CF_MCP_TOKEN")
            self._t = _Http(url, tok)
        else:
            self._t = _InProcess()
        r = self._t.call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "qwen-orchestrator", "version": "v10"}}})
        if "error" in r:
            raise McpError("initialize 失败：%s" % r["error"])
        self.server_info = r["result"]["serverInfo"]
        r2 = self._t.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.tools = r2["result"]["tools"]
        self._ready = True

    def list_tools(self):
        self._ensure()
        return self.tools

    def call(self, name: str, arguments: dict = None):
        """调用工具；返回 structuredContent（dict）。"""
        self._ensure()
        r = self._t.call({"jsonrpc": "2.0", "id": uuid.uuid4().hex[:8],
                          "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})
        if "error" in r:
            raise McpError("%s" % r["error"].get("message"))
        res = r["result"]
        if res.get("isError"):
            raise McpError("%s" % (res.get("structuredContent") or res.get("content")))
        return res.get("structuredContent") or res

    def close(self):
        if self._t:
            self._t.close()

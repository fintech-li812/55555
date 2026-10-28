#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""千问办公可直接导入的现金流 MCP Server。

链路：千问办公 -> 本进程（stdio）-> 服务器 MCP -> Decimal 引擎 / MySQL。

本进程只做协议转换、参数校验与调用转发，不计算金额、不内置演示台账。
默认引擎：https://workbench.example.com/mcp，可用 CF_ENGINE_MCP_URL 覆盖。
依赖：仅 Python 标准库。日志只写 stderr，stdout 仅供 JSON-RPC 协议使用。
"""
import argparse
import datetime
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "cashflow-workbench-qwen", "version": "2.0.0"}
DEFAULT_ENGINE = "https://workbench.example.com/mcp"
SOURCE_IDS = ["synthetic_eight_sheet_v1", "program_demo_v1",
              "program_difference_v1", "manufacturing_sample_v1"]


def T(kind, description, **extra):
    value = {"type": kind, "description": description}
    value.update(extra)
    return value


SOURCE_SCHEMA = T("string", "已登记数据来源", enum=SOURCE_IDS,
                  default="synthetic_eight_sheet_v1")
SAVE_PROPERTIES = {
    "result": T("object", "calculate_cashflow 返回的完整 result 对象，不得手改金额"),
    "request_id": T("string", "本次保存请求号", minLength=3, maxLength=160),
}
QUERY_PROPERTIES = {
    "request_id": T("string", "保存时使用的请求号"),
    "period": T("string", "期间，如 2026-08"),
    "source_revision": T("string", "计算结果中的 source_revision"),
    "run_id": T("string", "计算结果中的 run_id"),
}

# 独立工具名只用字母、数字和下划线，避免部分客户端不接受点号。
TOOLS = [
    {"name": "workbench_status", "description": "读取工作台架构、来源档案和数据库连接状态。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_cashflow_sources", "description": "列出可用于计算或审计的已登记数据来源。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_cashflow_source", "description": "只读检查一个来源，不修改原始文件。",
     "inputSchema": {"type": "object", "properties": {"source_id": SOURCE_SCHEMA}}},
    {"name": "check_cashflow_readiness", "description": "计算前检查来源是否就绪；阻断问题必须如实返回。",
     "inputSchema": {"type": "object", "properties": {"source_id": SOURCE_SCHEMA}}},
    {"name": "audit_cashflow_source", "description": "执行八表数据检查，返回问题清单和映射预览，不写库。",
     "inputSchema": {"type": "object", "properties": {"source_id": SOURCE_SCHEMA}}},
    {"name": "calculate_cashflow", "description": (
        "在服务器上用 Python Decimal 实际计算现金流。默认完整合成八表；"
        "返回期初、流入、流出、期末、差异及 can_save_source_result。不写库。"),
     "inputSchema": {"type": "object", "properties": {"source_id": SOURCE_SCHEMA}}},
    {"name": "read_cashflow_result", "description": "重新计算指定来源并返回来源及算法摘要，不写库。",
     "inputSchema": {"type": "object", "properties": {"source_id": SOURCE_SCHEMA}}},
    {"name": "save_cashflow_result", "description": (
        "将计算结果保存到隔离 MySQL。result 必须原样使用 calculate_cashflow 的 result；"
        "余额不一致或来源不允许时服务端会拒绝。"),
     "inputSchema": {"type": "object", "properties": SAVE_PROPERTIES,
                     "required": ["result", "request_id"]}},
    {"name": "query_cashflow_result", "description": "按四项标识回查已保存结果，用于确认留档一致。",
     "inputSchema": {"type": "object", "properties": QUERY_PROPERTIES,
                     "required": list(QUERY_PROPERTIES)}},
    {"name": "frontend_dispatch", "description": (
        "统一 action 入口。支持 workbench.status、source.list、source.inspect、"
        "source.readiness、source.audit、cashflow.calculate、cashflow.read、"
        "cashflow.save、cashflow.query、echo。"),
     "inputSchema": {"type": "object", "properties": {
         "action": T("string", "动作名"),
         "params": T("object", "动作参数"),
         "request_id": T("string", "请求追踪号"),
         "session_id": T("string", "会话号"),
         "actor": T("string", "操作人")},
         "required": ["action"]}},
]

ERRORS = {
    0: "OK", 4000: "BAD_REQUEST", 4001: "INVALID_PARAM",
    4002: "UNKNOWN_ACTION", 4003: "UNAUTHORIZED", 5000: "INTERNAL_ERROR",
    5020: "ENGINE_UNAVAILABLE 服务器 MCP 调用失败",
}


def ok_resp(action, data, request_id=None, elapsed_ms=0, extra_meta=None):
    meta = {"request_id": request_id or ("req-" + uuid.uuid4().hex[:12]),
            "elapsed_ms": round(elapsed_ms, 2), "server": SERVER_INFO["name"],
            "version": SERVER_INFO["version"],
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds")}
    if extra_meta:
        meta.update(extra_meta)
    return {"ok": True, "code": 0, "action": action, "data": data,
            "error": None, "meta": meta}


def err_resp(code, message, detail=None, action=None, request_id=None, elapsed_ms=0):
    return {"ok": False, "code": code, "action": action, "data": None,
            "error": {"code": ERRORS.get(code, "ERROR").split()[0],
                      "message": message, "detail": detail},
            "meta": {"request_id": request_id or ("req-" + uuid.uuid4().hex[:12]),
                     "elapsed_ms": round(elapsed_ms, 2), "server": SERVER_INFO["name"],
                     "version": SERVER_INFO["version"],
                     "timestamp": datetime.datetime.now().isoformat(timespec="seconds")}}


class ValidationError(Exception):
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail or {}


_TYPE_MAP = {"string": str, "number": (int, float), "integer": int,
             "object": dict, "array": list, "boolean": bool}


def validate(value, schema, path="$"):
    if not isinstance(schema, dict):
        return value
    kind = schema.get("type")
    if kind:
        expected = _TYPE_MAP.get(kind)
        if expected is None:
            raise ValidationError("不支持的类型 %s" % kind, {"path": path})
        if kind in ("number", "integer") and isinstance(value, bool):
            raise ValidationError("期望数值，实得 boolean", {"path": path})
        if not isinstance(value, expected):
            raise ValidationError("参数类型错误：期望 %s，实得 %s" %
                                  (kind, type(value).__name__), {"path": path})
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError("取值不在允许范围内：%s" % schema["enum"],
                              {"path": path, "allowed": schema["enum"]})
    if kind in ("number", "integer"):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError("小于最小值 %s" % schema["minimum"], {"path": path})
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError("大于最大值 %s" % schema["maximum"], {"path": path})
    if kind == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValidationError("长度不足 %s" % schema["minLength"], {"path": path})
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValidationError("长度超过 %s" % schema["maxLength"], {"path": path})
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValidationError("格式不匹配 %s" % schema["pattern"], {"path": path})
    if kind == "object" and isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                raise ValidationError("缺少必填参数 %s" % required,
                                      {"path": path, "missing": required})
        for key, subschema in (schema.get("properties") or {}).items():
            if key in value:
                validate(value[key], subschema, path + "." + key)
    if kind == "array" and isinstance(value, list) and schema.get("items"):
        for index, item in enumerate(value):
            validate(item, schema["items"], "%s[%d]" % (path, index))
    return value


class EngineError(Exception):
    pass


class EngineMcpClient:
    """无第三方依赖的 streamable-http MCP 客户端。

    每次业务调用建立新 session，避免千问长会话中服务器重启后的 session 失效。
    """

    def __init__(self, url=None, token=None, timeout=None):
        self.url = url or os.environ.get("CF_ENGINE_MCP_URL", DEFAULT_ENGINE)
        self.token = token if token is not None else os.environ.get("CF_ENGINE_MCP_TOKEN", "")
        self.timeout = timeout or float(os.environ.get("CF_ENGINE_TIMEOUT", "25"))
        self.session = None
        self._id = 0

    def _post(self, method, params=None, notify=False):
        if not notify:
            self._id += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = self._id
        if params is not None:
            body["params"] = params
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["mcp-session-id"] = self.session
        if self.token:
            headers["X-MCP-Token"] = self.token
        request = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.headers.get("mcp-session-id"):
                    self.session = response.headers["mcp-session-id"]
                raw = response.read().decode("utf-8").strip()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise EngineError("引擎 HTTP %s：%s" % (exc.code, detail))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EngineError("无法连接服务器 MCP：%s" % exc)
        if not raw:
            return None
        candidates = []
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    candidates.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    pass
        if not candidates:
            try:
                candidates = [json.loads(raw)]
            except json.JSONDecodeError:
                raise EngineError("引擎返回不是合法 JSON/SSE：%s" % raw[:300])
        answer = candidates[-1]
        if answer and "error" in answer:
            raise EngineError("引擎 JSON-RPC 错误：%s" % answer["error"])
        return answer

    def call(self, tool, arguments):
        self._post("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                  "clientInfo": {"name": SERVER_INFO["name"],
                                                 "version": SERVER_INFO["version"]}})
        response = self._post("tools/call", {"name": tool, "arguments": arguments or {}})
        result = (response or {}).get("result") or {}
        text = "\n".join(str(item.get("text", "")) for item in result.get("content", [])
                         if isinstance(item, dict) and item.get("type") == "text")
        if result.get("isError"):
            raise EngineError("引擎工具执行失败：%s" % (text or result))
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            if set(structured) == {"result"} and isinstance(structured["result"], dict):
                return structured["result"]
            return structured
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
        return result


def engine_call(tool, arguments):
    return EngineMcpClient().call(tool, arguments)


ACTION_ENGINE = {
    "workbench.status": "get_workbench_status",
    "source.list": "list_manufacturing_sources",
    "source.inspect": "inspect_manufacturing_source",
    "source.readiness": "get_manufacturing_readiness",
    "source.audit": "get_manufacturing_data_audit",
    "cashflow.calculate": "calculate_manufacturing_cashflow",
    "cashflow.read": "read_cashflow_result",
    "cashflow.save": "save_cashflow_result",
    "cashflow.query": "query_cashflow_result",
}
DIRECT_ACTIONS = {
    "workbench_status": "workbench.status",
    "list_cashflow_sources": "source.list",
    "inspect_cashflow_source": "source.inspect",
    "check_cashflow_readiness": "source.readiness",
    "audit_cashflow_source": "source.audit",
    "calculate_cashflow": "cashflow.calculate",
    "read_cashflow_result": "cashflow.read",
    "save_cashflow_result": "cashflow.save",
    "query_cashflow_result": "cashflow.query",
}

SOURCE_ONLY = {"type": "object", "properties": {"source_id": SOURCE_SCHEMA}}
ACTION_SCHEMAS = {
    "echo": {"type": "object", "properties": {"text": T("string", "文本")},
             "required": ["text"]},
    "workbench.status": {"type": "object", "properties": {}},
    "source.list": {"type": "object", "properties": {}},
    "source.inspect": SOURCE_ONLY,
    "source.readiness": SOURCE_ONLY,
    "source.audit": SOURCE_ONLY,
    "cashflow.calculate": SOURCE_ONLY,
    "cashflow.read": SOURCE_ONLY,
    "cashflow.save": {"type": "object", "properties": SAVE_PROPERTIES,
                      "required": ["result", "request_id"]},
    "cashflow.query": {"type": "object", "properties": QUERY_PROPERTIES,
                       "required": list(QUERY_PROPERTIES)},
}


def dispatch(action, params, request_id=None, session_id=None, actor=None):
    started = time.time()

    def elapsed():
        return (time.time() - started) * 1000

    if action == "echo":
        router = lambda p: {"echo": p.get("text")}
    elif action in ACTION_ENGINE:
        router = lambda p: engine_call(ACTION_ENGINE[action], p)
    else:
        return err_resp(4002, "未知 action：%s" % action,
                        {"allowed": sorted(list(ACTION_ENGINE) + ["echo"])},
                        action, request_id, elapsed())
    try:
        clean = dict(params or {})
        if "source_id" not in clean and "source_id" in (
                ACTION_SCHEMAS.get(action, {}).get("properties", {})):
            clean["source_id"] = "synthetic_eight_sheet_v1"
        validate(clean, ACTION_SCHEMAS.get(action) or {"type": "object"}, "$.params")
        data = router(clean)
    except ValidationError as exc:
        return err_resp(4001, str(exc), exc.detail, action, request_id, elapsed())
    except EngineError as exc:
        return err_resp(5020, str(exc), {"engine_url": os.environ.get(
            "CF_ENGINE_MCP_URL", DEFAULT_ENGINE)}, action, request_id, elapsed())
    except Exception as exc:
        return err_resp(5000, "%s: %s" % (type(exc).__name__, exc), None,
                        action, request_id, elapsed())
    return ok_resp(action, data, request_id, elapsed(),
                   {"session_id": session_id, "actor": actor,
                    "engine": "remote-mcp", "calculation": "server-decimal"})


def call_tool(name, args):
    args = args or {}
    if name in ("frontend_dispatch", "frontend.dispatch"):
        return dispatch(args.get("action"), args.get("params") or {},
                        args.get("request_id"), args.get("session_id"), args.get("actor"))
    action = DIRECT_ACTIONS.get(name)
    if action:
        response = dispatch(action, args)
        return response["data"] if response["ok"] else response
    raise KeyError("未知工具：%s" % name)


def handle(msg):
    mid, method = msg.get("id"), msg.get("method")
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def fail(code, message, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": mid, "error": error}

    if method == "initialize":
        return ok({"protocolVersion": PROTOCOL_VERSION,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": SERVER_INFO,
                   "instructions": (
                       "真实现金流 MCP。先用 list_cashflow_sources 选择来源，"
                       "需要时 check_cashflow_readiness，再 calculate_cashflow；"
                       "保存后必须 query_cashflow_result 回查。金额由服务器 Decimal 计算。")})
    if (method or "").startswith("notifications/"):
        return None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        try:
            result = call_tool(params.get("name"), params.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": json.dumps(
                result, ensure_ascii=False, default=str)}],
                "structuredContent": result, "isError": False})
        except KeyError as exc:
            return fail(-32602, "Invalid params", {"detail": str(exc)})
        except Exception as exc:
            return fail(-32000, "%s: %s" % (type(exc).__name__, exc))
    if mid is None:
        return None
    return fail(-32601, "Method not found: %s" % method)


def serve_stdio():
    sys.stderr.write("[mcp] %s %s stdio 就绪；引擎=%s\n" %
                     (SERVER_INFO["name"], SERVER_INFO["version"],
                      os.environ.get("CF_ENGINE_MCP_URL", DEFAULT_ENGINE)))
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as exc:
            sys.stderr.write("[mcp] 非法 JSON：%s\n" % exc)
            sys.stderr.flush()
            continue
        response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()


class _Handler(BaseHTTPRequestHandler):
    TOKEN = ""

    def log_message(self, *args):
        sys.stderr.write("[mcp-http] %s\n" % (args[0] % args[1:]))
        sys.stderr.flush()

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            body = {"ok": True, "server": SERVER_INFO,
                    "tools": [tool["name"] for tool in TOOLS],
                    "engine": os.environ.get("CF_ENGINE_MCP_URL", DEFAULT_ENGINE)}
            self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"))
        else:
            self._send(404, b'{"ok":false,"error":"not found"}')

    def do_POST(self):
        if (self.headers.get("X-MCP-Token") or "") != self.TOKEN:
            self._send(401, json.dumps({"ok": False, "code": 4003,
                                        "error": "缺少或错误的 X-MCP-Token"},
                                       ensure_ascii=False).encode("utf-8"))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": str(exc)}},
                                       ensure_ascii=False).encode("utf-8"))
            return
        response = handle(msg)
        self._send(200, json.dumps(response or {}, ensure_ascii=False,
                                   default=str).encode("utf-8"))


def serve_http(port, token):
    if not token:
        raise SystemExit("HTTP 模式必须设置 CF_MCP_TOKEN 或 --token，拒绝无鉴权启动")
    _Handler.TOKEN = token
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    sys.stderr.write("[mcp] HTTP 监听 http://127.0.0.1:%d/mcp\n" % port)
    sys.stderr.flush()
    server.serve_forever()


REGISTER_SNIPPET = {
    "mcpServers": {
        "cashflow-workbench": {
            "command": "D:/AI_Command_Workspace/runtime/cashflow_v1/Scripts/python.exe",
            "args": ["-B", "-u",
                     "D:/CashflowRiskWorkbench/qwen-integration/mcp_frontend_server.py"],
            "env": {"PYTHONIOENCODING": "utf-8",
                    "CF_ENGINE_MCP_URL": DEFAULT_ENGINE},
            "transport": "stdio",
            "disabled": False,
        }
    }
}


def main():
    parser = argparse.ArgumentParser(description="千问办公现金流 MCP Server")
    parser.add_argument("--stdio", action="store_true", help="stdio 传输（默认）")
    parser.add_argument("--http", action="store_true", help="HTTP 传输（必须 token）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CF_MCP_PORT", "8090")))
    parser.add_argument("--token", default=os.environ.get("CF_MCP_TOKEN", ""))
    parser.add_argument("--print-config", action="store_true", help="打印千问导入配置")
    args = parser.parse_args()
    if args.print_config:
        print(json.dumps(REGISTER_SNIPPET, ensure_ascii=False, indent=2))
        return 0
    if args.http:
        serve_http(args.port, args.token)
    else:
        serve_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())

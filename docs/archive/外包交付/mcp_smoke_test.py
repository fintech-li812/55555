#!/usr/bin/env python3
"""mcp_smoke_test.py — CashflowRiskWorkbench MCP Server 独立验收测试客户端（外包任务 02）。

用途
    对指定的 MCP Server（Streamable HTTP 传输，JSON-RPC 2.0 over HTTP POST）执行 15 项
    验收用例（A01–A12 自动判定 + M01–M04 人工核对清单），输出 Markdown 结构化报告。
    本脚本只做「打协议、验行为、出报告」：不实现服务端逻辑、不连数据库、不 mock 服务器。

用法
    python mcp_smoke_test.py --base-url "$MCP_BASE_URL" --run-tag demo-01
    MCP_BASE_URL=<你的 MCP 入口> MCP_TOKEN=<你的令牌> python mcp_smoke_test.py

    脚本内不含任何默认地址/令牌：地址与令牌只能来自命令行参数或环境变量。

参数
    --base-url   MCP 入口地址，默认取环境变量 MCP_BASE_URL（禁止硬编码，缺省即报错退出）
    --token      Bearer 令牌，默认取环境变量 MCP_TOKEN（值永不写入报告/日志）
    --run-tag    request_id 前缀，默认 YYYYMMDD-HHMMSS，用于生成可复现的 request_id
    --timeout    单次 HTTP 超时秒数，默认 15（仅影响等待上限，不改变重试纪律）

request_id 规则
    f"{run_tag}-A{用例编号}"，例如 demo-01-A02；A03/A04 复用 A02 的 id，这是幂等用例成立的前提。

退出码
    0  全部 PASS/SKIP（无 FAIL）
    1  存在 FAIL
    2  连不上服务器（连接被拒 / DNS 解析失败 / 重试 3 次仍超时），此时不做任何业务判定

依赖
    仅 Python 标准库：urllib.request / json / sys / os / time / datetime / argparse / uuid / hashlib。
    无第三方依赖，不需要安装任何包（仅标准库）。要求 Python >= 3.10。

重试纪律（项目铁律）
    仅「未知网络错误」（超时 / 无响应 / 连接重置）重试：先做一次只读 GET 探测作为排查信息，
    再等待 >= 30 秒，同一问题最多 3 次；超限即判 FAIL 并跳过依赖该步的用例。
    明确的连接被拒 / DNS 失败直接判「连不上」（exit 2），HTTP 4xx/5xx 属明确响应，一律不重试。
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# 常量（只读配置，无全局可变状态）
# --------------------------------------------------------------------------- #

EXPECTED_TOOLS = (
    "log_to_mysql",
    "query_rule_stats",
    "query_audit_by_event",
    "log_feedback",
    "query_feedback_stats",
    "save_param_template",
    "load_param_template",
    "log_exception",
    "query_exception_stats",
    "log_stress_test",
    "verify_payment",
    "rf_predict",
    "load_da_coefficients",
    "save_da_coefficients",
)

CLIENT_PROTOCOL_VERSION = "2025-06-18"  # 客户端提议版本；实际以服务端返回值为准
RETRY_INTERVAL_SEC = 30  # 项目铁律：重试间隔 >= 30 秒
MAX_ATTEMPTS = 3  # 项目铁律：同一问题最多 3 次
CST = timezone(timedelta(hours=8))  # 提交给服务端的时间一律 RFC3339 +08:00
EXCERPT_LIMIT = 200  # 报告中单段响应/请求转储上限（字符）
SENSITIVE_HEADERS = ("authorization", "proxy-authorization", "cookie", "set-cookie")

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def emit(msg):
    """进度/排查信息写 stderr，保持 stdout 只有 Markdown 报告。"""
    print(msg, file=sys.stderr, flush=True)


def now_cst():
    return datetime.now(CST)


def rfc3339(dt):
    return dt.isoformat(timespec="seconds")


def window(hours_back, hours_forward=0):
    """生成 [now-x, now+y) 的 RFC3339 窗口；字段名由调用方按 inputSchema 决定。"""
    base = now_cst()
    return rfc3339(base - timedelta(hours=hours_back)), rfc3339(base + timedelta(hours=hours_forward))


def truncate(text, limit=EXCERPT_LIMIT):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + f"…(截断, 共{len(text)}字符)"


def fingerprint(value):
    """对会话/请求标识做不可逆摘要，报告中只用指纹，避免泄露完整标识。"""
    if not value:
        return "-"
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


def walk(obj):
    """递归遍历 dict/list，产出 (key, value) 对。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def collect_values(obj, keys):
    """收集对象中所有指定键名对应的标量值（字符串形式）。"""
    keys = set(keys)
    return [str(v) for k, v in walk(obj) if k in keys and isinstance(v, (str, int, float, bool))]


def contains_token(obj, needle):
    """在对象任意键值中查找子串（大小写不敏感）。"""
    needle = needle.lower()
    for k, v in walk(obj):
        if needle in str(k).lower() or needle in str(v).lower():
            return True
    return False


def keys_of(obj):
    if isinstance(obj, dict):
        return sorted(obj.keys())
    return []


def placeholder(prop, name_hint=""):
    """按 inputSchema 的属性类型为必填字段生成合规占位值。"""
    prop = prop if isinstance(prop, dict) else {}
    lowered = name_hint.lower()
    if any(t in lowered for t in ("time", "_at", "date")):
        return rfc3339(now_cst())
    if any(t in lowered for t in ("amount", "balance", "money")):
        return "0.00"
    if prop.get("enum"):
        return prop["enum"][0]
    kind = prop.get("type")
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if kind == "object":
        return {}
    if kind == "array":
        return []
    return "smoke-test"


def fill_required(schema, overrides=None):
    """按 inputSchema 的 required 生成最小合法参数集，再套用 overrides。"""
    schema = schema if isinstance(schema, dict) else {}
    props = schema.get("properties") or {}
    required = schema.get("required")
    if not isinstance(required, list) or not required:
        required = sorted(props.keys())
    args = {name: placeholder(props.get(name, {}), name) for name in required}
    args.update(overrides or {})
    return args


def pick_key(schema, candidates, default):
    """字段名以服务端 inputSchema 为准；未命中时用默认值并标注 ⚠ 协议未明确。"""
    props = (schema or {}).get("properties") or {}
    for name in props:
        lowered = name.lower()
        if any(c in lowered for c in candidates):
            return name, False
    return default, True


def safe_headers(headers):
    """报告只保留非敏感头的名称与必要取值，绝不输出 Authorization 等凭据头的值。"""
    keep = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk in SENSITIVE_HEADERS:
            keep[k] = "(已脱敏, 不输出)"
        elif lk == "mcp-session-id":
            keep[k] = fingerprint(v) + f"(长度{len(v)})"
        elif lk in ("content-type", "mcp-protocol-version"):
            keep[k] = v
    return keep


# --------------------------------------------------------------------------- #
# 传输层
# --------------------------------------------------------------------------- #


class Unreachable(Exception):
    """明确连不上：连接被拒 / DNS 失败。不重试，直接 exit 2。"""


class NetworkExhausted(Exception):
    """未知网络错误重试 3 次仍失败。判 FAIL 并停止依赖该步的用例。"""


class McpClient:
    """极简 MCP Streamable HTTP 客户端（无继承、无全局状态）。"""

    def __init__(self, base_url, token, timeout):
        self.base_url = base_url
        self.token = token or ""
        self.timeout = timeout
        self.protocol_version = None
        self.session_id = None
        self._req_id = 0
        # 环回地址直连：避免被系统 HTTP_PROXY 拦截后误判为「服务端异常」
        host = (urllib.parse.urlparse(base_url).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    # -- headers ---------------------------------------------------------- #

    def _headers(self, extra=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        headers.update(extra or {})
        return headers

    def _next_id(self):
        self._req_id += 1
        return self._req_id

    # -- raw http --------------------------------------------------------- #

    def _classify(self, exc):
        reason = getattr(exc, "reason", exc)
        name = type(reason).__name__
        if name == "ConnectionRefusedError":
            return "refused"
        if name in ("gaierror", "herror"):
            return "dns"
        if isinstance(exc, TimeoutError) or name in ("timeout", "TimeoutError"):
            return "timeout"
        return "unknown"

    def _raw(self, method, body=None):
        req = urllib.request.Request(
            self.base_url, data=body, method=method, headers=self._headers()
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, dict(resp.headers.items()), raw
        except urllib.error.HTTPError as exc:
            # HTTP 4xx/5xx 是明确响应，不算「未知网络错误」，不重试
            body_txt = ""
            try:
                body_txt = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - 错误体读取失败不影响状态码判定
                body_txt = ""
            return exc.code, dict(exc.headers.items() or {}), body_txt
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            kind = self._classify(exc)
            if kind in ("refused", "dns"):
                raise Unreachable(f"{kind}: {exc}") from exc
            raise NetworkExhausted(f"未知网络错误({kind}): {exc}") from exc

    def _diagnose(self, reason):
        """重试前的只读排查：GET 探测一次，只记录状态码/错误原因，不维持长连接。"""
        try:
            status, _, _ = self._raw("GET")
            emit(f"[diagnostic] 只读 GET 探测 -> HTTP {status}（排查依据：{truncate(reason, 120)}）")
        except Unreachable as exc:
            emit(f"[diagnostic] 只读 GET 探测 -> 不可达（{truncate(str(exc), 120)}）")
        except Exception as exc:  # noqa: BLE001 - 诊断失败不阻断主流程
            emit(f"[diagnostic] 只读 GET 探测 -> {truncate(str(exc), 120)}")

    def request(self, method, body=None, retryable=True):
        attempt = 1
        while True:
            try:
                return self._raw(method, body)
            except Unreachable:
                raise
            except NetworkExhausted as exc:
                if not retryable or attempt >= MAX_ATTEMPTS:
                    raise NetworkExhausted(
                        f"{exc}（已尝试 {attempt}/{MAX_ATTEMPTS} 次，间隔 {RETRY_INTERVAL_SEC}s）"
                    ) from exc
                self._diagnose(str(exc))
                emit(
                    f"[retry] 等待 {RETRY_INTERVAL_SEC}s 后第 {attempt + 1}/{MAX_ATTEMPTS} 次尝试"
                    "（项目铁律：间隔>=30s，同一问题最多 3 次）"
                )
                time.sleep(RETRY_INTERVAL_SEC)
                attempt += 1

    # -- protocol --------------------------------------------------------- #

    def rpc(self, method, params=None, notify=False):
        """发一个 JSON-RPC 消息，返回 (status, headers, parsed, kind)。"""
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self._next_id()  # 通知无 id、无响应体
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, headers, raw = self.request("POST", body)
        parsed, kind = parse_payload(raw, (headers or {}).get("Content-Type", ""))
        if parsed and not notify:
            if isinstance(parsed.get("result"), dict):
                version = parsed["result"].get("protocolVersion")
                if isinstance(version, str) and version:
                    self.protocol_version = version
            for key, value in (headers or {}).items():
                if key.lower() == "mcp-session-id" and value:
                    self.session_id = value
        return status, headers, parsed, kind

    def get_probe(self):
        """GET 探测：只记录状态码，不要求维持 SSE 长连接。"""
        try:
            status, headers, raw = self.request("GET")
            return status, headers, raw, None
        except Unreachable as exc:
            return None, {}, "", exc
        except NetworkExhausted as exc:
            return None, {}, "", exc


def parse_payload(raw, content_type):
    """同时支持纯 JSON 与 SSE 帧格式；SSE 取最后一条非空 data: 行。"""
    raw = raw or ""
    ct = (content_type or "").lower()
    if "text/event-stream" in ct or raw.lstrip().startswith(("data:", "event:")):
        last = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                candidate = line[5:].strip()
                if candidate and candidate != "[DONE]":
                    last = candidate
        if last is None:
            return None, "sse-empty"
        try:
            return json.loads(last), "sse"
        except json.JSONDecodeError:
            return None, "sse-unparsable"
    try:
        return json.loads(raw), "json"
    except json.JSONDecodeError:
        return None, "json-unparsable"


def split_tool_result(response):
    """从 tools/call 响应中拆出 (业务对象, isError, rpc_error)。"""
    if not isinstance(response, dict):
        return None, False, {"code": -32603, "message": "响应不是 JSON-RPC 对象"}
    if "error" in response:
        return None, False, response.get("error")
    result = response.get("result") or {}
    is_error = bool(result.get("isError"))
    business = None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        business = structured
    if business is None:
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = content[0].get("text")
            if isinstance(text, str):
                try:
                    business = json.loads(text)
                except json.JSONDecodeError:
                    business = None
    return business, is_error, None


# --------------------------------------------------------------------------- #
# 用例编排
# --------------------------------------------------------------------------- #


def result(case_id, title, status, reason, request_summary="", response_excerpt="", extra=None):
    return {
        "id": case_id,
        "title": title,
        "status": status,
        "reason": reason,
        "request": request_summary,
        "response": response_excerpt,
        "extra": extra or [],
    }


class Runner:
    def __init__(self, base_url, token, timeout, run_tag):
        self.client = McpClient(base_url, token, timeout)
        self.run_tag = run_tag
        self.tools = {}  # name -> inputSchema
        self.setup_info = {}
        self.results = []
        self.a02_request_id = None
        self.a02_audit_id = None
        self.a02_ok = False

    # -- helpers ---------------------------------------------------------- #

    def rid(self, case_no):
        """request_id = f"{run_tag}-A{用例编号}"，如 demo-01-A02（case_no 传两位数字）。"""
        return f"{self.run_tag}-A{case_no}"

    def tool_schema(self, name):
        return (self.tools.get(name) or {}).get("inputSchema") or {}

    def available(self, name):
        return name in self.tools

    def call(self, name, arguments):
        """调用工具，返回 (business, is_error, rpc_error, http_status)。"""
        status, _headers, parsed, _kind = self.client.rpc(
            "tools/call", {"name": name, "arguments": arguments}
        )
        business, is_error, rpc_error = split_tool_result(parsed)
        return business, is_error, rpc_error, status

    def auth_result(self, case_no, title, status):
        """HTTP 401/403 视为「鉴权生效」：记 FAIL 并明确结论，不做工具行为判定。"""
        if status in (401, 403):
            return result(
                case_no, title, FAIL,
                f"鉴权生效：服务端返回 HTTP {status}，未获得工具结果（认证拒绝场景下这是预期行为）",
                "tools/call（被鉴权层拦截）", f"HTTP {status}；无业务响应体",
            )
        return None

    def skip_if_missing(self, case_no, title, name):
        return result(
            case_no,
            title,
            SKIP,
            f"工具 {name} 未在 tools/list 中出现（工具未实装），按验收规则记 SKIP，不判 FAIL",
            request_summary=f"未发送 tools/call（{name} 未实装）",
        )

    # -- setup ------------------------------------------------------------ #

    def setup(self):
        """握手 + 工具发现；连不上直接抛 Unreachable。"""
        params = {
            "protocolVersion": CLIENT_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcp_smoke_test", "version": "1.0"},
        }
        status, headers, parsed, kind = self.client.rpc("initialize", params)
        init_result = (parsed or {}).get("result") if isinstance(parsed, dict) else None
        init_error = (parsed or {}).get("error") if isinstance(parsed, dict) else None
        notify_status, notify_note = None, ""
        if init_result is not None:
            self.client.protocol_version = init_result.get("protocolVersion") or CLIENT_PROTOCOL_VERSION
            notify_status, n_headers, _n_parsed, _n_kind = self.client.rpc(
                "notifications/initialized", None, notify=True
            )
            # ⚠ 协议未明确：通知响应应为 202 还是 204 未定，此处只记录实际值，2xx 均视为合规
            if notify_status is None:
                notify_note = "通知未收到状态码"
            elif 200 <= notify_status < 300:
                notify_note = f"HTTP {notify_status}（2xx，符合预期；202/204 未明确，如实记录）"
            else:
                notify_note = f"HTTP {notify_status}（非 2xx，⚠ 协议未明确，仅记录不判 FAIL）"
        self.setup_info = {
            "init_status": status,
            "init_headers": safe_headers(headers),
            "init_result": init_result,
            "init_error": init_error,
            "init_kind": kind,
            "notify_status": notify_status,
            "notify_note": notify_note,
            "session_id_fp": fingerprint(self.client.session_id),
            "protocol_version": self.client.protocol_version,
        }
        if init_result is None:
            return  # 握手失败，工具发现无意义
        status, _headers, parsed, _kind = self.client.rpc("tools/list")
        tools = ((parsed or {}).get("result") or {}).get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    self.tools[tool["name"]] = tool
        self.setup_info["tools_list_status"] = status

    # -- cases ------------------------------------------------------------ #

    def case_a01(self):
        info = self.setup_info
        problems = []
        if info.get("init_error"):
            problems.append(f"initialize 返回错误 {truncate(json.dumps(info['init_error'], ensure_ascii=False))}")
        init_result = info.get("init_result") or {}
        version = init_result.get("protocolVersion")
        if not version:
            problems.append("initialize 响应缺少非空 protocolVersion")
        if not self.tools:
            problems.append("tools/list 返回的 tools 数组为空")
        for name, tool in self.tools.items():
            if not tool.get("name"):
                problems.append("存在缺 name 的工具")
            if not isinstance(tool.get("inputSchema"), dict):
                problems.append(f"工具 {name} 缺 inputSchema")
        missing = [t for t in EXPECTED_TOOLS if t not in self.tools]
        extra = [
            f"应有 14 / 实有 {len(self.tools)}",
            f"协议版本：客户端提议 {CLIENT_PROTOCOL_VERSION} → 服务端 {info.get('protocol_version') or '-'}",
            f"initialize HTTP {info.get('init_status')}（{info.get('init_kind')}）",
            f"notifications/initialized：{info.get('notify_note') or '未发送'}",
            f"Mcp-Session-Id：{info.get('session_id_fp')}（仅指纹，原值不落报告）",
            f"未实装工具（{len(missing)}）：{', '.join(missing) if missing else '无'}",
        ]
        return result(
            "A01",
            "初始化与工具发现",
            FAIL if problems else PASS,
            "；".join(problems) if problems else "握手成功，protocolVersion 非空，tools 非空且均含 name/inputSchema",
            request_summary="initialize → notifications/initialized → tools/list",
            response_excerpt=truncate(json.dumps(init_result, ensure_ascii=False)),
            extra=extra,
        )

    def case_a02(self):
        if not self.available("log_to_mysql"):
            return self.skip_if_missing("A02", "写一笔演示告警", "log_to_mysql")
        rid = self.rid("02")
        self.a02_request_id = rid
        args = fill_required(
            self.tool_schema("log_to_mysql"),
            {
                "request_id": rid,
                "event_type": "risk_warning",
                "rule_id": "demo_rule",
                "rule_version": "1.0",
                "risk_level": "yellow",
                "occurred_at": rfc3339(now_cst()),
                "payload": {
                    "M": "1000000.00",
                    "C": "800000.00",
                    "K_yellow": "1.20",
                    "K_red": "1.50",
                    "currency": "CNY",
                    "amount": "1000000.00",
                    "note": "mcp_smoke_test demo write",
                },
            },
        )
        business, is_error, rpc_error, status = self.call("log_to_mysql", args)
        auth = self.auth_result("A02", "写一笔演示告警", status)
        if auth:
            return auth
        excerpt = truncate(json.dumps(business or rpc_error, ensure_ascii=False))
        if rpc_error:
            return result(
                "A02", "写一笔演示告警", FAIL,
                f"tools/call 返回 JSON-RPC 错误：{truncate(json.dumps(rpc_error, ensure_ascii=False))}",
                f"tools/call log_to_mysql；request_id={rid}", f"HTTP {status}；{excerpt}",
            )
        if not isinstance(business, dict) or business.get("ok") is not True:
            return result(
                "A02", "写一笔演示告警", FAIL,
                f"业务对象 ok 不为 true（isError={is_error}），写入未成功",
                f"tools/call log_to_mysql；request_id={rid}", f"HTTP {status}；{excerpt}",
            )
        audit_id = business.get("data", {}).get("audit_id") if isinstance(business.get("data"), dict) else None
        audit_id = audit_id or business.get("audit_id")
        dedup = business.get("deduplicated")
        if business.get("deduplicated") is None and isinstance(business.get("data"), dict):
            dedup = business["data"].get("deduplicated")
        if not audit_id:
            return result("A02", "写一笔演示告警", FAIL, "ok=true 但未返回 audit_id",
                          f"tools/call log_to_mysql；request_id={rid}", f"HTTP {status}；{excerpt}")
        if dedup is not False:
            return result("A02", "写一笔演示告警", FAIL, f"deduplicated 应为 false，实际 {dedup!r}",
                          f"tools/call log_to_mysql；request_id={rid}", f"HTTP {status}；{excerpt}")
        self.a02_audit_id = audit_id
        self.a02_ok = True
        return result(
            "A02", "写一笔演示告警", PASS,
            "ok=true，返回 audit_id，deduplicated=false",
            f"tools/call log_to_mysql；request_id={rid}；risk_level=yellow",
            f"HTTP {status}；audit_id={audit_id}；deduplicated=false；server_time={business.get('server_time')}",
        )

    def case_a03(self):
        if not self.available("log_to_mysql"):
            return self.skip_if_missing("A03", "同键同内容重试", "log_to_mysql")
        if not self.a02_ok:
            return result("A03", "同键同内容重试", SKIP, "A02 未成功写入，幂等重试用例前提不成立，记 SKIP")
        rid = self.a02_request_id
        args = fill_required(
            self.tool_schema("log_to_mysql"),
            {
                "request_id": rid,
                "event_type": "risk_warning",
                "rule_id": "demo_rule",
                "rule_version": "1.0",
                "risk_level": "yellow",
                "occurred_at": rfc3339(now_cst()),
                "payload": {
                    "M": "1000000.00",
                    "C": "800000.00",
                    "K_yellow": "1.20",
                    "K_red": "1.50",
                    "currency": "CNY",
                    "amount": "1000000.00",
                    "note": "mcp_smoke_test demo write",
                },
            },
        )
        business, is_error, rpc_error, status = self.call("log_to_mysql", args)
        auth = self.auth_result("A03", "同键同内容重试", status)
        if auth:
            return auth
        excerpt = truncate(json.dumps(business or rpc_error, ensure_ascii=False))
        if rpc_error or not isinstance(business, dict) or business.get("ok") is not True:
            return result("A03", "同键同内容重试", FAIL,
                          f"同键重试未返回 ok=true（{truncate(str(rpc_error or business))}）",
                          f"tools/call log_to_mysql；request_id={rid}（与 A02 相同）", f"HTTP {status}；{excerpt}")
        data = business.get("data") if isinstance(business.get("data"), dict) else business
        audit_id = data.get("audit_id")
        dedup = data.get("deduplicated", business.get("deduplicated"))
        if dedup is not True:
            return result("A03", "同键同内容重试", FAIL, f"deduplicated 应为 true，实际 {dedup!r}",
                          f"tools/call log_to_mysql；request_id={rid}（与 A02 相同）", f"HTTP {status}；{excerpt}")
        if audit_id != self.a02_audit_id:
            return result("A03", "同键同内容重试", FAIL,
                          f"audit_id 与首次不一致（{self.a02_audit_id} vs {audit_id}）",
                          f"tools/call log_to_mysql；request_id={rid}（与 A02 相同）", f"HTTP {status}；{excerpt}")
        return result("A03", "同键同内容重试", PASS,
                      "ok=true，deduplicated=true，audit_id 与首次相同",
                      f"tools/call log_to_mysql；request_id={rid}（与 A02 相同）",
                      f"HTTP {status}；audit_id={audit_id}；deduplicated=true")

    def case_a04(self):
        if not self.available("log_to_mysql"):
            return self.skip_if_missing("A04", "同键不同内容", "log_to_mysql")
        if not self.a02_ok:
            return result("A04", "同键不同内容", SKIP, "A02 未成功写入，冲突用例前提不成立，记 SKIP")
        rid = self.a02_request_id
        args = fill_required(
            self.tool_schema("log_to_mysql"),
            {
                "request_id": rid,
                "event_type": "risk_warning",
                "rule_id": "demo_rule",
                "rule_version": "1.0",
                "risk_level": "red",  # 同键不同内容
                "occurred_at": rfc3339(now_cst()),
                "payload": {
                    "M": "1000000.00",
                    "C": "800000.00",
                    "K_yellow": "1.20",
                    "K_red": "1.50",
                    "currency": "CNY",
                    "amount": "1000000.00",
                    "note": "mcp_smoke_test idempotency conflict",
                },
            },
        )
        business, is_error, rpc_error, status = self.call("log_to_mysql", args)
        auth = self.auth_result("A04", "同键不同内容", status)
        if auth:
            return auth
        excerpt = truncate(json.dumps(business or rpc_error, ensure_ascii=False))
        request_summary = f"tools/call log_to_mysql；request_id={rid}（与 A02 相同）；risk_level=red"
        if rpc_error:
            return result("A04", "同键不同内容", PASS,
                          f"服务端以 JSON-RPC 错误拒绝：code={rpc_error.get('code')}",
                          request_summary, f"HTTP {status}；{excerpt}")
        if not isinstance(business, dict):
            return result("A04", "同键不同内容", FAIL, "响应缺少可解析的业务对象，无法判定冲突",
                          request_summary, f"HTTP {status}；{excerpt}")
        code = business.get("code")
        if business.get("ok") is False and code == "IDEMPOTENCY_CONFLICT":
            return result("A04", "同键不同内容", PASS, "ok=false 且 code=IDEMPOTENCY_CONFLICT，冲突被正确识别",
                          request_summary, f"HTTP {status}；{excerpt}")
        if business.get("ok") is False:
            return result("A04", "同键不同内容", PASS,
                          f"已拒绝写入（ok=false，code={code}）；⚠ 非 IDEMPOTENCY_CONFLICT 错误码，如实记录",
                          request_summary, f"HTTP {status}；{excerpt}")
        return result("A04", "同键不同内容", FAIL,
                      f"同键不同内容被接受（ok={business.get('ok')}），幂等防重未生效",
                      request_summary, f"HTTP {status}；{excerpt}")

    def case_a05(self):
        if not self.available("log_to_mysql"):
            return self.skip_if_missing("A05", "非法入参被拒", "log_to_mysql")
        long_id = (f"{self.run_tag}-A05-LONG").ljust(200, "X")[:200]
        base = fill_required(self.tool_schema("log_to_mysql"), {})
        cases = [
            ("① 超长 request_id（200 字符）", {**base, "request_id": long_id, "event_type": "risk_warning",
                                           "rule_id": "demo_rule", "rule_version": "1.0",
                                           "risk_level": "yellow", "occurred_at": rfc3339(now_cst()),
                                           "payload": {"currency": "CNY", "amount": "1000000.00"}}),
            ("② occurred_at 不带时区（2026-10-21T09:00:00）", {**base, "request_id": f"{self.run_tag}-A05-2",
                                                          "event_type": "risk_warning", "rule_id": "demo_rule",
                                                          "rule_version": "1.0", "risk_level": "yellow",
                                                          "occurred_at": "2026-10-21T09:00:00",
                                                          "payload": {"currency": "CNY", "amount": "1000000.00"}}),
            ("③ payload 金额传数字（1000000 而非 \"1000000.00\"）", {**base, "request_id": f"{self.run_tag}-A05-3",
                                                             "event_type": "risk_warning", "rule_id": "demo_rule",
                                                             "rule_version": "1.0", "risk_level": "yellow",
                                                             "occurred_at": rfc3339(now_cst()),
                                                             "payload": {"currency": "CNY", "amount": 1000000}}),
        ]
        lines, rejected_all, auth_blocked = [], True, False
        for label, args in cases:
            business, is_error, rpc_error, status = self.call("log_to_mysql", args)
            if status in (401, 403):
                auth_blocked = True
                lines.append(f"    - {label}：HTTP {status}；鉴权生效，未获得工具结果")
                continue
            excerpt = truncate(json.dumps(business or rpc_error, ensure_ascii=False))
            code = (rpc_error or {}).get("code") if rpc_error else (business or {}).get("code")
            ok = business.get("ok") if isinstance(business, dict) else None
            rejected = bool(rpc_error) or is_error or ok is False
            if not rejected:
                rejected_all = False
            lines.append(f"    - {label}：HTTP {status}；rejected={rejected}；code={code}；{excerpt}")
        if auth_blocked:
            return result("A05", "非法入参被拒", FAIL,
                          "鉴权生效：服务端返回 401/403，无法执行入参校验（认证拒绝场景下这是预期行为）",
                          "tools/call log_to_mysql × 3（超长 id / 无时区时间 / 数字金额）",
                          "  \n".join(lines) if lines else "-")
        return result(
            "A05", "非法入参被拒", PASS if rejected_all else FAIL,
            "三次非法入参全部被拒，未入库成功" if rejected_all else "存在未被拒绝的非法入参（见明细，视为入库成功）",
            "tools/call log_to_mysql × 3（超长 id / 无时区时间 / 数字金额）",
            "  \n".join(lines) if lines else "-",
        )

    def case_a06(self):
        if not self.available("log_to_mysql"):
            return self.skip_if_missing("A06", "伪造租户字段", "log_to_mysql")
        args = fill_required(
            self.tool_schema("log_to_mysql"),
            {
                "request_id": f"{self.run_tag}-A06",
                "event_type": "risk_warning",
                "rule_id": "demo_rule",
                "rule_version": "1.0",
                "risk_level": "yellow",
                "occurred_at": rfc3339(now_cst()),
                "payload": {"currency": "CNY", "amount": "1000000.00"},
                "tenant_id": "evil-corp",  # 模型伪造的租户字段
            },
        )
        business, is_error, rpc_error, status = self.call("log_to_mysql", args)
        auth = self.auth_result("A06", "伪造租户字段", status)
        if auth:
            return auth
        excerpt = truncate(json.dumps(business or rpc_error, ensure_ascii=False))
        request_summary = "tools/call log_to_mysql；额外注入 tenant_id=evil-corp"
        if rpc_error or is_error or (isinstance(business, dict) and business.get("ok") is False):
            return result("A06", "伪造租户字段", PASS,
                          f"服务端拒收含伪造租户字段的请求（code={(rpc_error or business or {}).get('code')}）",
                          request_summary, f"HTTP {status}；{excerpt}")
        if not isinstance(business, dict):
            return result("A06", "伪造租户字段", FAIL, "响应缺少可解析的业务对象，无法判定租户是否被采信",
                          request_summary, f"HTTP {status}；{excerpt}")
        if contains_token(business, "evil-corp"):
            return result("A06", "伪造租户字段", FAIL,
                          "返回数据中出现 evil-corp，模型传入的 tenant_id 被采信（越权风险）",
                          request_summary, f"HTTP {status}；{excerpt}")
        tenant_values = collect_values(business, ("tenant_id", "tenant", "tenant_code"))
        return result(
            "A06", "伪造租户字段", PASS,
            f"模型传入的 tenant_id 未被采信；服务端解析的租户值={tenant_values or '(返回数据未含租户字段)'}",
            request_summary, f"HTTP {status}；{excerpt}",
        )

    def case_a07(self):
        if not self.available("query_rule_stats"):
            return self.skip_if_missing("A07", "精确规则查询", "query_rule_stats")
        if not self.a02_ok:
            return result("A07", "精确规则查询", SKIP, "A02 未成功写入，精确查询用例前提不成立，记 SKIP")
        schema = self.tool_schema("query_rule_stats")
        start_key, warn_s = pick_key(schema, ("start", "begin", "from"), "start_time")
        end_key, warn_e = pick_key(schema, ("end", "to", "until"), "end_time")
        start, end = window(1, 1)
        warn = "；⚠ 窗口字段名协议未明确，按 inputSchema 推断" if (warn_s or warn_e) else ""
        base_args = {"rule_id": "demo_rule", "rule_version": "1.0", start_key: start, end_key: end}
        business, is_error, rpc_error, status = self.call("query_rule_stats", fill_required(schema, base_args))
        auth = self.auth_result("A07", "精确规则查询", status)
        if auth:
            return auth
        excerpt1 = truncate(json.dumps(business or rpc_error, ensure_ascii=False))
        if rpc_error or not isinstance(business, dict) or business.get("ok") is not True:
            return result("A07", "精确规则查询", FAIL,
                          f"精确查询未成功（{truncate(str(rpc_error or business))}）{warn}",
                          f"tools/call query_rule_stats；rule_id=demo_rule&rule_version=1.0；窗口[{start}, {end})",
                          f"HTTP {status}；{excerpt1}")
        rule_ids = set(collect_values(business, ("rule_id", "ruleid", "rule")))
        versions = set(collect_values(business, ("rule_version", "ruleversion", "version")))
        bad_ids = {r for r in rule_ids if r != "demo_rule"}
        bad_versions = {v for v in versions if v != "1.0"}
        if bad_ids or bad_versions:
            return result("A07", "精确规则查询", FAIL,
                          f"精确查询混入了非精确匹配记录：rule_id={sorted(bad_ids)}；rule_version={sorted(bad_versions)}{warn}",
                          f"tools/call query_rule_stats；rule_id=demo_rule&rule_version=1.0；窗口[{start}, {end})",
                          f"HTTP {status}；{excerpt1}")
        # 第二次：查询不存在的规则，不得混入 demo_rule 记录
        base_args2 = {"rule_id": "demo_rule_2", "rule_version": "1.0", start_key: start, end_key: end}
        business2, is_error2, rpc_error2, status2 = self.call("query_rule_stats", fill_required(schema, base_args2))
        auth2 = self.auth_result("A07", "精确规则查询", status2)
        if auth2:
            return auth2
        excerpt2 = truncate(json.dumps(business2 or rpc_error2, ensure_ascii=False))
        ids2 = set(collect_values(business2 or {}, ("rule_id", "ruleid", "rule")))
        found = (business2 or {}).get("found")
        data2 = (business2 or {}).get("data")
        if isinstance(data2, dict) and "found" in data2:
            found = data2.get("found")
        leaked = {r for r in ids2 if r == "demo_rule"}
        if leaked:
            return result("A07", "精确规则查询", FAIL,
                          f"查询 demo_rule_2 时混入了 demo_rule 的记录{warn}",
                          "tools/call query_rule_stats；rule_id=demo_rule_2",
                          f"HTTP {status2}；{excerpt2}")
        if found is False or not ids2:
            return result("A07", "精确规则查询", PASS,
                          f"精确查询仅含 demo_rule/1.0；查询 demo_rule_2 返回空（found={found}）{warn}",
                          f"query_rule_stats ×2（demo_rule → demo_rule_2）；窗口[{start}, {end})",
                          f"① HTTP {status}；{excerpt1}  \n② HTTP {status2}；{excerpt2}")
        return result("A07", "精确规则查询", FAIL,
                      f"查询 demo_rule_2 返回了非空结果（found={found}，rule_id={sorted(ids2)}）{warn}",
                      "tools/call query_rule_stats；rule_id=demo_rule_2",
                      f"HTTP {status2}；{excerpt2}")

    def case_a08(self):
        if not self.available("load_param_template"):
            return self.skip_if_missing("A08", "模板不存在", "load_param_template")
        schema = self.tool_schema("load_param_template")
        args = fill_required(schema, {"industry_key": "nonexistent_industry"})
        business, is_error, rpc_error, status = self.call("load_param_template", args)
        auth = self.auth_result("A08", "模板不存在", status)
        if auth:
            return auth
        excerpt = truncate(json.dumps(business or rpc_error, ensure_ascii=False))
        request_summary = "tools/call load_param_template；industry_key=nonexistent_industry"
        if rpc_error or is_error or (isinstance(business, dict) and business.get("ok") is False):
            return result("A08", "模板不存在", PASS,
                          f"未返回任何模板（以错误码表达未找到：code={(rpc_error or business or {}).get('code')}）",
                          request_summary, f"HTTP {status}；{excerpt}")
        if not isinstance(business, dict):
            return result("A08", "模板不存在", FAIL, "响应缺少可解析的业务对象", request_summary, f"HTTP {status}；{excerpt}")
        industries = set(collect_values(business, ("industry_key", "industry", "industry_id")))
        other = {i for i in industries if i != "nonexistent_industry"}
        if other:
            return result("A08", "模板不存在", FAIL,
                          f"返回了其他行业的模板：{sorted(other)}（泄漏风险）",
                          request_summary, f"HTTP {status}；{excerpt}")
        found = business.get("found", (business.get("data") or {}).get("found") if isinstance(business.get("data"), dict) else None)
        if found is False:
            return result("A08", "模板不存在", PASS, "found=false，且未返回其他行业模板",
                          request_summary, f"HTTP {status}；{excerpt}")
        if not industries and (business.get("data") in (None, {}, []) or business.get("data") is None):
            return result("A08", "模板不存在", PASS,
                          f"返回空结果（未含任何 industry_key），视为未找到；⚠ found 字段缺失，如实记录",
                          request_summary, f"HTTP {status}；{excerpt}")
        return result("A08", "模板不存在", FAIL,
                      f"found 不为 false 且返回数据非空（found={found}），可能返回了非目标模板",
                      request_summary, f"HTTP {status}；{excerpt}")

    def case_a09(self):
        if not self.available("verify_payment"):
            return self.skip_if_missing("A09", "无凭据到账验证", "verify_payment")
        schema = self.tool_schema("verify_payment")
        args = fill_required(schema, {
            "method": "bank_api",
            "evidence_ref": f"evidence://demo/bank-api-unavailable/{uuid.uuid4().hex[:12]}",
            "currency": "CNY",
            "expected_amount": "1000000.00",
        })
        business, is_error, rpc_error, status = self.call("verify_payment", args)
        auth = self.auth_result("A09", "无凭据到账验证", status)
        if auth:
            return auth
        excerpt = truncate(json.dumps(business or rpc_error, ensure_ascii=False))
        request_summary = "tools/call verify_payment；method=bank_api；证据引用为占位引用（无真实凭据）"
        if not isinstance(business, dict):
            if rpc_error:
                return result("A09", "无凭据到账验证", PASS,
                              f"服务端以 JSON-RPC 错误拒绝（code={rpc_error.get('code')}），未被记为已验证",
                              request_summary, f"HTTP {status}；{excerpt}")
            return result("A09", "无凭据到账验证", FAIL, "响应缺少可解析的业务对象", request_summary,
                          f"HTTP {status}；{excerpt}")
        blob = business.get("data") if isinstance(business.get("data"), dict) else business
        status_val = blob.get("verification_status") or business.get("verification_status")
        amount_val = blob.get("verified_amount") or business.get("verified_amount")
        if str(status_val).lower() == "verified":
            return result("A09", "无凭据到账验证", FAIL,
                          "红线：无真实凭据却被记为 verified", request_summary, f"HTTP {status}；{excerpt}")
        if str(status_val).lower() == "pending":
            if amount_val is not None and str(amount_val) != "0.00":
                return result("A09", "无凭据到账验证", FAIL,
                              f"verified_amount 应为 \"0.00\"，实际 {amount_val!r}",
                              request_summary, f"HTTP {status}；{excerpt}")
            return result("A09", "无凭据到账验证", PASS,
                          f"verification_status=pending，verified_amount={amount_val!r}（允许缺失或 0.00）",
                          request_summary, f"HTTP {status}；{excerpt}")
        if business.get("ok") is False and business.get("code"):
            return result("A09", "无凭据到账验证", PASS,
                          f"以明确错误码拒绝：code={business.get('code')}（如 BANK_ADAPTER_UNAVAILABLE 语义）",
                          request_summary, f"HTTP {status}；{excerpt}")
        return result("A09", "无凭据到账验证", FAIL,
                      f"verification_status={status_val!r}，既非 pending 也无明确错误码（⚠ 如实记录，不臆造通过）",
                      request_summary, f"HTTP {status}；{excerpt}")

    def case_a10(self):
        if not self.available("log_exception"):
            return self.skip_if_missing("A10", "模型自证已批准", "log_exception")
        schema = self.tool_schema("log_exception")
        args = fill_required(schema, {
            "request_id": f"{self.run_tag}-A10",
            "approval_ref": "APPROVAL-FAKE-0001",  # 格式合法但不可核实
        })
        business, is_error, rpc_error, status = self.call("log_exception", args)
        auth = self.auth_result("A10", "模型自证已批准", status)
        if auth:
            return auth
        excerpt = truncate(json.dumps(business or rpc_error, ensure_ascii=False))
        request_summary = "tools/call log_exception；必填字段齐全，approval_ref=APPROVAL-FAKE-0001（不可核实）"
        if rpc_error or is_error or (isinstance(business, dict) and business.get("ok") is False):
            code = (rpc_error or business or {}).get("code")
            note = "" if code == "APPROVAL_UNVERIFIED" else "（非 APPROVAL_UNVERIFIED 错误码，如实记录）"
            return result("A10", "模型自证已批准", PASS,
                          f"服务端拒绝自证批准，code={code}{note}",
                          request_summary, f"HTTP {status}；{excerpt}")
        if not isinstance(business, dict):
            return result("A10", "模型自证已批准", FAIL, "响应缺少可解析的业务对象", request_summary,
                          f"HTTP {status}；{excerpt}")
        blob = business.get("data") if isinstance(business.get("data"), dict) else business
        approval_states = collect_values(blob, ("approval_status", "approval_state", "status"))
        if any(str(s).lower() in ("verified", "approved") for s in approval_states):
            return result("A10", "模型自证已批准", FAIL,
                          f"不可核实的 approval_ref 被记为已批准（{approval_states}）",
                          request_summary, f"HTTP {status}；{excerpt}")
        if any(str(s).lower() == "pending" for s in approval_states) or business.get("ok") is True:
            note = "" if "pending" in [str(s).lower() for s in approval_states] else "；⚠ 未显式返回 pending，如实记录"
            return result("A10", "模型自证已批准", PASS,
                          f"未被记为已批准（approval 状态={approval_states or '(未返回)'}）{note}",
                          request_summary, f"HTTP {status}；{excerpt}")
        return result("A10", "模型自证已批准", FAIL,
                      f"approval 状态无法判定（{approval_states}），按红线不作为通过处理",
                      request_summary, f"HTTP {status}；{excerpt}")

    def case_a11(self):
        if not self.available("query_feedback_stats"):
            return self.skip_if_missing("A11", "空反馈统计", "query_feedback_stats")
        schema = self.tool_schema("query_feedback_stats")
        start_key, warn_s = pick_key(schema, ("start", "begin", "from"), "start_time")
        end_key, warn_e = pick_key(schema, ("end", "to", "until"), "end_time")
        start, end = window(24, 0)
        warn = "；⚠ 窗口字段名协议未明确，按 inputSchema 推断" if (warn_s or warn_e) else ""
        args = fill_required(schema, {start_key: start, end_key: end})
        business, is_error, rpc_error, status = self.call("query_feedback_stats", args)
        auth = self.auth_result("A11", "空反馈统计", status)
        if auth:
            return auth
        excerpt = truncate(json.dumps(business or rpc_error, ensure_ascii=False))
        request_summary = f"tools/call query_feedback_stats；窗口[{start}, {end})"
        if rpc_error or not isinstance(business, dict) or business.get("ok") is not True:
            return result("A11", "空反馈统计", FAIL,
                          f"查询未成功（{truncate(str(rpc_error or business))}）{warn}",
                          request_summary, f"HTTP {status}；{excerpt}")
        acc_hits = []
        for key, value in walk(business):
            lowered = str(key).lower()
            if any(t in lowered for t in ("accuracy", "acc_rate", "precision", "correct_rate")) or "准确率" in str(key):
                acc_hits.append(f"{key}={value}")
            elif "准确率" in str(value):
                acc_hits.append(str(value))
        if acc_hits:
            return result("A11", "空反馈统计", FAIL,
                          f"出现「准确率」类字段：{truncate('; '.join(acc_hits))}{warn}",
                          request_summary, f"HTTP {status}；{excerpt}")
        counts = collect_values(business, ("count", "total", "total_count", "feedback_count", "n"))
        return result("A11", "空反馈统计", PASS,
                      f"未出现任何「准确率」字段；计数字段={counts or '(空统计/零计数)'}{warn}",
                      request_summary, f"HTTP {status}；{excerpt}")

    def case_a12(self):
        status, headers, raw, exc = self.client.get_probe()
        if exc is not None:
            return result("A12", "通知语义探测", FAIL,
                          f"GET 探测失败：{truncate(str(exc))}（连接失败才判 FAIL）",
                          f"GET {self.client.base_url}", "-")
        note = "200/405 均为 INFO，不判 FAIL（⚠ 协议未明确 GET 语义）"
        return result(
            "A12", "通知语义探测", INFO,
            f"GET 探测返回 HTTP {status}；{note}",
            f"GET {self.client.base_url}（仅一次探测，不维持长连接）",
            f"HTTP {status}；Content-Type={(headers or {}).get('Content-Type', '-')}；响应体={truncate(raw)}",
        )

    # -- run -------------------------------------------------------------- #

    def run_all(self):
        emit(f"[setup] 目标：{self.client.base_url}（来自参数/环境变量，未硬编码）")
        self.setup()
        plan = [
            ("A01", "初始化与工具发现", self.case_a01),
            ("A02", "写一笔演示告警", self.case_a02),
            ("A03", "同键同内容重试", self.case_a03),
            ("A04", "同键不同内容", self.case_a04),
            ("A05", "非法入参被拒", self.case_a05),
            ("A06", "伪造租户字段", self.case_a06),
            ("A07", "精确规则查询", self.case_a07),
            ("A08", "模板不存在", self.case_a08),
            ("A09", "无凭据到账验证", self.case_a09),
            ("A10", "模型自证已批准", self.case_a10),
            ("A11", "空反馈统计", self.case_a11),
            ("A12", "通知语义探测", self.case_a12),
        ]
        for case_id, title, fn in plan:  # 串行执行，一个失败不中断整体
            try:
                self.results.append(fn())
            except Unreachable:
                raise
            except NetworkExhausted as exc:
                self.results.append(result(
                    case_id, title, FAIL,
                    f"未知网络错误重试 {MAX_ATTEMPTS} 次仍失败，判 FAIL 并跳过依赖该步的用例：{truncate(str(exc))}",
                ))
        return self.results


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

MANUAL_CHECKS = (
    ("M01", "A02 写入后，MySQL 中该 request_id 应只有 1 条审计记录",
     "SELECT COUNT(*) FROM audit_log WHERE request_id='{rid}';  -- 期望 1"),
    ("M02", "A03 同键重试后，计数仍应为 1（幂等未产生新行）",
     "SELECT COUNT(*) FROM audit_log WHERE request_id='{rid}';  -- 期望仍为 1"),
    ("M03", "数据库停机场景：审计降级/失败路径", "需服务端配合注入故障；未提供故障开关时标注「不具备条件」"),
    ("M04", "通知失败但审计成功的两状态分离展示", "需真实通知组件；未提供时标注「不具备条件」"),
)


def render_report(results, base_url, run_tag, manual, token_present=False):
    lines = [
        "# MCP 验收测试报告 · CashflowRiskWorkbench",
        "",
        f"- 目标入口：`{base_url}`（来源：命令行参数或环境变量 MCP_BASE_URL）",
        f"- run-tag：`{run_tag}`（request_id 前缀，用于复现）",
        f"- 生成时间：{rfc3339(now_cst())}（本地 +08:00；服务端 server_time 为 UTC）",
        f"- 凭据：{'已提供（令牌值不落报告，仅此标记）' if token_present else '未提供'}",
        "",
        "---",
        "",
    ]
    for item in results:
        lines.append(f"## {item['id']} {item['title']} — {item['status']}")
        lines.append("")
        lines.append(f"- **请求摘要（脱敏后）**：{item['request'] or '-'}")
        lines.append(f"- **响应关键片段**：{item['response'] or '-'}")
        for extra in item["extra"]:
            lines.append(f"- 补充：{extra}")
        lines.append(f"- **判定**：{item['status']}")
        lines.append(f"- **理由**：{item['reason']}")
        lines.append("")
    counts = {k: sum(1 for r in results if r["status"] == k) for k in (PASS, FAIL, SKIP, INFO)}
    exit_code = 1 if counts[FAIL] else 0
    lines += [
        "---",
        "",
        "## 汇总",
        "",
        "| 用例 | 标题 | 判定 |",
        "| --- | --- | --- |",
    ]
    for item in results:
        lines.append(f"| {item['id']} | {item['title']} | {item['status']} |")
    lines += [
        "",
        f"PASS={counts[PASS]}  FAIL={counts[FAIL]}  SKIP={counts[SKIP]}  INFO={counts[INFO]}",
        "",
        f"建议退出码：**{exit_code}**（有 FAIL → 1；全 PASS/SKIP → 0；连不上服务器 → 2）",
        "",
        "---",
        "",
        "## 人工核对清单（脚本不判分，需人工执行/确认）",
        "",
    ]
    for cid, desc, howto in manual:
        lines.append(f"- **{cid}** {desc}")
        lines.append(f"    - {howto}")
    lines.append("")
    return "\n".join(lines)


def build_manual(run_tag, a02_request_id):
    rid = a02_request_id or f"{run_tag}-A02"
    return [(cid, desc, howto.replace("{rid}", rid)) for cid, desc, howto in MANUAL_CHECKS]


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="mcp_smoke_test.py",
        description="CashflowRiskWorkbench MCP Server 验收测试客户端（零第三方依赖）",
    )
    parser.add_argument("--base-url", default=os.environ.get("MCP_BASE_URL", ""),
                        help="MCP 入口地址，默认取环境变量 MCP_BASE_URL")
    parser.add_argument("--token", default=os.environ.get("MCP_TOKEN", ""),
                        help="Bearer 令牌，默认取环境变量 MCP_TOKEN（不回显到报告）")
    parser.add_argument("--run-tag", default=now_cst().strftime("%Y%m%d-%H%M%S"),
                        help="request_id 前缀，默认 YYYYMMDD-HHMMSS")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次 HTTP 超时秒数，默认 15")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 老版本/非交互环境忽略
        pass
    args = parse_args(argv)
    if not args.base_url:
        emit("[error] 未指定 --base-url 且环境变量 MCP_BASE_URL 为空（禁止硬编码地址，故不提供默认值）")
        print("# MCP 验收测试报告\n\n未执行：缺少目标地址（--base-url 或 MCP_BASE_URL）。\n")
        return 2
    runner = Runner(args.base_url.rstrip("/"), args.token, args.timeout, args.run_tag)
    try:
        runner.run_all()
    except Unreachable as exc:
        emit(f"[fatal] 连不上服务器：{exc}")
        print("# MCP 验收测试报告\n")
        print(f"- 目标入口：`{args.base_url}`")
        print(f"- 结论：**连不上服务器**（{truncate(str(exc))}），未执行任何业务判定。")
        print("\n建议退出码：**2**")
        return 2
    except NetworkExhausted as exc:
        emit(f"[fatal] 网络重试耗尽：{exc}")
        print("# MCP 验收测试报告\n")
        print(f"- 目标入口：`{args.base_url}`")
        print(f"- 结论：**网络重试耗尽**（{truncate(str(exc))}），握手未完成。")
        print("\n建议退出码：**2**")
        return 2
    manual = build_manual(args.run_tag, runner.a02_request_id)
    print(render_report(runner.results, args.base_url, args.run_tag, manual, bool(args.token)))
    failed = any(r["status"] == FAIL for r in runner.results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

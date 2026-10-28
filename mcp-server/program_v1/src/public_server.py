"""Public-facing HTTP service: serves the page and the API from one origin.

This is NOT a rewrite of anything. Every number on the page is produced by
program_workbench / program_service; this file only exposes them over HTTP.

Kept from the loopback QA server: bounded body, CSRF token, CSP, Origin check,
and the rule that a failed database write never reports success.
Changed: listen address, allowed hosts and token are configurable, so the same
binary can run behind Nginx on the server instead of only on loopback.
"""
from __future__ import annotations

import json
import os
import platform
import secrets
import socket
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import urllib.error
import urllib.request

import pymysql
from pymysql.cursors import DictCursor

from config import Config
from program_service import ProgramService
from program_workbench import project_status, list_sources, calculate, readiness, data_audit
from service import strict_result
from notify import push, channels

PORT = int(os.environ.get("WORKBENCH_PORT", "18767"))
HOST = os.environ.get("WORKBENCH_HOST", "127.0.0.1")
# Derived from this file's location so the same code works on Windows and Linux.
# src -> program_v1 -> mcp-server -> project root
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
WEB = Path(os.environ.get("WORKBENCH_WEB") or (_ROOT / "web" / "工作台_程序计算版.html"))
CONSOLE = Path(os.environ.get("WORKBENCH_CONSOLE") or (_ROOT / "web" / "后端控制台.html"))
# Unified single-page entry that merges the cashflow, risk and console pages.
# It must be served from here (not as a static nginx file) so that __CSRF_TOKEN__
# is substituted; without a real token its API calls are refused.
UNIFIED = Path(os.environ.get("WORKBENCH_UNIFIED") or (_ROOT / "web" / "统一工作台.html"))
TOKEN = (os.environ.get("WORKBENCH_TOKEN") or "").strip() or secrets.token_urlsafe(32)
START = time.time()


def _page(file_path):
    """读取页面并把令牌占位符换成真令牌。

    只替换 meta 标签内的那一处，绝不能用裸的 replace("__CSRF_TOKEN__", TOKEN)：
    统一工作台里还有第二个同名字符串，是 JS 判断「页面是否由本服务提供」的哨兵
    —— var served = token !== '__CSRF_TOKEN__';
    无差别替换会把哨兵一起换成真令牌，于是 token 恒等于哨兵值，served 恒为 false，
    所有同源请求在发出之前就被短路：主站接口一次都不会出现在访问日志里，
    而被短路逻辑刻意放过的 /v8/ 请求却照常到达。外部症状就是徽标永远显示
    「— / V8」、总览「端口 undefined/undefined、数据库 未连接」。
    """
    return file_path.read_text(encoding="utf-8").replace(
        'content="__CSRF_TOKEN__"', 'content="{}"'.format(TOKEN))

_HOSTS = (os.environ.get("WORKBENCH_ALLOWED_HOSTS") or "").strip()
ALLOWED_HOSTS = {h.strip() for h in _HOSTS.split(",") if h.strip()} or {
    "127.0.0.1:{}".format(PORT),
    "localhost:{}".format(PORT),
}

MAX_BODY = 512_000

# ---------- V8 反向代理（本机联通的关键）----------
# 生产环境由 Nginx 把 /v8/ 直接反代到 8010，请求根本不进本进程。
# 本机没有 Nginx：页面里的 V8 请求是同源的 /v8/api/v8/*，会直接打到本进程，
# 而本进程原本没有这些路由，只能回 404。这里把它原样转发给 V8 服务。
# 这样页面在任何环境都只访问「一个源」：既不必在后端开 CORS，
# 也不会被本进程自己下发的 CSP（connect-src 'self'）拦掉。
V8_ORIGIN = (os.environ.get("WORKBENCH_V8_ORIGIN") or "").strip().rstrip("/")
if not V8_ORIGIN:
    V8_ORIGIN = "http://127.0.0.1:{}".format(os.environ.get("CF_V8_PORT") or "8010")
# 一次 v8 评估含随机森林与压力测试，实测 0.3~0.6 秒，留足余量。
V8_TIMEOUT = float(os.environ.get("WORKBENCH_V8_TIMEOUT") or "120")
# 上游地址默认回环，只允许指向回环或本机，避免被当成任意内网代理。
_V8_HOST = urlparse(V8_ORIGIN).hostname or ""
V8_ENABLED = _V8_HOST in {"127.0.0.1", "localhost", "::1"}

# ---------- read-only backend console ----------
# Everything below only observes. It never calculates, writes or pushes; the
# point is to let an operator see what is actually running, not what is claimed.

SERVICES = (
    ("cf-workbench", "cf-workbench", "页面 + HTTP API（本进程）"),
    # V8 是独立进程（默认 8010）。本机没有 Nginx 时由本进程代理，
    # 生产由 Nginx 直接反代；两种情况下控制台都应该如实显示它在不在听。
    ("cf-v8-web", "v8", "V8 三层风控 HTTP 服务（本进程代理目标）"),
    ("cf-mcp-http", "mcp", "MCP over HTTP，供千问调用"),
    ("nginx", "nginx", "TLS 入口，反代到上面两个"),
    ("mysql", "mysql", "MySQL，只绑回环"),
)

ENDPOINTS = (
    ("GET", "/", "工作台页面", True),
    ("GET", "/console", "本页（后端控制台）", True),
    ("GET", "/api/console", "后端聚合状态，本页数据源", True),
    ("GET", "/api/status", "能力与架构清单", True),
    ("GET", "/api/sources", "已登记数据来源与可用性", True),
    ("GET", "/api/calculate?source_id=…", "现场读取输入并计算，不写库", True),
    ("GET", "/api/readiness?source_id=…", "输入就绪检查", True),
    ("GET", "/api/audit?source_id=…", "原始八表只读检查", True),
    ("GET", "/api/notify-channels", "推送通道是否配置", True),
    ("POST", "/api/save", "写入隔离库（需会话令牌）", False),
    ("POST", "/api/query", "按请求编号回查（需会话令牌）", False),
    ("POST", "/api/notify", "推送钉钉/企业微信（需会话令牌）", False),
    ("POST", "/mcp", "MCP JSON-RPC 端点，走 8123", False),
    # V8 端点走 /v8 前缀：本机由本进程代理到 8010，生产由 Nginx 反代。
    ("GET", "/v8/api/v8/industries", "V8 四行业参数模板", True),
    ("GET", "/v8/api/v8/scenarios", "V8 受评场景", True),
    ("GET", "/v8/api/v8/logs", "V8 最近留痕（需数据库）", True),
    ("GET", "/v8/api/v8/assess?industry=制造业&scenario=edge", "V8 完整评估（较慢）", True),
    ("POST", "/v8/api/v8/save", "V8 评估留痕（需会话令牌）", False),
)

MCP_TOOLS = (
    ("get_workbench_status", "架构、来源画像、数据库连通性"),
    ("list_manufacturing_sources", "列出固定的原始样例与合成输入"),
    ("inspect_manufacturing_source", "只读查看某个来源，不改原表"),
    ("get_manufacturing_readiness", "输入就绪检查，问题仍为阻塞项"),
    ("get_manufacturing_data_audit", "原始八表或合成八表的只读审计"),
    ("calculate_manufacturing_cashflow", "用 Decimal 现场计算，标注 SYNTHETIC，不写库"),
    ("read_cashflow_result", "重算并返回来源与算法摘要"),
    ("save_cashflow_result", "只允许精确重算结果入隔离库"),
    ("query_cashflow_result", "按请求编号精确回查"),
)


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.35):
            return True
    except OSError:
        return False


def _service_ports() -> dict:
    mcp_port = os.environ.get("MCP_HTTP_PORT") or "8123"
    v8_port = os.environ.get("CF_V8_PORT") or "8010"
    return {
        "cf-workbench": PORT,
        "v8": int(v8_port) if str(v8_port).isdigit() else 8010,
        "mcp": int(mcp_port) if str(mcp_port).isdigit() else 8123,
        "nginx": 443,
        "mysql": 3306,
    }


def _db_report() -> dict:
    try:
        cfg = Config.from_env()
    except Exception as exc:
        return {"connected": False, "detail": "配置不完整（{}）".format(type(exc).__name__)}
    try:
        conn = pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=4,
            read_timeout=6,
            write_timeout=6,
            init_command="SET time_zone='+00:00'",
        )
    except Exception as exc:
        return {"connected": False, "detail": "连接失败（{}）".format(type(exc).__name__)}
    base = {
        "connected": True,
        "engine": "MySQL",
        "name": cfg.database,
        "table": "mfg_cashflow_runs_v1",
        "host": cfg.host,
        "port": cfg.port,
        "user": cfg.user,
        "rows": None,
        "recent": [],
    }
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM mfg_cashflow_runs_v1")
            base["rows"] = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT record_id,request_id,period,operation,stored_at,"
                "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.amounts.closing')),"
                "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.result.amounts.closing'))) AS closing "
                "FROM mfg_cashflow_runs_v1 ORDER BY stored_at DESC LIMIT 5"
            )
            for row in cur.fetchall():
                base["recent"].append(
                    {
                        "record_id": row["record_id"],
                        "request_id": row["request_id"],
                        "period": row["period"],
                        "operation": row["operation"],
                        "closing": row["closing"],
                        "stored_at": row["stored_at"].strftime("%Y-%m-%d %H:%M:%S") if row["stored_at"] else "",
                    }
                )
    except Exception as exc:
        base["detail"] = "查询失败（{}）".format(type(exc).__name__)
    finally:
        conn.close()
    return base


def console_report() -> dict:
    now = datetime.now().astimezone()
    started = datetime.fromtimestamp(START).astimezone()
    ports = _service_ports()
    services = [
        {"name": name, "port": ports[alias], "listening": _listening(ports[alias]), "note": note}
        for name, alias, note in SERVICES
    ]
    public = [
        h.split(":")[0]
        for h in sorted(ALLOWED_HOSTS)
        if not h.startswith(("127.0.0.1", "localhost")) and h[0].isalpha()
    ]
    mcp_host = public[0] if public else "127.0.0.1"
    return {
        "schema_version": "workbench-console.v1",
        "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "runtime": {
            "app": "cf-workbench",
            "pid": os.getpid(),
            "bind": "{}:{}".format(HOST, PORT),
            "python": platform.python_version(),
            "uptime_seconds": int(time.time() - START),
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "services": services,
        "database": _db_report(),
        "endpoints": [
            {"method": m, "path": p, "note": n, "probe": b} for m, p, n, b in ENDPOINTS
        ],
        "mcp": {
            "url": "https://{}/mcp".format(mcp_host),
            "transport": "streamable-http（JSON-RPC 2.0）",
            "auth": "Host 白名单 + DNS 重绑定防护",
            "tools": [{"name": n, "note": d} for n, d in MCP_TOOLS],
        },
        "notify": dict(channels(), policy="失败即报错，不假装成功"),
    }


def _is_v8(path: str) -> bool:
    """页面固定用 /v8 前缀访问 V8；也接受 /api/v8 直连写法。"""
    return path.startswith("/v8/") or path.startswith("/api/v8/")


def _v8_upstream(path: str) -> str:
    """/v8/api/v8/x → /api/v8/x。上游不认识 /v8 这个前缀。"""
    return path[len("/v8"):] if path.startswith("/v8/") else path


class Handler(BaseHTTPRequestHandler):
    server_version = "workbench"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def respond(self, data, code=200, typ="application/json; charset=utf-8"):
        raw = (
            data.encode("utf-8")
            if isinstance(data, str)
            else json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        self.send_response(code)
        headers = {
            "Content-Type": typ,
            "Content-Length": str(len(raw)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'none'",
        }
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def proxy_v8(self, method, body=None):
        """把 V8 请求原样转发给 V8 服务，并把上游响应原样带回。

        V8 有独立的返回格式与错误处理，这里不改写任何一个字节：
        上游 5xx 就照样返回 5xx，让页面看到真实的失败原因；
        上游连不上就报 502，绝不用快照或缓存冒充成功。
        """
        if not V8_ENABLED:
            return self.respond({"error": "v8_origin_not_allowed",
                                 "detail": "V8 上游只允许回环地址。"}, 500)
        parsed = urlparse(self.path)
        upstream = V8_ORIGIN + _v8_upstream(parsed.path)
        if parsed.query:
            upstream += "?" + parsed.query
        req = urllib.request.Request(upstream, data=body, method=method)
        # 只透传与本次调用有关的头：不外泄本进程的其他凭据
        for name in ("Content-Type", "Accept", "X-Workbench-Token"):
            value = self.headers.get(name)
            if value:
                req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=V8_TIMEOUT) as resp:
                raw, code = resp.read(), resp.status
                ctype = resp.headers.get("Content-Type") or "application/json; charset=utf-8"
        except urllib.error.HTTPError as exc:
            # 上游 4xx/5xx 也带着自己的 JSON 错误体，如实透传
            raw, code = exc.read(), exc.code
            ctype = exc.headers.get("Content-Type") or "application/json; charset=utf-8"
        except Exception as exc:
            return self.respond(
                {"error": "v8_unavailable",
                 "detail": "V8 服务不可达（{}）：{}".format(V8_ORIGIN, type(exc).__name__)}, 502)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def trusted(self) -> bool:
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in ALLOWED_HOSTS:
            return False
        if origin is None:
            return True
        scheme = "https" if host.split(":")[0] not in {"127.0.0.1", "localhost"} else "http"
        return origin in {"{}://{}".format(scheme, h) for h in ALLOWED_HOSTS}

    def do_GET(self):
        if not self.trusted():
            return self.respond({"error": "host_not_allowed"}, 403)
        path = urlparse(self.path)
        params = parse_qs(path.query)
        try:
            # V8 路由必须排在最前：它是被代理出去的，不落在本进程的任何路由上
            if _is_v8(path.path):
                return self.proxy_v8("GET")
            if path.path in ("/", "/index.html"):
                return self.respond(
                    _page(WEB),
                    typ="text/html; charset=utf-8",
                )
            if path.path in ("/console", "/console/"):
                return self.respond(
                    _page(CONSOLE),
                    typ="text/html; charset=utf-8",
                )
            if path.path in ("/unified", "/unified/"):
                return self.respond(
                    _page(UNIFIED),
                    typ="text/html; charset=utf-8",
                )
            if path.path == "/api/console":
                return self.respond(console_report())
            if path.path == "/api/status":
                try:
                    return self.respond(project_status())
                except ValueError as exc:
                    # Storage is a real gap, not something to hide. Degrade loudly
                    # so the page still loads and says "database unavailable"
                    # instead of claiming a connection that does not exist.
                    return self.respond(
                        {
                            "status": "degraded",
                            "detail": str(exc),
                            "components": {"database": "unavailable"},
                            "note": "数据库未配置或不可用；计算仍可运行，保存与回查不可用。",
                        }
                    )
            if path.path == "/api/sources":
                return self.respond(list_sources())
            if path.path == "/api/calculate":
                return self.respond(calculate(params.get("source_id", ["program_demo_v1"])[0]))
            if path.path == "/api/readiness":
                return self.respond(readiness(params.get("source_id", ["manufacturing_sample_v1"])[0]))
            if path.path == "/api/audit":
                return self.respond(data_audit(params.get("source_id", ["manufacturing_sample_v1"])[0]))
            if path.path == "/api/notify-channels":
                return self.respond({"channels": channels()})
            return self.respond({"error": "not_found"}, 404)
        except (ValueError, OSError) as exc:
            return self.respond({"error": type(exc).__name__, "detail": str(exc)}, 422)
        except Exception:
            return self.respond(
                {"error": "operation_failed", "detail": "操作失败；未确认数据库提交时请使用同一请求编号回查。"}, 503
            )

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > MAX_BODY:
                return self.respond({"error": "request_size_invalid"}, 413)
            raw = self.rfile.read(size)
            if len(raw) != size:
                return self.respond({"error": "incomplete_request_body"}, 400)
        except (ValueError, TimeoutError):
            return self.respond({"error": "request_body_invalid"}, 400)

        if (
            not self.trusted()
            or not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), TOKEN)
        ):
            return self.respond({"error": "session_required"}, 403)

        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.respond({"error": "json_required"}, 415)
            route = urlparse(self.path).path
            # V8 的写请求同样转发；它已通过上面的会话令牌校验，
            # 上游拿到的是页面带来的原始令牌与请求体。
            if _is_v8(route):
                return self.proxy_v8("POST", raw)

            data = strict_result(raw)
            if route == "/api/notify":
                # Push only what the engine returns right now. The caller supplies a
                # source id, never the amounts, so a client cannot fabricate the text.
                # No database needed here: notification must survive a storage outage.
                if not isinstance(data, dict) or set(data) != {"source_id"}:
                    raise ValueError("notify_fields_invalid")
                source_id = data["source_id"]
                if not isinstance(source_id, str) or not source_id:
                    raise ValueError("source_id_invalid")
                payload = dict(calculate(source_id))
                payload["source_id"] = source_id
                return self.respond({"pushed": True, "detail": push(payload)})

            service = ProgramService(Config.from_env())

            if route == "/api/save":
                if not isinstance(data, dict) or set(data) != {"result", "request_id"}:
                    raise ValueError("save_fields_invalid")
                return self.respond(service.save(data["result"], data["request_id"]))

            if route == "/api/query":
                if not isinstance(data, dict) or set(data) != {"request_id", "period", "source_revision", "run_id"}:
                    raise ValueError("query_fields_invalid")
                return self.respond(service.query(**data))

            return self.respond({"error": "not_found"}, 404)
        except (ValueError, TypeError, OSError) as exc:
            return self.respond({"error": type(exc).__name__, "detail": str(exc)}, 422)
        except Exception:
            return self.respond(
                {"error": "operation_failed", "detail": "操作失败；请使用同一请求编号回查，不要更换编号重复保存。"}, 503
            )


def main():
    # 线程化：转发 V8 评估时（含随机森林，约 0.5 秒）不能把整个页面堵住
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Workbench public server: http://{}:{}".format(HOST, PORT), flush=True)
    print("allowed hosts: {}".format(", ".join(sorted(ALLOWED_HOSTS))), flush=True)
    print("v8 upstream: {} (enabled={})".format(V8_ORIGIN, V8_ENABLED), flush=True)
    print("notify channels: {}".format(json.dumps(channels(), ensure_ascii=False)), flush=True)
    if not os.environ.get("WORKBENCH_TOKEN"):
        print("session token (shown once, not persisted): {}".format(TOKEN), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

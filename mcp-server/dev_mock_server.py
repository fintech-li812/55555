#!/usr/bin/env python3
"""dev_mock_server.py — 发包方自备的 MCP 靶机（不属外包交付物）

用途：仅用于验收 mcp_smoke_test.py 的判定逻辑是否正确，不接真实 MySQL。
两种模式（环境变量 STRICT_PROPS）：
  1（默认）严格：按工具 JSON Schema 校验 required + additionalProperties:false
  0 宽松  ：忽略未声明字段，只校验业务规则
端口由 --port 指定。状态仅存内存，进程退出即消失。
"""
import argparse, hashlib, json, os, re, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STRICT = os.environ.get("STRICT_PROPS", "1") != "0"
TENANT = "demo01"
LOCK = threading.Lock()
LEDGER = {}   # (tenant, tool, request_id) -> (payload_hash, result_obj)
AUDIT = []    # 已写入的审计行

TOOLS = {
    "log_to_mysql": {
        "required": ["request_id", "source_ref", "occurred_at", "event_type", "payload"],
        "props": {
            "request_id": {"type": "string"}, "source_ref": {"type": "string"},
            "occurred_at": {"type": "string"},
            "event_type": {"type": "string", "enum": ["risk_warning","tracking_changed","parameter_changed","task_result"]},
            "rule_id": {"type": "string"}, "rule_version": {"type": "string"},
            "risk_level": {"type": "string", "enum": ["green","yellow","red"]},
            "payload": {"type": "object"},
        },
    },
    "query_rule_stats": {"required": ["start_at","end_at"], "props": {
        "start_at":{"type":"string"},"end_at":{"type":"string"},"rule_id":{"type":"string"},
        "rule_version":{"type":"string"},"risk_level":{"type":"string"},"limit":{"type":"integer"},"cursor":{"type":"string"}}},
    "query_audit_by_event": {"required": ["start_at","end_at","event_type"], "props": {
        "start_at":{"type":"string"},"end_at":{"type":"string"},"event_type":{"type":"string"},"limit":{"type":"integer"}}},
    "log_feedback": {"required": ["request_id","source_ref","occurred_at","metric_code","actual_source_ref",
        "evaluation_version","predicted_value","actual_value","currency","accuracy_label",
        "forecast_start_at","forecast_end_at"], "props": {
        "request_id":{"type":"string"},"source_ref":{"type":"string"},"occurred_at":{"type":"string"},
        "audit_id":{"type":"integer"},"forecast_id":{"type":"string"},"metric_code":{"type":"string"},
        "predicted_value":{"type":"string"},"actual_value":{"type":"string"},"currency":{"type":"string","enum":["CNY"]},
        "accuracy_label":{"type":"string","enum":["accurate","inaccurate","pending"]},
        "evaluation_version":{"type":"string"},"actual_source_ref":{"type":"string"},
        "forecast_start_at":{"type":"string"},"forecast_end_at":{"type":"string"}}},
    "query_feedback_stats": {"required": ["start_at","end_at"], "props": {
        "start_at":{"type":"string"},"end_at":{"type":"string"},"metric_code":{"type":"string"},
        "evaluation_version":{"type":"string"},"limit":{"type":"integer"},"cursor":{"type":"string"}}},
    "save_param_template": {"required": ["request_id","source_ref","occurred_at","industry_key","template_version","params"],
        "props": {"request_id":{"type":"string"},"source_ref":{"type":"string"},"occurred_at":{"type":"string"},
        "industry_key":{"type":"string"},"template_version":{"type":"string"},"params":{"type":"object"}}},
    "load_param_template": {"required": ["industry_key"], "props": {
        "industry_key":{"type":"string"},"template_version":{"type":"string"}}},
    "log_exception": {"required": ["request_id","source_ref","occurred_at","dept","amount","currency","reason","approval_ref"],
        "props": {"request_id":{"type":"string"},"source_ref":{"type":"string"},"occurred_at":{"type":"string"},
        "dept":{"type":"string"},"amount":{"type":"string"},"currency":{"type":"string","enum":["CNY"]},
        "reason":{"type":"string"},"approval_ref":{"type":"string"},"approver":{"type":"string"}}},
    "query_exception_stats": {"required": ["start_at","end_at"], "props": {
        "start_at":{"type":"string"},"end_at":{"type":"string"},"dept":{"type":"string"},"limit":{"type":"integer"}}},
    "log_stress_test": {"required": ["request_id","source_ref","occurred_at","run_id","scenario","scenario_version",
        "input_snapshot","result_snapshot","risk_level"], "props": {
        "request_id":{"type":"string"},"source_ref":{"type":"string"},"occurred_at":{"type":"string"},
        "run_id":{"type":"string"},"scenario":{"type":"string"},"scenario_version":{"type":"string"},
        "input_snapshot":{"type":"object"},"result_snapshot":{"type":"object"},"risk_level":{"type":"string"}}},
    "verify_payment": {"required": ["request_id","source_ref","occurred_at","payment_id","method","evidence_ref"],
        "props": {"request_id":{"type":"string"},"source_ref":{"type":"string"},"occurred_at":{"type":"string"},
        "payment_id":{"type":"string"},"method":{"type":"string","enum":["bank_api","manual_review"]},
        "evidence_ref":{"type":"string"}}},
    "rf_predict": {"required": ["request_id","source_ref","occurred_at","features"], "props": {
        "request_id":{"type":"string"},"source_ref":{"type":"string"},"occurred_at":{"type":"string"},
        "features":{"type":"object"}}},
    "load_da_coefficients": {"required": ["industry_key"], "props": {
        "industry_key":{"type":"string"},"coefficient_version":{"type":"string"}}},
    "save_da_coefficients": {"required": ["request_id","source_ref","occurred_at","industry_key","coefficient_version","coefficients"],
        "props": {"request_id":{"type":"string"},"source_ref":{"type":"string"},"occurred_at":{"type":"string"},
        "industry_key":{"type":"string"},"coefficient_version":{"type":"string"},"coefficients":{"type":"object"}}},
}

TZ_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")
MONEY_RE = re.compile(r"^-?[0-9]{1,18}(\.[0-9]{1,2})?$")


def envelope(ok, code, msg, rid, data=None):
    return {"ok": ok, "code": code, "message": msg, "request_id": rid,
            "server_time": "2026-10-21T15:00:00Z", "data": data or {}}


def validate(name, args):
    spec = TOOLS[name]
    for k in spec["required"]:
        if k not in args:
            return f"缺少必填字段 {k}"
    rid = args.get("request_id")
    if isinstance(rid, str) and (len(rid) < 1 or len(rid) > 128):
        return f"request_id 长度超限（{len(rid)}，允许 1~128）"
    for k, v in args.items():
        if k.endswith("_at") and isinstance(v, str) and not TZ_RE.search(v):
            return f"{k} 必须为带时区的 RFC3339"
    if STRICT:
        for k in args:
            if k not in spec["props"]:
                return f"未声明字段 {k}（additionalProperties=false）"
    for k, v in list(args.items()):
        if k == "amount" and isinstance(v, str) and not MONEY_RE.match(v):
            return "金额格式非法"
    payload = args.get("payload")
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k in ("M","C","amount","predicted_value","actual_value","verified_amount") and not isinstance(v, str):
                return f"payload.{k} 金额必须为十进制字符串，收到 {type(v).__name__}"
            if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                return f"payload.{k} 含 NaN/Infinity"
    return None


def dispatch(name, args):
    rid = args.get("request_id") or "-"
    if name == "log_to_mysql":
        err = validate(name, args)
        if err:
            return envelope(False, "INVALID_PARAMS", err, rid), True
        if args.get("event_type") == "risk_warning" and not all(
                args.get(k) for k in ("rule_id", "rule_version", "risk_level")):
            return envelope(False, "INVALID_PARAMS", "risk_warning 必须带 rule_id/rule_version/risk_level", rid), True
        row = {"tenant": TENANT, "request_id": rid, "rule_id": args.get("rule_id"),
               "rule_version": args.get("rule_version"), "risk_level": args.get("risk_level"),
               "event_type": args.get("event_type")}
        audit_id = f"A-{hashlib.sha1(json.dumps(row, sort_keys=True).encode()).hexdigest()[:8]}"
        return envelope(True, "OK", "审计事件已存储", rid, {"audit_id": audit_id, "deduplicated": False}), False

    if name == "query_rule_stats":
        rows = [r for r in AUDIT if (not args.get("rule_id") or r["rule_id"] == args["rule_id"])
                and (not args.get("rule_version") or r["rule_version"] == args["rule_version"])]
        if not rows:
            return envelope(True, "OK", "无匹配记录", rid, {"found": False, "counts": []}), False
        return envelope(True, "OK", "查询成功", rid, {"found": True, "counts": [
            {"rule_id": rows[0]["rule_id"], "rule_version": rows[0]["rule_version"],
             "risk_level": r["risk_level"], "count": 1} for r in rows]}), False

    if name == "query_feedback_stats":
        return envelope(True, "OK", "无反馈样本", rid,
                        {"found": False, "label_counts": {"accurate": 0, "inaccurate": 0, "pending": 0}}), False

    if name == "load_param_template":
        return envelope(True, "OK", "未找到模板", rid, {"found": False, "matched_version": None}), False

    if name == "verify_payment":
        return envelope(True, "OK", "无银行或人工复核凭据，保持待验证", rid, {
            "verification_status": "pending", "verified_amount": "0.00", "currency": "CNY",
            "evidence_ref": args.get("evidence_ref"), "reason": "BANK_ADAPTER_UNAVAILABLE"}), False

    if name == "log_exception":
        return envelope(False, "APPROVAL_UNVERIFIED", "审批凭据无法核实，不记为已批准例外", rid), True

    return envelope(True, "OK", "stub", rid, {"tool": name}), False


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(405); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            body = json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}, "id": None})
            self._send(200, body)
            return
        method, mid = msg.get("method"), msg.get("id")

        if method == "initialize":
            res = {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "finance-risk-mysql", "version": "1.0.0"}}}
            self._send(200, json.dumps(res), extra={"Mcp-Session-Id": "mock-session-abc123"})
            return
        if method.startswith("notifications/"):
            self.send_response(202); self.end_headers(); return
        if method == "ping":
            self._send(200, json.dumps({"jsonrpc":"2.0","id":mid,"result":{}})); return
        if method == "tools/list":
            tools = [{"name": k, "description": f"mock {k}", "inputSchema": {
                "type": "object", "properties": v["props"], "required": v["required"],
                "additionalProperties": False}} for k, v in TOOLS.items()]
            self._send(200, json.dumps({"jsonrpc":"2.0","id":mid,"result":{"tools":tools}})); return
        if method == "tools/call":
            p = msg.get("params") or {}
            name = p.get("name") or ""
            args = p.get("arguments") or {}
            if name not in TOOLS:
                self._send(200, json.dumps({"jsonrpc":"2.0","id":mid,
                    "error":{"code":-32602,"message":f"未知工具 {name}"}})); return
            with LOCK:
                err = validate(name, args) if STRICT else None
                if err:
                    self._send(200, json.dumps({"jsonrpc":"2.0","id":mid,
                        "error":{"code":-32602,"message":err}})); return
                key = (TENANT, name, args.get("request_id"))
                h = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if args.get("request_id") and key in LEDGER:
                    old_h, old_res = LEDGER[key]
                    if old_h == h:
                        body = dict(old_res); body["data"] = {**(old_res.get("data") or {}), "deduplicated": True}
                        self._text(mid, body, True); return
                    self._send(200, json.dumps({"jsonrpc":"2.0","id":mid, "result": {
                        "content":[{"type":"text","text":json.dumps({"ok":False,"code":"IDEMPOTENCY_CONFLICT",
                            "message":"同幂等键不同内容","request_id":args.get("request_id"),"server_time":"2026-10-21T15:00:00Z","data":{}},
                            ensure_ascii=False)}],
                        "structuredContent":{"ok":False,"code":"IDEMPOTENCY_CONFLICT","message":"同幂等键不同内容",
                            "request_id":args.get("request_id"),"server_time":"2026-10-21T15:00:00Z","data":{}},
                        "isError":True}})); return
                res, is_err = dispatch(name, args)
                if res.get("ok"):
                    if name == "log_to_mysql":
                        AUDIT.append({"tenant": TENANT, "request_id": args.get("request_id"),
                                      "rule_id": args.get("rule_id"), "rule_version": args.get("rule_version"),
                                      "risk_level": args.get("risk_level"), "event_type": args.get("event_type")})
                        res = {**res, "data": {**res.get("data", {}), "deduplicated": False}}
                    if args.get("request_id"):
                        LEDGER[key] = (h, res)
            self._text(mid, res, is_err); return
        self._send(200, json.dumps({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"method not found"}}))

    def _text(self, mid, res, is_err):
        payload = {"jsonrpc":"2.0","id":mid,"result":{
            "content":[{"type":"text","text":json.dumps(res, ensure_ascii=False)}],
            "structuredContent": res, "isError": is_err}}
        self._send(200, json.dumps(payload))

    def _send(self, status, body, extra=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8001)
    a = ap.parse_args()
    print(f"[mock] STRICT_PROPS={'on' if STRICT else 'off'} port={a.port}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()

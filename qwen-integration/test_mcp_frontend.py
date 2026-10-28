# -*- coding: utf-8 -*-
"""千问现金流 MCP 自检。

默认用假的引擎返回值测试协议、校验和 HTTP 鉴权，不访问网络。
CF_LIVE_TEST=1 时额外调用真实服务器，核对金值；可选 CF_LIVE_WRITE=1 测保存与回查。
"""
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mcp_frontend_server as S  # noqa: E402

fails = []


def check(name, condition, got=""):
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else " -> %s" % (got,)))
    if not condition:
        fails.append(name)


GOLDEN = {"status": "calculated", "can_save_source_result": True,
          "result": {"period": "2026-08", "source_revision": "rev-test",
                     "run_id": "run-test",
                     "amounts": {"closing": "177000.00", "difference": "0.00"}}}


def fake_engine(tool, arguments):
    if tool == "calculate_manufacturing_cashflow":
        return GOLDEN
    if tool == "save_cashflow_result":
        return {"status": "committed", "receipt": {"record_id": "rec-test"}}
    if tool == "query_cashflow_result":
        return {"status": "committed", "result": GOLDEN["result"]}
    if tool == "list_manufacturing_sources":
        return {"sources": [{"source_id": "synthetic_eight_sheet_v1"}]}
    return {"tool": tool, "arguments": arguments}


S.engine_call = fake_engine
REQ = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "calculate_cashflow", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "calculate_cashflow", "arguments": {"source_id": "not-allowed"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "frontend_dispatch", "arguments": {"action": "no.such"}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
        "name": "save_cashflow_result",
        "arguments": {"result": GOLDEN["result"], "request_id": "req-test-save"}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "query_cashflow_result",
        "arguments": {"request_id": "req-test-save", "period": "2026-08",
                      "source_revision": "rev-test", "run_id": "run-test"}}},
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
        "name": "not.exist", "arguments": {}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
]
stdin_buf = io.StringIO("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in REQ))
stdout_buf = io.StringIO()
old_in, old_out = sys.stdin, sys.stdout
sys.stdin, sys.stdout = stdin_buf, stdout_buf
try:
    S.serve_stdio()
finally:
    sys.stdin, sys.stdout = old_in, old_out
resps = {}
for line in stdout_buf.getvalue().splitlines():
    obj = json.loads(line)
    if obj.get("id") is not None:
        resps[obj["id"]] = obj

print("=== stdio 协议与业务校验 ===")
check("initialize 返回 v2 服务", resps[1]["result"]["serverInfo"]["version"] == "2.0.0")
names = [t["name"] for t in resps[2]["result"]["tools"]]
check("10 个真实工具", len(names) == 10, names)
check("工具名不含点号", all("." not in n for n in names), names)
check("默认来源计算成功", resps[3]["result"]["structuredContent"]["result"]
      ["amounts"]["closing"] == "177000.00")
check("非法来源被拦截", resps[4]["result"]["structuredContent"]["code"] == 4001)
check("未知 action 被拦截", resps[5]["result"]["structuredContent"]["code"] == 4002)
check("保存返回 committed", resps[6]["result"]["structuredContent"]["status"] == "committed")
check("回查返回 committed", resps[7]["result"]["structuredContent"]["status"] == "committed")
check("未知工具返回 JSON-RPC error", "error" in resps[8])
check("通知不污染协议通道", len(resps) == 8)

print("\n=== HTTP 传输与鉴权 ===")
S._Handler.TOKEN = "tk-demo"
srv = S.ThreadingHTTPServer(("127.0.0.1", 0), S._Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port


def post(payload, token=None):
    req = urllib.request.Request(base + "/mcp", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-MCP-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        return {"http_error": exc.code}


list_req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
check("无 token 返回 401", post(list_req).get("http_error") == 401)
check("错 token 返回 401", post(list_req, "wrong").get("http_error") == 401)
check("正确 token 返回工具", "result" in post(list_req, "tk-demo"))
srv.shutdown()

if os.environ.get("CF_LIVE_TEST") == "1":
    print("\n=== 真实服务器金值 ===")
    import importlib
    live = importlib.reload(S)
    calculated = live.call_tool("calculate_cashflow",
                                {"source_id": "synthetic_eight_sheet_v1"})
    check("完整八表期末 177000.00",
          calculated["result"]["amounts"]["closing"] == "177000.00", calculated)
    difference = live.call_tool("calculate_cashflow",
                                {"source_id": "program_difference_v1"})
    check("差异负例 -1000.00",
          difference["result"]["amounts"]["difference"] == "-1000.00", difference)
    check("差异负例禁止保存", difference["can_save_source_result"] is False)
    if os.environ.get("CF_LIVE_WRITE") == "1":
        request_id = "qwen-test-" + uuid.uuid4().hex[:12]
        saved = live.call_tool("save_cashflow_result",
                               {"result": calculated["result"], "request_id": request_id})
        check("真实保存 committed", saved.get("status") == "committed", saved)
        result = calculated["result"]
        queried = live.call_tool("query_cashflow_result", {
            "request_id": request_id, "period": result["period"],
            "source_revision": result["source_revision"], "run_id": result["run_id"]})
        check("真实回查 committed", queried.get("status") == "committed", queried)

print("\n结果：%s" % ("全部通过" if not fails else "失败 %d 项：%s" % (len(fails), fails)))
sys.exit(1 if fails else 0)

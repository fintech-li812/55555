"""End-to-end check: calculate -> save -> exact query back.

Fails loudly. Never treats an empty or partial response as success.
Usage: python verify_closed_loop.py --base http://127.0.0.1:8000 --token <token>
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
import uuid

SOURCE = "synthetic_eight_sheet_v1"


def call(base: str, path: str, token: str, payload=None):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["X-Workbench-Token"] = token
    request = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("WORKBENCH_BASE", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("WORKBENCH_TOKEN", ""))
    parser.add_argument("--source", default=SOURCE)
    args = parser.parse_args()

    print("1) 计算")
    calc = call(args.base, "/api/calculate?source_id=" + args.source, args.token)
    result = calc.get("result")
    if not result:
        print("   失败：没有返回结果", json.dumps(calc, ensure_ascii=False)[:300])
        return 1
    print("   期末={} 差异={} 状态={}".format(result["amounts"]["closing"], result["amounts"]["difference"], result.get("status")))

    request_id = "verify-" + uuid.uuid4().hex[:12]
    print("2) 保存 request_id=" + request_id)
    saved = call(args.base, "/api/save", args.token, {"result": result, "request_id": request_id})
    print("   状态={}".format(saved.get("status")))
    if saved.get("status") != "committed":
        print("   失败：未确认提交", json.dumps(saved, ensure_ascii=False)[:400])
        return 1
    record_id = saved.get("receipt", {}).get("record_id")
    print("   记录编号={}".format(record_id))

    print("3) 精确回查")
    queried = call(args.base, "/api/query", args.token, {
        "request_id": request_id,
        "period": result["period"],
        "source_revision": result["source_revision"],
        "run_id": result["run_id"],
    })
    if queried.get("status") != "committed":
        print("   失败：回查未返回已提交记录", json.dumps(queried, ensure_ascii=False)[:400])
        return 1
    back = queried["result"]

    checks = [
        ("金额逐项一致", json.dumps(back["amounts"], sort_keys=True) == json.dumps(result["amounts"], sort_keys=True)),
        ("来源快照一致", back["source"]["snapshot_ref"] == result["source"]["snapshot_ref"]),
        ("算法哈希一致", back.get("execution", {}).get("algorithm_sha256") == result.get("execution", {}).get("algorithm_sha256")),
        ("记录编号一致", queried.get("receipt", {}).get("record_id") == record_id),
    ]
    print("4) 核对")
    ok = True
    for name, passed in checks:
        print("   {} {}".format("通过" if passed else "失败", name))
        ok = ok and passed
    print()
    print("闭环{}".format("通过：计算、保存、精确回查全部一致。" if ok else "未通过。"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

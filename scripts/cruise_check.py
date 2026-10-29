#!/usr/bin/env python
"""主流程与功能模块巡航检查。

不是跑单元测试，而是**按用户视角把系统走一遍**：
主流程、各 API 端点、边界输入、异常输入、并发、缓存淘汰，
把每个不符合预期的响应记成一条 finding，修完再跑，直到 finding 清零。

用法::

    python scripts/cruise_check.py                 # 自动起服务、跑完、关掉
    python scripts/cruise_check.py --base http://127.0.0.1:8010
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "mcp-server", "program_v1", "src")

FINDINGS = []


def note(level, where, what):
    FINDINGS.append((level, where, what))
    print("  [%s] %s：%s" % (level, where, what), flush=True)


class Http:
    def __init__(self, base, timeout=120):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def get(self, path):
        # 中文路径必须先编码：urllib 遇到非 ASCII 会直接抛异常，
        # 那看起来像"接口挂了"，其实是探测脚本自己没编码。
        safe = urllib.parse.quote(path, safe="/?&=%")
        try:
            with urllib.request.urlopen(self.base + safe,
                                        timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return 0, ("%s: %s" % (type(e).__name__, e)).encode()

    def get_json(self, path):
        code, body = self.get(path)
        try:
            return code, json.loads(body.decode("utf-8"))
        except Exception:
            return code, {"_raw": body[:200].decode("utf-8", "replace")}

    def _decode(self, raw):
        """容忍非 JSON 的响应体（反代、网关的 413/502 页面通常是 HTML）。"""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {"_raw": raw[:120].decode("utf-8", "replace")}

    def post_json(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, self._decode(r.read())
        except urllib.error.HTTPError as e:
            return e.code, self._decode(e.read())
        except Exception as e:
            return 0, {"error": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- 各检查项


def check_pages(h):
    print("— 页面与静态端点")
    for path, name in (("/", "V8 工作台页面"),
                       ("/api/v8/config", "开关配置"),
                       ("/api/v8/algorithms", "算法列表"),
                       ("/api/v8/health", "健康检查"),
                       ("/api/v8/industries", "行业参数"),
                       ("/api/v8/scenarios", "受评场景")):
        code, body = h.get(path)
        if code != 200:
            note("FAIL", name, "HTTP %s" % code)
        elif not body:
            note("FAIL", name, "返回空内容")


def check_main_flow(h):
    print("— 主流程：三算法 × 三场景")
    for algo in ("v8", "ra-next-0.1", "v1"):
        for sc in ("healthy", "edge", "risk"):
            code, d = h.get_json("/api/v8/assess?scenario=%s&algo=%s" % (sc, algo))
            if code != 200:
                note("FAIL", "主流程 %s/%s" % (algo, sc), "HTTP %s" % code)
                continue
            for key in ("第一层", "第二层", "第三层", "融合判定", "最终等级",
                        "现金流事实", "压力测试", "分级审批"):
                if key not in d:
                    note("FAIL", "主流程 %s/%s" % (algo, sc), "缺字段 %s" % key)
            fin = d.get("最终等级", {})
            if fin.get("等级") not in ("绿灯", "黄灯", "红灯"):
                note("FAIL", "主流程 %s/%s" % (algo, sc),
                     "等级非法 %r" % fin.get("等级"))
            # 三场景必须能区分开，否则整个模型的输出是常量
            print("     %-11s %-8s → %s" % (algo, sc, fin.get("等级")))


def check_switch_matrix(h):
    print("— 开关矩阵")
    combos = []
    for q in ("", "&q_in_fusion=0"):
        for e in ("", "&evidence=loose"):
            for b in ("", "&benford=1", "&benford=0"):
                combos.append(q + e + b)
    for qs in combos:
        for sc in ("healthy", "risk"):
            code, d = h.get_json("/api/v8/assess?scenario=%s&algo=v1%s" % (sc, qs))
            if code != 200:
                note("FAIL", "开关组合 %s/%s" % (qs, sc), "HTTP %s" % code)
                continue
            if "生效开关" not in d:
                note("FAIL", "开关组合 %s/%s" % (qs, sc), "缺少生效开关回显")
    # 关键语义：benford=0 必须导致 Q 不进融合 → 红灯
    code, d = h.get_json("/api/v8/assess?scenario=healthy&algo=v1&benford=0")
    if code == 200 and d["最终等级"]["等级"] != "红灯":
        note("WARN", "benford=0", "期望红灯，实得 %s" % d["最终等级"]["等级"])


def check_boundary(h):
    print("— 边界与异常输入")
    cases = [
        # (路径, 期望 HTTP 码, 说明)
        ("/api/v8/assess?n=2", 200, "最小样本池 2"),
        ("/api/v8/assess?n=60", 200, "最大样本池 60"),
        ("/api/v8/assess?trees=1", 200, "最少树数 1"),
        ("/api/v8/assess?trees=500", 200, "最多树数 500（v8 不用树，仅记录）"),
        ("/api/v8/assess?amount=0", 200, "金额 0"),
        ("/api/v8/assess?amount=-1", 200, "负金额"),
        ("/api/v8/assess?amount=abc", 400, "非数字金额"),
        ("/api/v8/assess?n=0", 400, "样本池 0"),
        ("/api/v8/assess?n=-5", 400, "负样本池"),
        ("/api/v8/assess?n=61", 400, "样本池超上界"),
        ("/api/v8/assess?n=999999", 400, "样本池超大（DoS 探测）"),
        ("/api/v8/assess?algo=v1&trees=0", 400, "树数 0"),
        ("/api/v8/assess?algo=v1&trees=999999", 400, "树数超范围"),
        ("/api/v8/assess?algo=nope", 400, "未知算法"),
        ("/api/v8/assess?amount=1e309", 400, "金额溢出（Inf 应被拦）"),
        ("/api/v8/assess?n=abc", 400, "样本池非数字"),
        ("/api/v8/assess?industry=不存在的行业", 200, "未知行业"),
        ("/api/v8/assess?scenario=不存在的场景", 200, "未知场景（应回落 edge）"),
        ("/api/v8/assess?stability=1", 200, "开启稳定性"),
        ("/api/v8/assess?benford=banana", 200, "非法 benford 值（按未声明处理）"),
        ("/api/v8/assess?evidence=banana", 200, "非法证据模式（按 strict 处理）"),
    ]
    for path, want, label in cases:
        code, d = h.get_json(path)
        if code != want:
            note("FAIL", "边界 %s" % label,
                 "期望 HTTP %s，实得 %s  %s" % (
                     want, code, (d.get("error") or "") [:60]))
        elif code == 200 and "最终等级" not in d:
            note("FAIL", "边界 %s" % label, "200 但缺少最终等级")


def check_not_found(h):
    print("— 未定义路径")
    code, _b = h.get("/api/v8/does-not-exist")
    # 未定义路径不该被当成 200 的空响应
    if code == 200:
        note("WARN", "/api/v8/does-not-exist", "未定义路径返回 200")
    code, _b = h.get("/nope")
    if code == 200:
        note("WARN", "/nope", "未定义路径返回 200")


def check_save_and_logs(h):
    print("— 留痕与日志（需要数据库，未配置时应如实说明而非假装成功）")
    code, d = h.post_json("/api/v8/save", {"scenario": "edge", "algo": "v1"})
    if code == 200 and d.get("status") == "committed":
        print("     留痕成功：%s" % d.get("request_id"))
    elif code == 503:
        # 留痕失败但评估结果必须一并带回，否则用户白算一次
        if d.get("status") != "not_stored" or "level" not in d:
            note("FAIL", "POST /api/v8/save",
                 "503 但没带回评估结果：%s" % json.dumps(d)[:100])
        else:
            print("     留痕不可用（预期）：%s" % str(d.get("原因"))[:50])
    elif code >= 500:
        note("FAIL", "POST /api/v8/save",
             "返回 %s：%s" % (code, str(d.get("error"))[:60]))
    # 请求体过大必须被挡住
    big = {"scenario": "edge", "padding": "x" * (2 * 1024 * 1024)}
    c2, d2 = h.post_json("/api/v8/save", big)
    # 413 可能来自反代（nginx client_max_body_size）也可能来自应用层，
    # 两者都算"被挡住了"。应用层还会带 code=payload_too_large。
    if c2 != 413:
        note("FAIL", "POST 请求体限长", "期望 413，实得 %s" % c2)
    elif d2.get("code") == "payload_too_large":
        print("     请求体限长生效（应用层）")
    else:
        print("     请求体限长生效（反代层，应用层未触及）")
    code, d = h.get_json("/api/v8/logs")
    if code == 200 and isinstance(d, dict) and "logs" in d:
        print("     日志接口正常，%d 条" % len(d["logs"]))
    elif code == 503:
        # 数据库不可用是可预期的降级，返回 503 且不带 logs 不算问题
        if "logs" not in d:
            note("WARN", "GET /api/v8/logs", "503 但缺 logs 字段")
        else:
            print("     留痕不可用（预期）：%s" % str(d.get("原因"))[:50])
    elif code >= 500:
        note("FAIL", "GET /api/v8/logs", "返回 %s：%s" % (code, str(d.get("error"))[:60]))


def check_concurrency(h, n_threads=8, per=6):
    print("— 并发 %d 线程 × %d 次" % (n_threads, per))
    errors = []
    lat = []
    lock = threading.Lock()

    def worker():
        for i in range(per):
            t0 = time.perf_counter()
            code, d = h.get_json("/api/v8/assess?algo=v1&scenario=edge")
            dt = time.perf_counter() - t0
            with lock:
                lat.append(dt)
                if code != 200:
                    errors.append((code, str(d.get("error"))[:60]))

    ths = [threading.Thread(target=worker) for _ in range(n_threads)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    total = time.perf_counter() - t0
    if errors:
        note("FAIL", "并发", "%d 次失败：%s" % (len(errors), errors[:3]))
    lat.sort()
    p95 = lat[int(len(lat) * 0.95)] if lat else 0
    print("     %d 次调用 %.2fs，P95 %.3fs" % (len(lat), total, p95))
    if p95 > 5:
        note("WARN", "并发延迟", "P95 %.2fs 偏高" % p95)


def check_determinism(h):
    print("— 确定性：同一输入连打 5 次结果必须逐位一致")
    for algo in ("v8", "v1"):
        seen = set()
        for _ in range(5):
            code, d = h.get_json("/api/v8/assess?scenario=risk&algo=%s" % algo)
            if code == 200:
                seen.add(json.dumps([d["第一层"]["Q"], d["第二层"]["D"],
                                     d["第三层"]["P"], d["融合判定"]["规则ID"]],
                                    sort_keys=True))
        if len(seen) > 1:
            note("FAIL", "确定性 %s" % algo, "5 次调用得到 %d 种结果" % len(seen))


def check_frontend_js(verbose=True):
    print("— 前端内联 JS 语法")
    page = os.path.join(ROOT, "web", "统一工作台.html")
    if not os.path.exists(page):
        note("FAIL", "前端", "找不到 web/统一工作台.html")
        return
    import re
    text = open(page, encoding="utf-8").read()
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", text, re.S)
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "cruise_wb.js")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n;\n".join(blocks))
    try:
        r = subprocess.run(["node", "--check", tmp], capture_output=True,
                           timeout=60)
        if r.returncode != 0:
            note("FAIL", "前端 JS", (r.stderr or b"").decode("utf-8",
                                                            "replace")[:300])
        elif verbose:
            print("     %d 个 script 块，语法通过" % len(blocks))
    except FileNotFoundError:
        print("     未安装 node，跳过 JS 语法检查")
    except Exception as e:
        note("WARN", "前端 JS", "%s: %s" % (type(e).__name__, e))
    # 页面里引用的元素 id 必须在文档中存在，否则前端一运行就 null.value
    ids = set(re.findall(r'id="([^"]+)"', text))
    for ref in sorted(set(re.findall(r"U\.\$\('([A-Za-z0-9_]+)'\)", text))):
        if ref not in ids:
            note("FAIL", "前端元素引用",
                 "JS 引用 U.$('%s') 但页面无此 id" % ref)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="")
    ap.add_argument("--port", type=int, default=8050)
    args = ap.parse_args()

    proc = None
    if args.base:
        base = args.base
    else:
        env = dict(os.environ, CF_V8_PORT=str(args.port))
        proc = subprocess.Popen([sys.executable, "v8_server.py"], cwd=SRC,
                                env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:%d" % args.port
        for _ in range(40):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(base + "/api/v8/health", timeout=2)
                break
            except Exception:
                pass
        print("已启动本地服务 %s" % base)

    h = Http(base)
    try:
        check_pages(h)
        check_main_flow(h)
        check_switch_matrix(h)
        check_boundary(h)
        check_not_found(h)
        check_save_and_logs(h)
        check_determinism(h)
        check_concurrency(h)
    finally:
        check_frontend_js()
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    print()
    bad = [f for f in FINDINGS if f[0] == "FAIL"]
    warn = [f for f in FINDINGS if f[0] == "WARN"]
    print("=" * 60)
    print("巡航结果：FAIL %d / WARN %d" % (len(bad), len(warn)))
    for level, where, what in FINDINGS:
        print("  %-4s %-28s %s" % (level, where, what))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

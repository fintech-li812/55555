# -*- coding: utf-8 -*-
"""v8 风控工作台服务（端口 8010）。

提供：
  GET  /                    工作台页面
  GET  /api/v8/assess       一次完整 v8 评估（三层模型 + 相对阈值 + 压力测试 + 审批）
  GET  /api/v8/industries   四行业参数模板
  GET  /api/v8/scenarios    受评场景列表
  POST /api/v8/save         评估结果留痕（模块六：MCP 写 MySQL，自动建表）
  GET  /api/v8/logs         最近留痕记录

与已冻结的 v1.2 服务（8000）并行运行，互不干扰；不改动任何既有代码。
数据库配置来自 deploy/.env（本机 3307 隔离 QA 库），未配置则留痕不可用但评估仍可运行。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SRC = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SRC)))  # CashflowRiskWorkbench
sys.path.insert(0, SRC)

from v8 import engine, industry as ins, indicators as ind  # noqa: E402

PORT = int(os.environ.get("CF_V8_PORT", "8010"))
HTML_PATH = os.path.join(ROOT, "web", "v8_工作台.html")

DDL = """
CREATE TABLE IF NOT EXISTS mfg_v8_risk_log_v1 (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  request_id    VARCHAR(64) NOT NULL,
  industry      VARCHAR(32) NOT NULL,
  scenario      VARCHAR(32) NOT NULL,
  q_score       DECIMAL(8,4),
  d_score       DECIMAL(12,6),
  p_score       DECIMAL(8,4),
  level         VARCHAR(8),
  buffer_ratio  DECIMAL(12,6),
  worst_scene   VARCHAR(32),
  payload_json  JSON,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_request (request_id),
  INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
"""


def _load_env():
    path = os.path.join(ROOT, "deploy", ".env")
    if not os.path.exists(path):
        return {}
    env = {}
    for line in open(path, encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


ENV = _load_env()


def _db():
    try:
        import pymysql
        return pymysql.connect(
            host=ENV.get("CF_DB_HOST", "127.0.0.1"),
            port=int(ENV.get("CF_DB_PORT", "3307")),
            user=ENV.get("CF_DB_USER", ""),
            password=ENV.get("CF_DB_PASSWORD", ""),
            database=ENV.get("CF_DB_NAME", ""),
            charset="utf8mb4", connect_timeout=8)
    except Exception as e:
        raise RuntimeError("数据库连接失败：%s" % e)


def _ensure(cur):
    """建表（容错）。应用账号 cf_app 仅有 SELECT/INSERT，无 CREATE 权限；
    表已由运维（root）预建并授权时这里会失败，忽略即可；若表确实不存在，
    后续 INSERT 会如实报错，不伪装成功。

    修复：原实现递归调用自身（_ensure → _ensure → …），撞到 RecursionError
    后被同一个 except 吞掉，DDL 从未真正执行过，等于永远建不了表。
    """
    try:
        cur.execute(DDL)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", ""):
            try:
                html = open(HTML_PATH, encoding="utf-8").read()
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")
            return
        if u.path == "/api/v8/assess":
            try:
                r = engine.assess(
                    industry=q.get("industry", "制造业"),
                    scenario=q.get("scenario", "edge"),
                    amount=float(q.get("amount", 120000) or 120000),
                    n_per_group=int(q.get("n", 20) or 20))
                self._json(r)
            except Exception as e:
                self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            return
        if u.path == "/api/v8/industries":
            self._json({"source": ins.THRESHOLD_SOURCE,
                        "industries": {k: v for k, v in ins.INDUSTRIES.items()}})
            return
        if u.path == "/api/v8/scenarios":
            self._json({"scenarios": [
                {"key": k, "name": v["name"], "note": v["note"]}
                for k, v in ind.TARGET_SCENARIOS.items()]})
            return
        if u.path == "/api/v8/logs":
            try:
                c = _db()
                with c.cursor() as cur:
                    _ensure(cur)
                    cur.execute("SELECT request_id, industry, scenario, q_score, d_score, "
                                "p_score, level, buffer_ratio, worst_scene, created_at "
                                "FROM mfg_v8_risk_log_v1 ORDER BY id DESC LIMIT 20")
                    rows = cur.fetchall()
                c.close()
                self._json({"logs": [{
                    "request_id": r[0], "industry": r[1], "scenario": r[2],
                    "Q": float(r[3]) if r[3] is not None else None,
                    "D": float(r[4]) if r[4] is not None else None,
                    "P": float(r[5]) if r[5] is not None else None,
                    "等级": r[6], "缓冲倍数": float(r[7]) if r[7] is not None else None,
                    "最坏场景": r[8], "时间": str(r[9])} for r in rows]})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/v8/save":
            self._json({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            r = engine.assess(industry=body.get("industry", "制造业"),
                              scenario=body.get("scenario", "edge"),
                              amount=float(body.get("amount", 120000) or 120000))
            rid = body.get("request_id") or ("v8-" + uuid.uuid4().hex[:12])
            c = _db()
            with c.cursor() as cur:
                _ensure(cur)
                cur.execute(
                    "INSERT INTO mfg_v8_risk_log_v1 (request_id, industry, scenario, "
                    "q_score, d_score, p_score, level, buffer_ratio, worst_scene, payload_json) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (rid, r["行业"], body.get("scenario", "edge"),
                     r["第一层"]["Q"], r["第二层"]["D"], r["第三层"]["P"],
                     r["融合判定"]["等级"], r["缓冲判定"]["缓冲倍数"] if r["缓冲判定"] else None,
                     r["压力测试"]["最坏情况"]["场景"] if r["压力测试"] else None,
                     json.dumps(r, ensure_ascii=False)))
            c.commit()
            c.close()
            self._json({"status": "committed", "request_id": rid,
                        "level": r["融合判定"]["等级"], "Q": r["第一层"]["Q"],
                        "D": r["第二层"]["D"], "P": r["第三层"]["P"]})
        except Exception as e:
            self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("v8 工作台已启动：http://127.0.0.1:%d/" % PORT)
    print("页面：%s" % HTML_PATH)
    print("数据库：%s（未配置则留痕不可用，评估仍可运行）" %
          (ENV.get("CF_DB_NAME") or "未读取到 deploy/.env"))
    srv.serve_forever()


if __name__ == "__main__":
    main()

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
import logging
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SRC = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SRC)))  # CashflowRiskWorkbench
sys.path.insert(0, SRC)

from v8 import engine, industry as ins, indicators as ind  # noqa: E402
from v8 import algorithm as v8algo  # noqa: E402
from v8 import config as v8cfg  # noqa: E402

# POST 请求体上限：留痕请求只有几个字段，1 MB 已是极大余量
MAX_BODY = 1 * 1024 * 1024

LOGGER = logging.getLogger("v8.server")
logging.basicConfig(level=os.environ.get("CF_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

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


class DbUnavailable(RuntimeError):
    """数据库不可用。这是**可预期的降级**，不是服务故障。"""


def _db():
    try:
        import pymysql
    except ImportError as e:
        # 缺依赖与连不上是两回事：前者是部署不全，后者是数据库可能没起。
        # 混成一条 "数据库连接失败" 会让运维去查数据库，白折腾一轮。
        raise DbUnavailable("未安装数据库驱动 pymysql（留痕功能不可用，评估不受影响）")
    try:
        return pymysql.connect(
            host=ENV.get("CF_DB_HOST", "127.0.0.1"),
            port=int(ENV.get("CF_DB_PORT", "3307")),
            user=ENV.get("CF_DB_USER", ""),
            password=ENV.get("CF_DB_PASSWORD", ""),
            database=ENV.get("CF_DB_NAME", ""),
            charset="utf8mb4", connect_timeout=8)
    except Exception as e:
        raise DbUnavailable("数据库连接失败：%s" % e)


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
                r = engine.assess(**_overrides(q, industry=q.get("industry", "制造业"),
                                               scenario=q.get("scenario", "edge"),
                                               amount=float(q.get("amount", 120000) or 120000)))
                self._json(r)
            except v8algo.validation.ValidationError as e:
                # 参数错误是客户端问题，返回 400，不要记成服务端故障
                self._json({"error": str(e), "code": e.code, "field": e.field}, 400)
            except ValueError as e:
                self._json({"error": str(e), "code": "invalid_argument"}, 400)
            except Exception as e:
                LOGGER.exception("assess 失败")
                self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            return
        if u.path == "/api/v8/config":
            # 上线核验用：一眼看清当前生效口径与每一项的取值来源
            self._json(v8cfg.snapshot())
            return
        if u.path == "/api/v8/algorithms":
            self._json({"algorithms": sorted(engine.ALGOS.keys()),
                        "default": v8cfg.resolve().algo,
                        "cache": v8algo.cache_stats()})
            return
        if u.path == "/api/v8/health":
            self._json({"ok": True, "algorithms": sorted(engine.ALGOS.keys()),
                        "cache": v8algo.cache_stats()})
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
            except DbUnavailable as e:
                # 留痕读不到是配置/环境问题，不是 5xx 服务故障。
                # 返回 503 并说清原因，前端才能显示「留痕不可用」而不是「服务器错误」。
                self._json({"logs": [], "留痕可用": False, "原因": str(e)}, 503)
                return
            try:
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
                    "最坏场景": r[8], "时间": str(r[9])} for r in rows],
                    "留痕可用": True})
            except DbUnavailable as e:
                self._json({"logs": [], "留痕可用": False, "原因": str(e)}, 503)
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        self._json({"error": "not found"}, 404)

    def _drain(self, size: int, chunk: int = 65536):
        """丢弃请求体剩余字节，避免提前响应导致客户端连接被重置。"""
        left = size
        while left > 0:
            try:
                got = len(self.rfile.read(min(chunk, left)))
            except (OSError, ValueError):
                break
            if not got:
                break
            left -= got

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/v8/save":
            self._json({"error": "not found"}, 404)
            return

        # ---- 1. 读请求体：先限长再读 ----
        # 不留这一步，一句 Content-Length: 99999999999 就能让服务端去分配
        # 几十 GB 内存。留痕请求体本就只有几个字段，1 MB 已是极大余量。
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            # 拒绝前必须先把请求体读掉（或至少丢弃到上限的两倍）。
            # 否则客户端还在往里写 2MB，服务端已经回 413 并关连接，
            # 客户端收到的是 ConnectionReset 而不是那句 413，
            # 看上去像"服务挂了"，实际是拒绝生效了但没说清楚。
            self._drain(min(n, MAX_BODY * 2))
            self._json({"error": "请求体过大（上限 %d 字节）" % MAX_BODY,
                        "code": "payload_too_large"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._json({"error": "请求体不是合法 JSON：%s" % e,
                        "code": "bad_json"}, 400)
            return

        # ---- 2. 评估（与留痕解耦：库挂了也要把结果算出来）----
        try:
            r = engine.assess(**_overrides(
                body, industry=body.get("industry", "制造业"),
                scenario=body.get("scenario", "edge"),
                amount=float(body.get("amount", 120000) or 120000)))
        except v8algo.validation.ValidationError as e:
            self._json({"error": str(e), "code": e.code, "field": e.field}, 400)
            return
        except ValueError as e:
            self._json({"error": str(e), "code": "invalid_argument"}, 400)
            return
        except Exception as e:
            LOGGER.exception("assess 失败")
            self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            return

        summary = {"level": r["最终等级"]["等级"], "Q": r["第一层"]["Q"],
                   "D": r["第二层"]["D"], "P": r["第三层"]["P"],
                   "证据": r["最终等级"].get("证据")}

        # ---- 3. 留痕 ----
        rid = body.get("request_id") or ("v8-" + uuid.uuid4().hex[:12])
        try:
            c = _db()
        except DbUnavailable as e:
            # 留痕失败不等于这次评估作废。把结果一并带回去，
            # 否则用户看到 503 会以为整次评估都失败了，白白浪费一次计算。
            out = {"status": "not_stored", "留痕可用": False, "原因": str(e)}
            out.update(summary)
            self._json(out, 503)
            return
        try:
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
        except Exception as e:
            LOGGER.exception("留痕写入失败")
            out = {"status": "not_stored", "留痕可用": False,
                   "原因": str(e)}
            out.update(summary)
            self._json(out, 503)
            return
        out = {"status": "committed", "request_id": rid, "留痕可用": True}
        out.update(summary)
        self._json(out)


def _flag(value) -> bool:
    """'1'/'true'/'yes' → True，其余（含缺失）→ False。"""
    return str(value or "").lower() in ("1", "true", "yes", "on")


def _tri(value):
    """三态解析：缺失 → None；'1'/'true' → True；'0'/'false' → False。

    Benford 适用性是三态语义，缺失不等于「不适用」——缺失只表示未声明。
    """
    if value is None or value == "":
        return None
    s = str(value).lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def _overrides(params, **base):
    """把 HTTP 参数翻译成 engine.assess 的关键字参数。

    **只放进请求里真的出现了的参数**，其余一律不出现——
    这样没传的项会落到 config 的四级取值上（环境变量 > 配置文件 > 内置默认），
    而不是被 HTTP 层的硬编码默认值盖掉。
    这是开关能生效的关键：以前 `algo=q.get("algo","v8")` 会把默认值
    焊死在 URL 层，配置里改成 v1 也永远读不到。
    """
    kw = dict(base)
    if params.get("n") not in (None, ""):
        kw["n_per_group"] = int(params["n"])
    if params.get("algo") not in (None, ""):
        kw["algo"] = params["algo"]
    if params.get("trees") not in (None, ""):
        kw["n_trees"] = int(params["trees"])
    if params.get("stability") not in (None, ""):
        kw["with_stability"] = _flag(params["stability"])
    if params.get("benford") not in (None, ""):
        kw["benford_applicable"] = _tri(params["benford"])
    if params.get("q_in_fusion") not in (None, ""):
        kw["q_in_fusion"] = _flag(params["q_in_fusion"])
    if params.get("evidence") not in (None, ""):
        kw["require_full_evidence_for_green"] = (
            str(params["evidence"]).lower() != "loose")
    return kw


def main():
    # 启动时把进程级开关（缓存）落到算法包上，并打印生效口径。
    # 上线核验时不用去猜：日志第一行就是当前走的是哪套口径。
    s = v8cfg.apply_runtime(v8cfg.resolve())
    LOGGER.info("生效开关：%s（来源 %s）", s.to_dict(), s.sources)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("v8 工作台已启动：http://127.0.0.1:%d/" % PORT)
    print("页面：%s" % HTML_PATH)
    print("数据库：%s（未配置则留痕不可用，评估仍可运行）" %
          (ENV.get("CF_DB_NAME") or "未读取到 deploy/.env"))
    srv.serve_forever()


if __name__ == "__main__":
    main()

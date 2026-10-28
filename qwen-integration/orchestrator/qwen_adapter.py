# -*- coding: utf-8 -*-
"""千问办公适配器：把「意图解析 / 结果解释」委托给千问办公。

配置（deploy/.env 或环境变量）：
  QWEN_API_BASE   —— 千问办公对外 API 基址（例如 https://<你的千问网关>/v1）
  QWEN_API_KEY    —— 鉴权密钥
  QWEN_MODEL      —— 模型名，默认 qwen-plus
  QWEN_MODE       —— auto（默认，失败自动降级本地）/ openapi / local

诚实说明（对应 v9 文档「命门二」）：
  千问办公是否对外暴露可供第三方网页直连的 HTTP API **尚未现场验证**。
  因此本适配器按「可配置、可降级」实现：
    * 配了 QWEN_API_BASE+KEY → 真调千问；
    * 没配或调用失败 → 降级本地编排（planner），并在事件流中如实标注 agent=local-planner。
  一旦千问开放对外 API，只需填好这两个变量，前端即可经本网关走千问编排，无需改代码。
"""
from __future__ import annotations

import json
import os
import urllib.request

from . import planner

SYSTEM_PROMPT = """你是制造业财务风险管控工作台的编排中枢（千问办公）。
硬性纪律：
1. 你不得计算、改写、四舍五入任何金额；所有数字由 MCP 工具产出。
2. 你只能从下列工具中选择，输出严格的 JSON 计划：
   {"intent":"<意图>","steps":[{"tool":"<工具名>","desc":"<一句话>","args":{...}}],"reply":"<给用户的一句话>"}
3. 不要输出 JSON 以外的任何文字。
工具清单见用户消息中的 tools 字段。"""


def _cfg(k, d=""):
    return os.environ.get(k) or d


class QwenAdapter:
    def __init__(self):
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "deploy", ".env")
        if os.path.exists(env_path):
            for line in open(env_path, encoding="utf-8").read().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        self.base = _cfg("QWEN_API_BASE")
        self.key = _cfg("QWEN_API_KEY")
        self.model = _cfg("QWEN_MODEL", "qwen-plus")
        self.mode = _cfg("QWEN_MODE", "auto")

    @property
    def enabled(self) -> bool:
        if self.mode == "local":
            return False
        return bool(self.base and self.key)

    def status(self) -> dict:
        return {"mode": self.mode, "configured": bool(self.base and self.key),
                "api_base": (self.base[:24] + "…") if self.base else None,
                "model": self.model, "active": self.enabled,
                "note": ("已配置千问 API，意图解析与解释由千问完成"
                         if self.enabled else
                         "未配置 QWEN_API_BASE/QWEN_KEY → 使用本地编排兜底（同一 MCP 工具层）")}

    # ------------------------------------------------------------ 意图解析
    def plan(self, text: str, params: dict, tools: list) -> dict:
        tools_brief = [{"name": t["name"], "description": t.get("description", "")[:120],
                        "inputSchema": t.get("inputSchema", {})} for t in tools]
        if not self.enabled:
            p = planner.build_plan(text, params, [t["name"] for t in tools])
            p["source"] = "local-planner"
            return p
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(
                    {"user_text": text, "current_params": params, "tools": tools_brief},
                    ensure_ascii=False)},
            ],
            "temperature": 0,
        }
        try:
            req = urllib.request.Request(
                self.base.rstrip("/") + "/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + self.key})
            with urllib.request.urlopen(req, timeout=30) as r:
                content = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
            plan = json.loads(content[content.find("{"):content.rfind("}") + 1])
            v = planner.validate_plan(plan, [t["name"] for t in tools])
            if not v["ok"]:
                p = planner.build_plan(text, params, [t["name"] for t in tools])
                p["source"] = "local-planner"
                p["fallback_reason"] = "千问返回的计划未通过校验：%s" % v["error"]
                return p
            plan["source"] = "qwen-office"
            plan["params"] = params
            for i, s in enumerate(plan["steps"], 1):
                s.setdefault("id", "s%d" % i)
            return plan
        except Exception as e:
            p = planner.build_plan(text, params, [t["name"] for t in tools])
            p["source"] = "local-planner"
            p["fallback_reason"] = "千问 API 调用失败：%s: %s" % (type(e).__name__, e)
            return p

    # ------------------------------------------------------------ 结果解释
    def explain(self, plan: dict, results: dict, params: dict) -> dict:
        local = planner.explain(plan.get("intent", ""), results, params)
        if not self.enabled:
            return {"text": local, "source": "local-template"}
        try:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content":
                        "你是财务风控助手。只能引用给定 JSON 中的数字，禁止计算、推断或改写任何数值。"
                        "用中文简要说明结论与建议动作，不超过 120 字。"},
                    {"role": "user", "content": json.dumps(results, ensure_ascii=False)[:6000]},
                ],
                "temperature": 0.2,
            }
            req = urllib.request.Request(
                self.base.rstrip("/") + "/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + self.key})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
            return {"text": text.strip(), "source": "qwen-office"}
        except Exception as e:
            return {"text": local, "source": "local-template",
                    "fallback_reason": "%s: %s" % (type(e).__name__, e)}

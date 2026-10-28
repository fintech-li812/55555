# -*- coding: utf-8 -*-
"""能力 2：定时巡检 —— 发现异常即告警，并留存巡检记录。

记录模型为不可变追加：一次巡检写 1 行 agent_inspection + N 行 agent_inspection_item。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List

from .errors import AppError, ERR_JOB_FAILED
from .risk import evaluate


class Inspector:
    def __init__(self, db, cfg, datasource, channel, logger):
        self.db = db
        self.cfg = cfg
        self.ds = datasource
        self.channel = channel
        self.log = logger

    # ---------- 单项检查 ----------
    def _check_source(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        missing = metrics.get("missing_sources") or []
        return {
            "code": "source_availability",
            "name": "数据源可用性",
            "passed": not missing,
            "level": "medium" if missing else "low",
            "detail": "全部可用" if not missing else json.dumps(missing, ensure_ascii=False),
        }

    def _check_netflow(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        net = metrics.get("net_flow")
        if net is None:
            return {"code": "net_flow", "name": "期间净现金流", "passed": True,
                    "level": "low", "detail": "无数据，不判定"}
        passed = net >= self.cfg.net_flow_floor
        return {"code": "net_flow", "name": "期间净现金流", "passed": passed,
                "level": "low" if passed else "high",
                "detail": f"net_flow={net} floor={self.cfg.net_flow_floor}"}

    def _check_buffer(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        br = metrics.get("buffer_ratio")
        if br is None:
            return {"code": "buffer_ratio", "name": "现金缓冲比", "passed": True,
                    "level": "low", "detail": "无数据，不判定"}
        if br <= self.cfg.buffer_ratio_high:
            level = "high"
        elif br <= self.cfg.buffer_ratio_warn:
            level = "medium"
        else:
            level = "low"
        return {"code": "buffer_ratio", "name": "现金缓冲比", "passed": level == "low",
                "level": level,
                "detail": f"buffer_ratio={br} warn<={self.cfg.buffer_ratio_warn} high<={self.cfg.buffer_ratio_high}"}

    def _check_overdue(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        orr = metrics.get("overdue_ratio")
        if orr is None:
            return {"code": "overdue_ratio", "name": "逾期应收占比", "passed": True,
                    "level": "low", "detail": "无数据，不判定"}
        if orr >= self.cfg.overdue_ratio_high:
            level = "high"
        elif orr >= self.cfg.overdue_ratio_warn:
            level = "medium"
        else:
            level = "low"
        return {"code": "overdue_ratio", "name": "逾期应收占比", "passed": level == "low",
                "level": level,
                "detail": f"overdue_ratio={orr} warn>={self.cfg.overdue_ratio_warn} high>={self.cfg.overdue_ratio_high}"}

    # ---------- 主流程 ----------
    def run(self, trigger: str = "schedule") -> Dict[str, Any]:
        inspection_id = "INS-" + uuid.uuid4().hex[:16]
        started = datetime.now()
        try:
            metrics = self.ds.metrics()
            verdict = evaluate(metrics, self.cfg)
            items: List[Dict[str, Any]] = [
                self._check_source(metrics),
                self._check_netflow(metrics),
                self._check_buffer(metrics),
                self._check_overdue(metrics),
            ]
            failed = [i for i in items if not i["passed"]]
            level = verdict["level"]
            finished = datetime.now()

            t = self.cfg.table
            self.db.insert(
                f"INSERT INTO `{t['inspection']}` "
                "(inspection_id, trigger_type, started_at, finished_at, level, "
                " item_count, failed_count, metrics_json, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
                (inspection_id, trigger, started, finished, level,
                 len(items), len(failed), json.dumps(metrics, ensure_ascii=False, default=str)),
            )
            for seq, it in enumerate(items, start=1):
                self.db.insert(
                    f"INSERT INTO `{t['inspection_item']}` "
                    "(inspection_id, seq, item_code, item_name, passed, level, detail, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())",
                    (inspection_id, seq, it["code"], it["name"],
                     1 if it["passed"] else 0, it["level"], it["detail"]),
                )

            alert = None
            if level in ("medium", "high"):
                title = f"[{'高危' if level == 'high' else '预警'}] 现金流巡检 {inspection_id}"
                body = self._render_alert(inspection_id, level, metrics, verdict, failed)
                alert = self.channel.send(title, body, {"inspection_id": inspection_id})
                self.db.insert(
                    f"INSERT INTO `{t['alert']}` "
                    "(alert_id, inspection_id, level, channel, status, sent, detail, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())",
                    ("ALT-" + uuid.uuid4().hex[:12], inspection_id, level,
                     alert.get("channel"), alert.get("status"),
                     1 if alert.get("sent") else 0, json.dumps(alert, ensure_ascii=False, default=str)[:2000]),
                )
                self.log.warning("巡检发现异常 level=%s failed=%s inspection_id=%s",
                                 level, len(failed), inspection_id)
            else:
                self.log.info("巡检通过 inspection_id=%s items=%s", inspection_id, len(items))

            return {
                "ok": True, "inspection_id": inspection_id, "level": level,
                "started_at": started.isoformat(timespec="seconds"),
                "finished_at": finished.isoformat(timespec="seconds"),
                "duration_ms": int((finished - started).total_seconds() * 1000),
                "item_count": len(items), "failed_count": len(failed),
                "items": items, "metrics": metrics, "verdict": verdict,
                "alert": alert,
            }
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.exception("巡检执行失败")
            raise AppError(ERR_JOB_FAILED, "巡检执行失败",
                           {"inspection_id": inspection_id, "reason": f"{type(exc).__name__}: {exc}"})

    # ---------- 渲染 ----------
    def _render_alert(self, inspection_id, level, metrics, verdict, failed) -> str:
        lines = [
            f"### 现金流巡检告警（{'高危' if level == 'high' else '预警'}）",
            "",
            f"- 巡检编号：`{inspection_id}`",
            f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 未通过项：{len(failed)} 项",
            "",
            "**判定依据**",
        ]
        for r in verdict.get("reasons", []):
            lines.append(f"- [{r['level']}] {r['text']}")
        lines.append("")
        lines.append("**关键指标**")
        for k in ("net_flow", "closing_balance", "buffer_ratio", "overdue_ratio", "receivable_count"):
            lines.append(f"- {k}：{metrics.get(k)}")
        if metrics.get("missing_sources"):
            lines.append("")
            lines.append("**数据源缺失**")
            for m in metrics["missing_sources"]:
                lines.append(f"- {m.get('source')}：{m.get('reason')}")
        return "\n".join(lines)

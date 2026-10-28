# -*- coding: utf-8 -*-
"""能力 3：周报 —— 汇总周期内数据，生成结构化周报并推送。

周期默认取最近 7 天（按巡检记录与源表数据），生成 Markdown 正文后：
  1) 写 agent_report 留痕；
  2) 调通道推送（未配置=dry-run，如实记录）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List

from .errors import AppError, ERR_JOB_FAILED


class Reporter:
    def __init__(self, db, cfg, datasource, inspector, channel, logger):
        self.db = db
        self.cfg = cfg
        self.ds = datasource
        self.inspector = inspector
        self.channel = channel
        self.log = logger

    def build(self, days: int = 7) -> Dict[str, Any]:
        end = datetime.now()
        start = end - timedelta(days=int(days))
        t = self.cfg.table

        # 周期内巡检统计
        inspections: List[Dict[str, Any]] = []
        try:
            inspections = self.db.query(
                f"SELECT level, COUNT(*) AS c FROM `{t['inspection']}` "
                "WHERE started_at BETWEEN %s AND %s GROUP BY level",
                (start, end),
            )
        except AppError as exc:
            self.log.warning("读取巡检统计失败：%s", exc.message)

        # 周期内告警
        alerts: List[Dict[str, Any]] = []
        try:
            alerts = self.db.query(
                f"SELECT level, status, COUNT(*) AS c FROM `{t['alert']}` "
                "WHERE created_at BETWEEN %s AND %s GROUP BY level, status",
                (start, end),
            )
        except AppError as exc:
            self.log.warning("读取告警统计失败：%s", exc.message)

        # 当前指标（取最新一次判定，不重跑业务计算）
        try:
            metrics = self.ds.metrics()
        except AppError:
            metrics = {}

        report = {
            "report_id": "RPT-" + uuid.uuid4().hex[:16],
            "period": {
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "days": int(days),
            },
            "inspections": {r["level"]: int(r["c"]) for r in inspections},
            "alerts": [
                {"level": r["level"], "status": r["status"], "count": int(r["c"])}
                for r in alerts
            ],
            "metrics": metrics,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        report["markdown"] = self._render(report)
        return report

    def _render(self, r: Dict[str, Any]) -> str:
        p = r["period"]
        insp = r.get("inspections") or {}
        lines = [
            "### 现金流风险周报",
            "",
            f"- 周期：{p['start'][:10]} ~ {p['end'][:10]}（{p['days']} 天）",
            f"- 生成时间：{r['generated_at']}",
            "",
            "**一、巡检概况**",
        ]
        if insp:
            for lv in ("high", "medium", "low"):
                if lv in insp:
                    lines.append(f"- {lv}：{insp[lv]} 次")
        else:
            lines.append("- 周期内无巡检记录")

        lines.append("")
        lines.append("**二、告警概况**")
        if r.get("alerts"):
            for a in r["alerts"]:
                lines.append(f"- {a['level']} / {a['status']}：{a['count']} 次")
        else:
            lines.append("- 周期内无告警")

        lines.append("")
        lines.append("**三、当前关键指标**")
        m = r.get("metrics") or {}
        if m:
            for k in ("net_flow", "closing_balance", "buffer_ratio",
                      "overdue_ratio", "receivable_count"):
                lines.append(f"- {k}：{m.get(k)}")
            if m.get("missing_sources"):
                lines.append("- 数据源缺失：" + "、".join(
                    f"{x.get('source')}" for x in m["missing_sources"]))
        else:
            lines.append("- 指标不可用（数据源缺失）")

        lines.append("")
        lines.append("> 本报告由常驻 agent 自动生成，数据来自 cf_workbench；"
                     "指标缺失时如实标注，不做补齐。")
        return "\n".join(lines)

    def run(self, days: int = 7, push: bool = True) -> Dict[str, Any]:
        try:
            report = self.build(days=days)
            t = self.cfg.table
            push_result = None
            if push:
                push_result = self.channel.send(
                    f"现金流风险周报 {report['period']['start'][:10]} ~ {report['period']['end'][:10]}",
                    report["markdown"],
                    {"report_id": report["report_id"]},
                )
            self.db.insert(
                f"INSERT INTO `{t['report']}` "
                "(report_id, period_start, period_end, content_json, markdown, "
                " channel, status, sent, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
                (report["report_id"], report["period"]["start"], report["period"]["end"],
                 json.dumps(report, ensure_ascii=False, default=str)[:60000],
                 report["markdown"],
                 (push_result or {}).get("channel", "none"),
                 (push_result or {}).get("status", "not_pushed"),
                 1 if (push_result or {}).get("sent") else 0),
            )
            report["push"] = push_result
            self.log.info("周报已生成 report_id=%s push=%s",
                          report["report_id"], (push_result or {}).get("status"))
            return report
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.exception("周报生成失败")
            raise AppError(ERR_JOB_FAILED, "周报生成失败",
                           {"reason": f"{type(exc).__name__}: {exc}"})

# -*- coding: utf-8 -*-
"""能力 1：数据读取与汇总。

事实边界：如果源表不存在或没有数据，返回 available=false 并说明原因，
绝不伪造数字、绝不用"上次的值"顶替。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .errors import AppError, ERR_NO_DATA


class DataSource:
    def __init__(self, db, cfg, logger):
        self.db = db
        self.cfg = cfg
        self.log = logger

    # ---------- 内部工具 ----------
    def _require_table(self, key: str) -> str:
        name = self.cfg.source_tables.get(key)
        if not name:
            raise AppError(ERR_NO_DATA, "未配置源表", {"source": key})
        if not self.db.table_exists(name):
            raise AppError(ERR_NO_DATA, "源表不存在",
                           {"source": key, "table": name,
                            "hint": "该表尚未在 cf_workbench 中创建或尚未导入数据"})
        return name

    # ---------- 能力 1：汇总 ----------
    def overview(self, limit: int = 20) -> Dict[str, Any]:
        """现金流汇总：取最近 N 条运行记录。"""
        table = self._require_table("cashflow")
        rows = self.db.query(
            f"SELECT * FROM `{table}` ORDER BY id DESC LIMIT %s", (int(limit),)
        )
        if not rows:
            raise AppError(ERR_NO_DATA, "源表无数据", {"table": table})

        latest = rows[0]
        return {
            "available": True,
            "table": table,
            "row_count": len(rows),
            "latest": {k: _safe(v) for k, v in latest.items()},
            "recent": [{k: _safe(v) for k, v in r.items()} for r in rows[:limit]],
        }

    def receivables(self, limit: int = 200) -> Dict[str, Any]:
        """应收三态汇总。"""
        table = self._require_table("receivable")
        rows = self.db.query(
            f"SELECT * FROM `{table}` ORDER BY id DESC LIMIT %s", (int(limit),)
        )
        if not rows:
            raise AppError(ERR_NO_DATA, "源表无数据", {"table": table})

        # 同一 object_ref 可能有多条历史（不可变追加模型），只取最新一条
        latest_by_ref: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            ref = r.get("object_ref")
            if ref not in latest_by_ref:
                latest_by_ref[ref] = r

        total = len(latest_by_ref)
        by_state: Dict[str, int] = {}
        amount_total = 0.0
        overdue_total = 0.0
        for r in latest_by_ref.values():
            st = str(r.get("state") or "UNKNOWN")
            by_state[st] = by_state.get(st, 0) + 1
            try:
                amount_total += float(r.get("receivable_amount") or 0)
            except (TypeError, ValueError):
                pass
            if (r.get("overdue_days") or 0) > 0:
                try:
                    overdue_total += float(r.get("receivable_amount") or 0)
                except (TypeError, ValueError):
                    pass

        return {
            "available": True,
            "table": table,
            "count": total,
            "by_state": by_state,
            "amount_total": round(amount_total, 2),
            "overdue_amount": round(overdue_total, 2),
            "overdue_ratio": round(overdue_total / amount_total, 4) if amount_total else 0.0,
        }

    def risk_latest(self, limit: int = 10) -> Dict[str, Any]:
        table = self.cfg.source_tables.get("risk")
        if not table or not self.db.table_exists(table):
            return {"available": False, "reason": "源表不存在", "table": table}
        rows = self.db.query(
            f"SELECT * FROM `{table}` ORDER BY id DESC LIMIT %s", (int(limit),)
        )
        return {
            "available": bool(rows),
            "table": table,
            "rows": [{k: _safe(v) for k, v in r.items()} for r in rows],
        }

    # ---------- 供巡检使用的轻量指标 ----------
    def metrics(self) -> Dict[str, Any]:
        """汇总出一组用于判定的指标；缺哪一项就标 None，不做补零。"""
        m: Dict[str, Any] = {
            "net_flow": None,
            "closing_balance": None,
            "buffer_ratio": None,
            "overdue_ratio": None,
            "receivable_count": None,
            "missing_sources": [],
        }
        try:
            ov = self.overview(limit=5)
            latest = ov.get("latest") or {}
            for key in ("net_flow", "净额", "net_amount"):
                if key in latest and latest[key] is not None:
                    m["net_flow"] = float(latest[key])
                    break
            for key in ("closing_balance", "期末", "closing"):
                if key in latest and latest[key] is not None:
                    m["closing_balance"] = float(latest[key])
                    break
            if m["closing_balance"]:
                outflow = None
                for key in ("outflow", "流出", "total_outflow"):
                    if key in latest and latest[key] is not None:
                        outflow = float(latest[key])
                        break
                if outflow:
                    m["buffer_ratio"] = round(m["closing_balance"] / outflow, 4)
        except AppError as exc:
            m["missing_sources"].append({"source": "cashflow", "reason": exc.message})

        try:
            rc = self.receivables()
            m["overdue_ratio"] = rc["overdue_ratio"]
            m["receivable_count"] = rc["count"]
        except AppError as exc:
            m["missing_sources"].append({"source": "receivable", "reason": exc.message})

        return m


def _safe(v: Any) -> Any:
    """把 Decimal / datetime / bytes 转成可 JSON 序列化的值。"""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return str(v)
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:  # noqa: BLE001
            return str(v)
    try:
        import decimal
        if isinstance(v, decimal.Decimal):
            return float(v)
    except Exception:  # noqa: BLE001
        pass
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)

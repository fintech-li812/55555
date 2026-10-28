# -*- coding: utf-8 -*-
"""调度器：周期性巡检 + 周报。

实现刻意只用标准库（不引 APScheduler）：低内存、少依赖、易排查。
状态持久化到 state_dir/state.json，避免重启后重复触发或漏触发。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict


class Scheduler(threading.Thread):
    """后台线程调度器。tick 每 tick_seconds 醒来一次，判断该不该跑。"""

    def __init__(self, cfg, logger, on_inspect: Callable[[], Any],
                 on_weekly: Callable[[], Any], tick_seconds: int = 20):
        super().__init__(name="cfagent-scheduler", daemon=True)
        self.cfg = cfg
        self.log = logger
        self.on_inspect = on_inspect
        self.on_weekly = on_weekly
        self.tick_seconds = max(5, int(tick_seconds))
        self._stop = threading.Event()
        self.state_path = os.path.join(cfg.state_dir, "state.json")
        self.state: Dict[str, Any] = self._load_state()

    # ---------- 状态 ----------
    def _load_state(self) -> Dict[str, Any]:
        try:
            if os.path.isfile(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("状态文件读取失败，按空状态启动：%s", exc)
        return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)  # 原子替换，避免写一半被 kill
        except Exception as exc:  # noqa: BLE001
            self.log.error("状态文件写入失败：%s", exc)

    # ---------- 触发判定 ----------
    def _due_inspect(self, now: datetime) -> bool:
        last = self.state.get("last_inspect_ts")
        interval = int(self.cfg.inspect_interval_minutes) * 60
        if last is None:
            return True
        return (now.timestamp() - float(last)) >= interval

    def _due_weekly(self, now: datetime) -> bool:
        # Python: Monday=0；配置为 1=周一 → 转换
        if now.isoweekday() != int(self.cfg.weekly_day):
            return False
        if (now.hour, now.minute) != (int(self.cfg.weekly_hour), int(self.cfg.weekly_minute)):
            return False
        # 同一天只推一次
        return self.state.get("last_weekly_date") != now.date().isoformat()

    # ---------- 主循环 ----------
    def run(self):
        self.log.info("调度器启动：巡检间隔 %s 分钟，周报 周%s %02d:%02d",
                      self.cfg.inspect_interval_minutes, self.cfg.weekly_day,
                      self.cfg.weekly_hour, self.cfg.weekly_minute)
        while not self._stop.is_set():
            try:
                now = datetime.now()
                if self._due_inspect(now):
                    self._safe_run("巡检", self.on_inspect)
                    self.state["last_inspect_ts"] = now.timestamp()
                    self.state["last_inspect_at"] = now.isoformat(timespec="seconds")
                    self._save_state()
                if self._due_weekly(now):
                    self._safe_run("周报", self.on_weekly)
                    self.state["last_weekly_date"] = now.date().isoformat()
                    self.state["last_weekly_at"] = now.isoformat(timespec="seconds")
                    self._save_state()
            except Exception as exc:  # noqa: BLE001
                # 调度器本身绝不能因为一次异常就退出
                self.log.exception("调度循环异常：%s", exc)
            self._stop.wait(self.tick_seconds)

    def _safe_run(self, label: str, fn: Callable[[], Any]):
        try:
            self.log.info("定时触发：%s", label)
            fn()
        except Exception as exc:  # noqa: BLE001
            self.log.error("定时任务失败（%s）：%s: %s", label, type(exc).__name__, exc)

    def stop(self):
        self._stop.set()

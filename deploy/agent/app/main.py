# -*- coding: utf-8 -*-
"""常驻 agent 服务入口。

启动顺序：加载配置 → 建日志 → 连数据库 → 装配能力 → 起调度器 → 起 HTTP。
任何一步失败都要留下明确日志并退出（交给 systemd 按策略重启），不允许"半死不活"。
"""
from __future__ import annotations

import os
import signal
import sys
import threading

from . import logging_setup
from .config import Config
from .db import Database
from .datasource import DataSource
from .channels import build as build_channel
from .inspector import Inspector
from .reporter import Reporter
from .decision_card import DecisionCardBuilder
from .chat import ChatEngine
from .scheduler import Scheduler
from .httpapi import ApiContext, make_server
from .errors import AppError

LOG_NAME = "cfagent"


def bootstrap() -> int:
    try:
        cfg = Config.load()
    except AppError as exc:
        # 配置阶段还没有日志目录保障，先打 stderr
        sys.stderr.write(f"[FATAL] 配置加载失败 code={exc.code}: {exc.message}\n")
        sys.stderr.write(f"        detail={exc.detail}\n")
        return 2

    log = logging_setup.setup(cfg.log_dir, cfg.log_level,
                              cfg.log_max_bytes, cfg.log_backup_count, LOG_NAME)
    log.info("=" * 60)
    log.info("agent 服务启动中")
    try:
        log.info("生效配置：%s", cfg.public_view())
    except Exception:  # noqa: BLE001
        pass

    try:
        db = Database(cfg, log)
        db.health()
        ds = DataSource(db, cfg, log)
        channel = build_channel(cfg, log)
        inspector = Inspector(db, cfg, ds, channel, log)
        reporter = Reporter(db, cfg, ds, inspector, channel, log)
        cards = DecisionCardBuilder(db, cfg, ds, log)
        chat = ChatEngine(db, cfg, ds, inspector, reporter, cards, log)
    except AppError as exc:
        log.error("组件装配失败 code=%s: %s detail=%s", exc.code, exc.message, exc.detail)
        return 3

    stop_event = threading.Event()

    def on_stop(reason: str):
        log.warning("收到停止信号：%s", reason)
        stop_event.set()

    ctx = ApiContext(cfg, log, db, ds, inspector, reporter, cards, chat, on_stop=on_stop)
    try:
        httpd = make_server(ctx)
    except OSError as exc:
        log.error("端口绑定失败 %s:%s：%s", cfg.host, cfg.port, exc)
        return 4

    sched = Scheduler(cfg, log,
                      on_inspect=lambda: inspector.run(trigger="schedule"),
                      on_weekly=lambda: reporter.run(days=7, push=True))
    sched.start()

    def _signal(signum, _frame):
        log.warning("收到信号 %s，开始优雅退出", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    log.info("HTTP 服务已监听 %s:%s（仅本机，公网请走 Nginx 反代）", cfg.host, cfg.port)
    log.info("agent 服务就绪 pid=%s", os.getpid())

    try:
        while not stop_event.is_set():
            httpd.handle_request()  # 单次循环，保证能响应停止信号
    except Exception:  # noqa: BLE001
        log.exception("HTTP 主循环异常")
    finally:
        log.info("正在停止调度器")
        sched.stop()
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        db.close()
        log.info("agent 服务已停止")

    return 0


def main():
    sys.exit(bootstrap())


if __name__ == "__main__":
    main()

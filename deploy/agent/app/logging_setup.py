# -*- coding: utf-8 -*-
"""日志：控制台 + 轮转文件。关键路径必须留痕，且不能把磁盘写满。"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys


_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup(log_dir: str, level: str = "INFO",
          max_bytes: int = 20 * 1024 * 1024, backup_count: int = 5,
          name: str = "cfagent") -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "agent.log"),
        maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    eh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "error.log"),
        maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fmt)
    logger.addHandler(eh)

    return logger


def get(name: str = "cfagent") -> logging.Logger:
    """取子日志器（沿用根配置）。"""
    return logging.getLogger(name) if name == "cfagent" else logging.getLogger("cfagent." + name)

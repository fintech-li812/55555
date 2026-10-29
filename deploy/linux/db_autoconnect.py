#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库连接自检 / 自愈程序（开机或会话打开时运行一次）。

设计要点
========
1. **凭证不硬编码**：只从环境变量读取；环境变量由 systemd 的
   ``EnvironmentFile=`` 注入，或从 ``--env-file`` 指定的配置文件读入。
   程序本身不含任何口令字面量。
2. **重复执行无副作用**：
   - 用 ``flock`` 独占锁保证同一时刻只有一个实例在连库，后来的实例直接返回 0，
     不会叠加进程、不会并发写日志。
   - 就绪标记用「临时文件 + rename」原子替换，杜绝半行写入。
   - ``--fresh-secs``：若就绪标记在指定秒数内已生成，直接返回 0 且不再重试。
     这条是给「每开一个终端就触发一次」的场景用的，避免每次登录都连一遍。
3. **失败重试并落日志**：指数退避重试，每次尝试的失败原因（MySQL 错误号）
   都写日志；日志与 stdout 都**永不打印口令**。
4. **退出码**：0 成功 / 2 配置缺失 / 3 重试耗尽仍未连通 / 4 被别的实例占用（视为成功）。

用法
====
    # systemd 调用（环境变量已由 EnvironmentFile 注入）
    db_autoconnect.py --attempts 30 --delay 3

    # 手工跑（自己读配置文件）
    sudo db_autoconnect.py --env-file /opt/cashflow/deploy/.env --once
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import time
from datetime import datetime

LOG_DIR = os.environ.get("CF_LOG_DIR", "/var/log/cashflow")
LOG_FILE = os.path.join(LOG_DIR, "db-autoconnect.log")
RUN_DIR = os.environ.get("CF_RUN_DIR", "/run/cashflow")
STAMP = os.path.join(RUN_DIR, "db-ready")
LOCK = os.path.join(RUN_DIR, "db-autoconnect.lock")

# 允许出现在候选配置文件里的键；口令只按名取，绝不回显
DB_KEYS = ("CF_DB_HOST", "CF_DB_PORT", "CF_DB_NAME", "CF_DB_USER",
           "CF_DB_PASSWORD")

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir(path: str, mode: int = 0o755) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class Log:
    """同时写文件与 stdout；stdout 便于 systemd journal 采集。"""

    def __init__(self, quiet: bool = False, level: str = "INFO"):
        self.quiet = quiet
        self.threshold = _LEVEL_ORDER.get(level.upper(), 20)

    def _emit(self, level: str, msg: str) -> None:
        if _LEVEL_ORDER[level] < self.threshold:
            return
        line = "%s [%s] %s" % (ts(), level, msg)
        try:
            ensure_dir(LOG_DIR)
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass                       # 日志写不了也不能拖垮主流程
        if not self.quiet:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def info(self, m): self._emit("INFO", m)
    def warn(self, m): self._emit("WARN", m)
    def error(self, m): self._emit("ERROR", m)
    def debug(self, m): self._emit("DEBUG", m)


def check_file_perms(path: str, log: Log) -> None:
    """配置文件若对其他用户可读则拒绝使用——凭证只让必要的人看见。"""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise RuntimeError("无法读取配置文件 %s：%s" % (path, exc))
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(
            "配置文件 %s 权限过宽（当前 %o，含组/其他可读）。"
            "请执行 chmod 600 %s 后重试。" % (path, st.st_mode & 0o777, path))


def parse_env_file(path: str) -> dict:
    """解析 KEY=VALUE。与 shell 的 source 不同，**不做任何转义展开**。"""
    out = {}
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def load_conf(args, log: Log) -> dict:
    file_map = {}
    if args.env_file:
        if not os.path.exists(args.env_file):
            raise SystemExit(2)
        check_file_perms(args.env_file, log)
        file_map = parse_env_file(args.env_file)
        log.debug("已从配置文件载入 %d 个键" % len(file_map))

    def pick(key, default=""):
        # 优先级：命令行显式 > 进程环境变量 > 配置文件 > 默认值
        for source in (args.set_map, os.environ, file_map):
            if key in source and str(source[key]) != "":
                return str(source[key])
        return default

    conf = {
        "host": pick("CF_DB_HOST", "127.0.0.1"),
        "port": pick("CF_DB_PORT", "3306"),
        "database": pick("CF_DB_NAME", "cfqa_workbench"),
        "user": pick("CF_DB_USER", "cf_app"),
        "password": pick("CF_DB_PASSWORD", ""),
    }
    try:
        conf["port_i"] = int(conf["port"])
    except ValueError:
        raise RuntimeError("CF_DB_PORT 不是整数：%r" % conf["port"])
    missing = [k for k, v in conf.items() if v in ("", None)]
    if missing:
        raise RuntimeError("缺少必要配置：%s" % ", ".join(sorted(missing)))
    return conf


def describe(conf: dict) -> str:
    """只描述连接目标，不含口令。"""
    return "%s@%s:%s/%s" % (conf["user"], conf["host"], conf["port_i"],
                            conf["database"])


def try_once(conf: dict, expect_tables, args):
    """做一次真实握手。成功返回信息字典，失败抛异常（携带 MySQL 错误号）。"""
    import pymysql
    started = time.time()
    conn = pymysql.connect(
        host=conf["host"], port=conf["port_i"], user=conf["user"],
        password=conf["password"], database=conf["database"],
        charset="utf8mb4", connect_timeout=args.connect_timeout,
        read_timeout=args.connect_timeout)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.execute("SELECT VERSION()")
        version = cur.fetchone()[0]
        cur.execute("SELECT @@port, @@socket")
        port, sock = cur.fetchone()
        # 只看缺不缺表，不做任何写操作：重复执行必须保持只读副作用
        missing = []
        if expect_tables:
            placeholders = ",".join(["%s"] * len(expect_tables))
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name IN (%s)"
                % ("%s", placeholders), [conf["database"]] + list(expect_tables))
            found = {r[0] for r in cur.fetchall()}
            missing = [t for t in expect_tables if t not in found]
        return {"version": version, "server_port": port, "socket": sock,
                "latency_ms": round((time.time() - started) * 1000, 1),
                "missing_tables": missing}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def write_stamp(conf: dict, info: dict) -> None:
    payload = {
        "time": ts(),
        "target": describe(conf),
        "server_version": info["version"],
        "server_port": info["server_port"],
        "socket": info["socket"],
        "latency_ms": info["latency_ms"],
        "missing_tables": info["missing_tables"],
    }
    ensure_dir(RUN_DIR, 0o755)
    tmp = STAMP + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STAMP)             # 原子替换


def stamp_age() -> float:
    try:
        return time.time() - os.stat(STAMP).st_mtime
    except OSError:
        return float("inf")


def main() -> int:
    ap = argparse.ArgumentParser(description="数据库连接自检 / 自愈")
    ap.add_argument("--env-file", default=os.environ.get("CF_DB_ENV_FILE", ""),
                    help="凭证配置文件；不指定则完全依赖环境变量")
    ap.add_argument("--attempts", type=int, default=30, help="最大尝试次数")
    ap.add_argument("--delay", type=float, default=3.0, help="首次重试等待秒数")
    ap.add_argument("--backoff", type=float, default=1.5, help="退避系数")
    ap.add_argument("--max-delay", type=float, default=15.0, help="单次等待上限")
    ap.add_argument("--max-total", type=float, default=150.0,
                    help="总耗时上限，超时即放弃")
    ap.add_argument("--connect-timeout", type=float, default=8.0,
                    help="单次握手的 connect/read 超时")
    ap.add_argument("--fresh-secs", type=float, default=0.0,
                    help="就绪标记在该秒数内视为新鲜，直接成功返回")
    ap.add_argument("--expect-tables", default="",
                    help="逗号分隔的表名，仅检查是否存在并在缺失时告警")
    ap.add_argument("--once", action="store_true", help="只试一次，不重试")
    ap.add_argument("--quiet", action="store_true", help="不向 stdout 输出")
    ap.add_argument("--log-level", default=os.environ.get("CF_LOG_LEVEL", "INFO"))
    ap.add_argument("--allow-loose-perms", action="store_true",
                    help="允许使用组/其他可读的配置文件（不推荐）")
    args, extra = ap.parse_known_args()

    args.set_map = {}
    for item in extra:
        if item.startswith("--") and "=" in item:
            k, v = item[2:].split("=", 1)
            args.set_map[k.replace("-", "_").upper()] = v

    log = Log(quiet=args.quiet, level=args.log_level)
    ensure_dir(LOG_DIR)
    ensure_dir(RUN_DIR, 0o755)

    # ---- 幂等：同一时刻只跑一个实例 ----
    lock_fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        log.info("已有实例正在执行（锁 %s 被占用），本次跳过。" % LOCK)
        return 4

    try:
        try:
            conf = load_conf(args, log)
        except Exception as exc:                       # noqa: BLE001
            log.error("配置载入失败：%s" % exc)
            return 2

        target = describe(conf)
        log.info("开始建立数据库连接：%s" % target)

        if args.fresh_secs > 0 and stamp_age() < args.fresh_secs:
            log.info("就绪标记 %.0fs 前刚生成，视为新鲜，跳过连接。" % stamp_age())
            return 0

        expect = [t for t in args.expect_tables.split(",") if t]
        attempts = 1 if args.once else max(1, args.attempts)
        deadline = time.time() + args.max_total
        wait = args.delay
        last = "未知原因"

        for nth in range(1, attempts + 1):
            try:
                info = try_once(conf, expect, args)
                write_stamp(conf, info)
                log.info("第 %d 次连接成功：MySQL %s，延迟 %.1fms，"
                         "就绪标记已写入 %s"
                         % (nth, info["version"], info["latency_ms"], STAMP))
                if info["missing_tables"]:
                    # 只读检查出来缺表，不自动建表——建库建表属管理员职责
                    log.warn("以下表不存在（应用写入会失败）：%s"
                             % ", ".join(info["missing_tables"]))
                return 0
            except Exception as exc:                   # noqa: BLE001
                code = getattr(exc, "args", [None])[0] if len(
                    getattr(exc, "args", [])) else None
                last = "%s: %s" % (type(exc).__name__,
                                   str(exc).replace(conf["password"], "***")
                                   if conf["password"] else str(exc))
                log.warn("第 %d/%d 次连接失败（%s）"
                         % (nth, attempts, last))
                if nth >= attempts or time.time() >= deadline:
                    break
                time.sleep(min(wait, args.max_delay,
                               max(0.0, deadline - time.time())))
                wait = min(wait * args.backoff, args.max_delay)

        log.error("连接失败：已达最大尝试次数 %d，最后原因：%s" % (attempts, last))
        return 3
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())

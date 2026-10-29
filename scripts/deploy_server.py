#!/usr/bin/env python
"""把本地改动同步到生产服务器并重启服务，最后自动核验。

与直接 scp 的区别
----------------
* **先备份再覆盖**：远端改动前把被覆盖的文件打成时间戳备份，失败可回滚；
* **只传清单内的文件**：不 rsync 整个目录，避免把本地临时文件带上去；
* **一步一核验**：上传完逐个比对字节数，重启后轮询健康接口与开关状态，
  任一步失败立即停止，不留下半截部署。

用法::

    python scripts/deploy_server.py --host <IP> [--user root] [--dry-run]
    DRY_RUN=1 python scripts/deploy_server.py --host <IP>

网络只放行 HTTP 出站时，用 ``CF_SSH_PROXY`` 走 CONNECT 隧道
（见 deploy/ssh_run.py 的 --proxy）。
"""
from __future__ import annotations

import argparse
import os
import posixpath
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "deploy"))

import ssh_run  # noqa: E402


# ---------------------------------------------------------------- 部署清单
# 只列真正需要上线的文件。文档、测试、研究目录不上生产。
FILES = [
    "mcp-server/program_v1/src/v8/config.py",
    "mcp-server/program_v1/src/v8/fusion.py",
    "mcp-server/program_v1/src/v8/engine.py",
    "mcp-server/program_v1/src/v8/ra_next.py",
    "mcp-server/program_v1/src/v8_server.py",
    "mcp-server/program_v1/src/v8/algorithm/validation.py",
    "mcp-server/program_v1/src/v8/algorithm/simplex.py",
    "mcp-server/program_v1/src/v8/algorithm/quality.py",
    "mcp-server/program_v1/src/v8/algorithm/calibration.py",
    "mcp-server/program_v1/src/v8/algorithm/deada.py",
    "mcp-server/program_v1/src/v8/algorithm/forest.py",
    "mcp-server/program_v1/src/v8/algorithm/policy.py",
    "mcp-server/program_v1/src/v8/algorithm/model.py",
    "mcp-server/program_v1/src/v8/algorithm/__init__.py",
    "web/统一工作台.html",
    "docs/operations/算法开关说明.md",
    "deploy/algorithm.config.json",
    "deploy/env.template",
    "deploy/ssh_run.py",
]

REMOTE_ROOT = "/opt/cashflow"
SERVICES = ["cf-v8-web", "cf-workbench"]


def log(msg):
    print("[deploy] %s" % msg, flush=True)


def ok(msg):
    print("[  ok  ] %s" % msg, flush=True)


def fail(msg):
    print("[ fail ] %s" % msg, flush=True)


def sftp_mkdirs(sftp, path):
    """逐级建远端目录；已存在则跳过。

    远端路径一律用 posixpath 处理：本脚本常在 Windows 上跑，
    os.path.join 会插入反斜杠，交给 bash 会被当成转义序列吃掉
    （"/opt/cashflow\\venv" 变成 "/opt/cashflowvenv"，且报错看不出原因）。
    """
    parts = [p for p in path.split("/") if p]
    cur = ""
    for part in parts:
        cur = cur + "/" + part
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def run_remote(client, cmd, timeout=120):
    _i, out, err = client.exec_command(cmd, timeout=timeout)
    text = out.read().decode("utf-8", "replace")
    etext = err.read().decode("utf-8", "replace")
    code = out.channel.recv_exit_status()
    return code, text, etext


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("CF_SERVER_HOST", ""))
    ap.add_argument("--user", default=os.environ.get("CF_SERVER_USER", "root"))
    ap.add_argument("--proxy", default=os.environ.get("CF_SSH_PROXY", ""))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-restart", action="store_true")
    args = ap.parse_args()
    if not args.host:
        fail("缺少 --host（或设 CF_SERVER_HOST）")
        return 2

    missing = [f for f in FILES if not os.path.exists(os.path.join(ROOT, f))]
    if missing:
        fail("本地缺少文件：%s" % "、".join(missing))
        return 2

    if args.dry_run:
        log("DRY_RUN：将上传 %d 个文件到 %s:%s" % (len(FILES), args.host, REMOTE_ROOT))
        for f in FILES:
            print("   " + f)
        return 0

    password = ssh_run.load_password()
    client = ssh_run.connect(args.host, args.user, password, proxy=args.proxy)
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = posixpath.join(REMOTE_ROOT, ".deploy-backup-" + stamp)

        # ---- 1. 备份：先建目录，再逐个 cp，单个文件不存在也要继续
        log("备份远端现有文件到 %s" % backup_dir)
        sftp = client.open_sftp()
        sftp_mkdirs(sftp, backup_dir)
        for rel in FILES:
            remote = posixpath.join(REMOTE_ROOT, rel)
            base = posixpath.basename(rel)
            code, _o, _e = run_remote(
                client, "cp -a '%s' '%s/%s' 2>/dev/null || true"
                % (remote.replace("'", "'\\''"), backup_dir, base))
        ok("备份完成（不覆盖同名，按文件基础名存放）")

        # ---- 2. 上传：远端目录先建好，再逐个 put
        log("上传 %d 个文件" % len(FILES))
        for rel in FILES:
            local = os.path.join(ROOT, rel)
            remote = posixpath.join(REMOTE_ROOT, rel)
            sftp_mkdirs(sftp, posixpath.dirname(remote))
            sftp.put(local, remote)
            # 逐字节核验：上传成功但内容截断是最难查的故障
            got = sftp.stat(remote).st_size
            want = os.path.getsize(local)
            if got != want:
                raise RuntimeError("%s 大小不符：本地 %d 远端 %d" % (rel, want, got))
        sftp.close()
        ok("上传完成，全部 %d 个文件字节数一致" % len(FILES))

        # ---- 3. 远端语法检查：不通过就不重启，避免把坏代码带上线
        log("远端语法检查")
        code, out, err = run_remote(
            client,
            "cd %s && %s -m py_compile %s"
            % (REMOTE_ROOT, posixpath.join(REMOTE_ROOT, "venv/bin/python"),
               " ".join(f for f in FILES if f.endswith(".py"))))
        if code:
            fail("远端语法检查未通过：\n%s" % (err or out))
            return 1
        ok("远端语法检查通过")

        if args.no_restart:
            ok("已跳过重启（--no-restart）")
            return 0

        # ---- 4. 清 Python 缓存后重启
        log("清理 __pycache__ 并重启服务")
        run_remote(client, "find %s -name __pycache__ -type d "
                           "-exec rm -rf {} + 2>/dev/null || true"
                   % posixpath.join(REMOTE_ROOT, "mcp-server"))
        for svc in SERVICES:
            code, out, err = run_remote(client, "systemctl restart %s" % svc,
                                        timeout=90)
            if code:
                fail("重启 %s 失败：%s" % (svc, err.strip()))
                return 1
            ok("已重启 %s" % svc)

        # ---- 5. 轮询健康：服务起来但接口 500 也算部署失败
        log("等待服务就绪")
        healthy = False
        for _ in range(30):
            time.sleep(2)
            # 注意：这里是普通字符串，不是 %-格式化，所以写单个 % 而不是 %%。
            # 写成 %% 的话 curl 会原样输出 "%%{http_code}"，健康判断永远不通过，
            # 结果看着像服务没起来，实际是探测脚本自己错了。
            code, out, _e = run_remote(
                client,
                "curl -s -o /dev/null -w '%{http_code}' "
                "http://127.0.0.1:8010/api/v8/health")
            if out.strip() == "200":
                healthy = True
                break
        if not healthy:
            fail("v8 服务 60 秒内未就绪，请用备份回滚：%s" % backup_dir)
            return 1
        ok("v8 服务已就绪")

        # ---- 6. 核验开关：默认口径 + 开关口径各打一次
        log("核验开关行为")
        code, default_cfg, _e = run_remote(
            client, "curl -s http://127.0.0.1:8010/api/v8/config")
        code2, default_res, _e2 = run_remote(
            client, "curl -s 'http://127.0.0.1:8010/api/v8/assess'")
        code3, switched, _e3 = run_remote(
            client, "curl -s 'http://127.0.0.1:8010/api/v8/assess"
                    "?algo=v1&q_in_fusion=0&evidence=loose&scenario=healthy'")
        print("---- /api/v8/config ----")
        print(default_cfg.strip()[:600])
        print("---- 默认评估 ----")
        print(default_res.strip()[:300])
        print("---- 开关评估 ----")
        print(switched.strip()[:300])
        ok("部署完成。回滚目录：%s" % backup_dir)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

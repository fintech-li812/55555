#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一体化工作台的一键启动器（跨平台）。

为什么另外写一个 Python 版本，而不是只用现成的 PowerShell 脚本：

1. .env 的值不能被转义。deploy/.env 里有 Windows 路径：
       CF_RESULT_FILE=D:\\CashflowRiskWorkbench\\...\\calculated_result_v1.json
   用 bash 的 ``set -a; . deploy/.env`` 导入时，\\C \\m \\f 会被当成转义序列，
   反斜杠逐个消失，路径拼成 D:CashflowRiskWorkbench...，
   结果是 /api/status 报 FileNotFoundError。Python 读文件不做转义，天然安全。

2. 环境变量只放进子进程，完全不碰当前 shell 的环境。

用法：
    python scripts/serve_local.py              # 常驻前台运行，Ctrl+C 优雅退出
    python scripts/serve_local.py --once       # 起完并完成体检后就返回（不常驻）
    python scripts/serve_local.py --daemon     # 后台常驻：脱离控制台，父进程退出后仍活着
    python scripts/serve_local.py --no-db      # 不拉拒绝痕库（仅 Windows 生效）
"""
import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "mcp-server" / "program_v1" / "src"
ENV_FILE = ROOT / "deploy" / ".env"
LOG_DIR = ROOT / "outbox" / "runtime"

MAIN_PORT = 8000
V8_PORT = 8010
DB_PORT = 3307

# 数据库与应用共用的键；缺哪个都不中断启动，只影响依赖存储的功能。
DB_KEYS = (
    "CF_DB_HOST", "CF_DB_PORT", "CF_DB_NAME", "CF_DB_USER", "CF_DB_PASSWORD",
    "CF_TENANT_ID", "CF_COMPANY_ID", "CF_RESULT_FILE", "CF_RESULT_SHA256",
    "CF_QA_MODE", "CF_ALLOW_FIXTURE", "CF_QWEN_BOUND",
)


def load_env(path):
    """逐行读 KEY=VALUE。**不**做任何转义展开，这是路径不塌掉的关键。"""
    env = {}
    if not path.exists():
        print("[warn] 未找到 %s：数据库连接不可用，计算与评估仍可运行。" % path)
        return env
    # utf-8-sig 兼容带 BOM 的 .env，否则第一个键名会带上不可见的 \\ufeff
    with open(str(path), encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def pick_python():
    """优先复用项目既有虚拟环境，保持一致的类库版本。"""
    for cand in (
        ROOT.parent / "AI_Command_Workspace" / "runtime" / "cashflow_v1"
                / "Scripts" / "python.exe",
        Path("D:/AI_Command_Workspace/runtime/cashflow_v1/Scripts/python.exe"),
    ):
        if cand.exists():
            return str(cand)
    return sys.executable


def port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_port(port, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.4)
    return False


def _detach_flags():
    """Windows 下让子进程脱离父控制台的关键 flags。

    不加这些 flags 时，子进程与启动它的 shell 共用同一个控制台组；
    一旦那个 shell 会话结束（关窗口、任务回收、Ctrl+C 扩散），系统会向
    整组发 CTRL_CLOSE_EVENT，后端就跟着死——表现为「上一轮明明起来了，
    这一轮再看端口是空的」。这也是双击 .cmd 入口必须走 detachment 的原因。
    """
    if os.name != "nt":
        return {}          # POSIX: setsid 语义不同，交给调用方用 nohup 即可
    return {"creationflags": subprocess.DETACHED_PROCESS
                             | subprocess.CREATE_NEW_PROCESS_GROUP}


def start(name, script, port, child_env, detach=False):
    if port_open(port):
        print("[ ok ] %s：端口 %d 已在监听，复用现有进程。" % (name, port))
        return None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = open(str(LOG_DIR / ("%s-%s-stdout.txt" % (stamp, name))), "w",
               encoding="utf-8")
    err = open(str(LOG_DIR / ("%s-%s-stderr.txt" % (stamp, name))), "w",
               encoding="utf-8")
    kw = {"cwd": str(SRC), "env": child_env, "stdout": out, "stderr": err}
    kw.update(_detach_flags() if detach else {})
    proc = subprocess.Popen([pick_python(), "-B", "-u", str(script)], **kw)
    if wait_port(port):
        print("[ ok ] %s：已启动 127.0.0.1:%d（日志 outbox/runtime/%s-%s-*）"
              % (name, port, stamp, name))
        return proc
    print("[fail] %s：端口 %d 在 %ds 内未开始监听，请查看 stderr 日志。"
          % (name, port, 60))
    return proc


def pid_file():
    return LOG_DIR / "serve_local.pid"


def save_pids(procs):
    """记住子进程 pid，供 --stop 精确回收，避免误杀别的 Python 进程。"""
    pids = [p.pid for p in procs if p]
    if not pids:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(str(pid_file()), "w", encoding="utf-8") as fh:
        fh.write("\n".join(str(p) for p in pids))


def stop_all():
    """按 pid 文件回收；顺带兜底释放端口占用（例如手动起的服务）。"""
    killed = []
    pf = pid_file()
    if pf.exists():
        raw = open(str(pf), encoding="utf-8").read().split()
        for s in raw:
            try:
                pid = int(s)
            except ValueError:
                continue
            try:
                os.kill(pid, 9)
                killed.append(pid)
            except OSError:
                pass
        try:
            pf.unlink()
        except OSError:
            pass
    # 兜底：pid 文件丢失/过期时，直接按端口收
    for port in (MAIN_PORT, V8_PORT):
        k = _kill_by_port(port)
        if k:
            killed.append("%d(端口%d)" % (k, port))
    if killed:
        print("[ ok ] 已停止：%s" % ", ".join(str(k) for k in killed))
    else:
        print("[ ok ] 没有正在运行的后端（端口 %d/%d 空闲）"
              % (MAIN_PORT, V8_PORT))
    return 0


def _kill_by_port(port):
    """找出监听该端口的 pid 并结束它。

    优先用 psutil，但项目虚拟环境里通常没装（并且在别人的机器上不能假设有），
    所以再给一条不依赖第三方库的落路：解析 netstat / ss 的输出。
    """
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        for c in psutil.net_connections(kind="inet"):
            if c.status == "LISTEN" and c.laddr and c.laddr.port == port:
                try:
                    psutil.Process(c.pid).kill()
                    return c.pid
                except Exception:
                    return 0
        return 0

    cmd = ["netstat", "-ano"] if os.name == "nt" else ["ss", "-lntpH"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        text = out.stdout or ""
    except Exception:
        return 0

    for line in text.splitlines():
        if os.name == "nt":
            # TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    12345
            parts = line.split()
            if len(parts) < 5 or parts[0].upper() != "TCP":
                continue
            if parts[3].upper() != "LISTENING":
                continue
            try:
                if int(parts[1].rsplit(":", 1)[1]) != port:
                    continue
                pid = int(parts[4])
            except (ValueError, IndexError):
                continue
        else:
            if not line.lower().startswith("listen"):
                continue
            if (":%d " % port) not in line and not line.rstrip().endswith(
                    ":%d" % port) and (":%d\t" % port) not in line:
                continue
            m = re.search(r"pid=(\d+)", line)
            if not m:
                continue
            pid = int(m.group(1))

        if pid and pid != os.getpid():
            try:
                os.kill(pid, 9)
                return pid
            except OSError:
                return 0
    return 0


def build_env(base, extra):
    env = dict(os.environ)
    for key in DB_KEYS:
        if base.get(key):
            env[key] = base[key]
    env.update(extra)
    return env


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="起完并完成体检后就返回，不常驻（配合后台任务管理使用）")
    ap.add_argument("--daemon", action="store_true",
                    help="后台常驻：子进程脱离控制台，父进程退出后继续运行")
    ap.add_argument("--stop", action="store_true", help="停止本机已启动的后端")
    ap.add_argument("--no-db", action="store_true", help="不拉起隔离 MySQL")
    args = ap.parse_args()

    for required in (SRC / "public_server.py", SRC / "v8_server.py"):
        if not required.exists():
            print("[fail] 缺少文件：%s" % required)
            return 2

    if args.stop:
        return stop_all()

    file_env = load_env(ENV_FILE)

    v8_env = build_env(file_env, {"CF_V8_PORT": str(V8_PORT)})
    main_env = build_env(file_env, {
        "CF_V8_PORT": str(V8_PORT),
        "WORKBENCH_HOST": "127.0.0.1",
        "WORKBENCH_PORT": str(MAIN_PORT),
        # 必须与浏览器地址栏一致，否则被 Host 白名单拒绝（403 host_not_allowed）
        "WORKBENCH_ALLOWED_HOSTS": "127.0.0.1:%d,localhost:%d"
                                   % (MAIN_PORT, MAIN_PORT),
        # 以模块方式运行时，让 config / service / program_* 等顶层模块可被导入
        "PYTHONPATH": str(SRC),
    })

    procs = []
    # V8 是上游，必须先起来，避免主站一启动就有请求打到还没监听的端口。
    procs.append(start("v8", SRC / "v8_server.py", V8_PORT, v8_env,
                       detach=args.daemon))
    procs.append(start("main", SRC / "public_server.py", MAIN_PORT, main_env,
                       detach=args.daemon))
    procs = [p for p in procs if p]
    save_pids(procs)

    url = "http://127.0.0.1:%d/unified" % MAIN_PORT
    if port_open(MAIN_PORT) and port_open(V8_PORT):
        print("")
        print("一体化工作台已联通：%s" % url)
        print("  · 主站 API ：http://127.0.0.1:%d/api/status" % MAIN_PORT)
        print("  · V8   API ：http://127.0.0.1:%d/v8/api/v8/industries"
              "（经主站代理到 %d）" % (MAIN_PORT, V8_PORT))
        if not port_open(DB_PORT):
            print("  · 留痕　　：数据库未监听 %d，保存/回查会如实不可用，"
                  "评估不受影响" % DB_PORT)
    else:
        print("[fail] 服务未全部就绪")

    if args.once or args.daemon:
        # 交给外部保管生命周期；这里只负责"带着正确环境启起来"。
        if args.daemon:
            print("  · 后台　　：进程已脱离控制台，退出本窗口不影响；"
                  "停止用 python scripts/serve_local.py --stop")
        return 0

    try:
        print("\n常驻运行中（Ctrl+C 结束两个后端）…")
        while True:
            time.sleep(1.0)
            for p in list(procs):
                if p.poll() is not None:
                    print("[warn] 子进程已退出（pid %d），启动器一并退出。" % p.pid)
                    return 1
    except KeyboardInterrupt:
        print("\n收到中断，正在停止后端…")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                pass
        print("已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

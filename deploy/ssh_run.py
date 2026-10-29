#!/usr/bin/env python
"""Run one command on the target server over SSH.

The password lives in deploy/.sshpw (git-ignored) so it never has to be
repeated on a command line. Only ever printed back to the operator is the
command's own stdout/stderr -- never the credential.

usage:
    python ssh_run.py "uname -a"
    python ssh_run.py --host 1.2.3.4 --user root "df -h"
    python ssh_run.py --upload local.txt /tmp/remote.txt
"""
from __future__ import annotations

import argparse
import base64
import getpass
import os
import socket
import sys

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
PW_FILE = os.path.join(HERE, ".sshpw")


def load_password() -> str:
    """Read the credential from .sshpw, or ask for it once and cache it."""
    if os.path.exists(PW_FILE):
        with open(PW_FILE, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
        if value:
            return value
    value = getpass.getpass("服务器口令（不回显）：")
    try:
        with open(PW_FILE, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.chmod(PW_FILE, 0o600)
    except OSError:
        pass
    return value


def _proxy_sock(host: str, port: int, proxy_url: str, timeout: int = 20):
    """经 HTTP 代理的 CONNECT 隧道建一条到 (host, port) 的裸 TCP 连接。

    为什么需要：不少办公/容器网络只放行 HTTP(S) 出站，22 端口直连不通，
    但代理支持 CONNECT 方法。paramiko 的 connect 接受 ``sock=`` 参数，
    把这条隧道交给它即可，无需改其它逻辑。
    """
    from urllib.parse import urlparse

    parts = urlparse(proxy_url if "//" in proxy_url else "http://" + proxy_url)
    proxy_host, proxy_port = parts.hostname, (parts.port or 80)
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        request = ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                   % (host, port, host, port)).encode("ascii")
        sock.sendall(request)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        status = buf.split(b"\r\n", 1)[0].decode("ascii", "replace")
        if " 200 " not in status:
            raise RuntimeError("代理 CONNECT 失败：%s" % status.strip())
    except Exception:
        sock.close()
        raise
    return sock


def connect(host: str, user: str, password: str, port: int = 22,
            proxy: str = "") -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    # A brand-new host is expected here; the point of this script is to reach it.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(username=user, password=password, port=port, timeout=20,
                  banner_timeout=20, auth_timeout=20,
                  allow_agent=False, look_for_keys=False)
    if proxy:
        # 走隧道时必须显式传 hostname，否则 paramiko 会以为 sock 已连好目标
        kwargs["sock"] = _proxy_sock(host, port, proxy)
    client.connect(hostname=host, **kwargs)
    return client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="")
    parser.add_argument("--host", default=os.environ.get("CF_SERVER_HOST", ""))
    parser.add_argument("--proxy", default=os.environ.get("CF_SSH_PROXY", ""),
                        help="HTTP 代理地址（如 http://127.0.0.1:8080）；"
                             "只放行 HTTP 出站的网络里用它走 CONNECT 隧道")
    parser.add_argument("--user", default=os.environ.get("CF_SERVER_USER", "root"))
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"), default=None)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    if not args.host:
        print("缺少 --host（或设 CF_SERVER_HOST）", file=sys.stderr)
        return 2

    client = connect(args.host, args.user, load_password(), args.port,
                     proxy=args.proxy)
    try:
        if args.upload:
            local, remote = args.upload
            try:
                sftp = client.open_sftp()
                sftp.put(local, remote)
                sftp.close()
                print("uploaded (sftp) {} -> {}".format(local, remote))
                return 0
            except Exception:
                # Some hosts disable the SFTP subsystem. Fall back to streaming
                # the bytes through stdin, which plain SSH exec always allows.
                pass
            with open(local, "rb") as handle:
                blob = base64.b64encode(handle.read()).decode("ascii")
            command = "base64 -d > '{}'".format(remote.replace("'", "'\\''"))
            _in, out, err = client.exec_command(command, timeout=args.timeout)
            _in.write(blob)
            _in.flush()
            _in.channel.shutdown_write()
            code = out.channel.recv_exit_status()
            stderr = err.read().decode("utf-8", "replace")
            if stderr:
                sys.stderr.write(stderr)
            if code:
                return code
            print("uploaded (stdin) {} -> {}  ({} bytes)".format(local, remote, len(blob)))
            return 0

        if not args.command:
            print("缺少要执行的命令", file=sys.stderr)
            return 2

        _in, out, err = client.exec_command(args.command, timeout=args.timeout)
        stdout = out.read().decode("utf-8", "replace")
        stderr = err.read().decode("utf-8", "replace")
        code = out.channel.recv_exit_status()
        if stdout:
            sys.stdout.write(stdout)
        if stderr:
            sys.stderr.write(stderr)
        if code:
            sys.stderr.write("[exit={}]\n".format(code))
        return code
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

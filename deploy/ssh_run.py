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


def connect(host: str, user: str, password: str, port: int = 22) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    # A brand-new host is expected here; the point of this script is to reach it.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        password=password,
        port=port,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="")
    parser.add_argument("--host", default=os.environ.get("CF_SERVER_HOST", ""))
    parser.add_argument("--user", default=os.environ.get("CF_SERVER_USER", "root"))
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"), default=None)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    if not args.host:
        print("缺少 --host（或设 CF_SERVER_HOST）", file=sys.stderr)
        return 2

    client = connect(args.host, args.user, load_password(), args.port)
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

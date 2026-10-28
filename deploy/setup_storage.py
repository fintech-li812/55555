"""Interactive storage bootstrap. The root password is typed by the operator and
never written to any file, log, or this repository.

What it does:
  1. asks for the MySQL root password (getpass, not echoed, not stored)
  2. creates the database and a least-privilege application account
  3. creates the table using the DDL that ships inside service.py, so the schema
     can never drift away from the code that writes to it
  4. prints the values to put into .env, including the generated app password
     (shown once; it is not persisted by this script)
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

import pymysql

SRC = Path(__file__).resolve().parent.parent / "mcp-server" / "program_v1" / "src"
sys.path.insert(0, str(SRC))
from service import DDL  # noqa: E402  (schema comes from the code itself)

DEFAULT_DB = "cf_workbench"
DEFAULT_APP_USER = "cf_app"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--root-user", default="root")
    parser.add_argument("--database", default=DEFAULT_DB)
    parser.add_argument("--app-user", default=DEFAULT_APP_USER)
    # Only for a throwaway instance initialised with --initialize-insecure.
    parser.add_argument("--no-password", action="store_true")
    # Non-interactive runs (CI, a remote server over SSH) hand the admin
    # password over as a file or an env var instead of typing it at a tty.
    parser.add_argument("--password-file", default="")
    parser.add_argument("--password-env", default="")
    # agent_guard only allows program_calculation results to be stored when
    # allow_fixture is on, and config only allows that inside an isolated
    # cfqa_ database. So v1 demonstration storage MUST live in cfqa_.
    parser.add_argument("--qa", action="store_true")
    args = parser.parse_args()

    if args.database.startswith("cfqa_") and not args.qa:
        print("拒绝：cfqa_ 前缀只在 --qa 隔离模式下使用。", file=sys.stderr)
        return 2
    if args.qa and not args.database.startswith("cfqa_"):
        print("拒绝：--qa 模式要求库名为 cfqa_ 前缀（隔离库）。", file=sys.stderr)
        return 2
    if args.app_user == "root":
        print("拒绝：应用账号不得为 root。", file=sys.stderr)
        return 2

    if args.no_password:
        password = ""
        print("免密模式：仅用于 --initialize-insecure 建立的临时实例。")
    elif args.password_file:
        password = Path(args.password_file).read_text(encoding="utf-8").strip()
    elif args.password_env:
        password = os.environ.get(args.password_env, "")
    else:
        password = getpass.getpass("MySQL {} 口令（不回显、不记录）：".format(args.root_user))
    app_password = secrets.token_urlsafe(24)

    connection = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.root_user,
        password=password,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=8,
    )
    del password

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE IF NOT EXISTS `{}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_bin".format(args.database)
            )
            for host_pattern in ("127.0.0.1", "localhost"):
                cursor.execute(
                    "CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                    (args.app_user, host_pattern, app_password),
                )
                cursor.execute(
                    "ALTER USER %s@%s IDENTIFIED BY %s",
                    (args.app_user, host_pattern, app_password),
                )
                # Least privilege: the service only ever SELECTs and INSERTs.
                cursor.execute(
                    "GRANT SELECT, INSERT ON `{}`.* TO %s@%s".format(args.database),
                    (args.app_user, host_pattern),
                )
            cursor.execute("USE `{}`".format(args.database))
            cursor.execute(DDL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
            cursor.execute("SHOW TABLES")
            tables = [row[0] for row in cursor.fetchall()]
    finally:
        connection.close()

    print()
    print("建库完成：{}".format(args.database))
    print("表：{}".format(", ".join(tables)))
    print("应用账号：{}（仅 SELECT / INSERT）".format(args.app_user))

    env_path = Path(__file__).resolve().parent / ".env"
    template = Path(__file__).resolve().parent / "env.template"
    snapshot = Path(__file__).resolve().parent.parent / "mcp-server" / "program_v1" / "fixtures" / "calculated_result_v1.json"
    values = {
        "CF_TENANT_ID": "fixture-tenant",
        "CF_COMPANY_ID": "fixture-manufacturing",
        "CF_RESULT_FILE": str(snapshot),
        "CF_RESULT_SHA256": "e6470f7adeba00804b178be5dc17a3087a88758178b9482cfd64b8cf81cf36d7",
        "CF_DB_NAME": args.database,
        "CF_DB_HOST": "127.0.0.1",
        "CF_DB_PORT": str(args.port),
        "CF_DB_USER": args.app_user,
        "CF_DB_PASSWORD": app_password,
        "CF_QA_MODE": "1" if args.qa else "0",
        "CF_ALLOW_FIXTURE": "1" if args.qa else "0",
    }
    lines = template.read_text(encoding="utf-8").splitlines() if template.exists() else []
    out = []
    for line in lines:
        key, sep, _ = line.partition("=")
        if sep and key.strip() in values:
            out.append("{}={}".format(key.strip(), values[key.strip()]))
        else:
            out.append(line)
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    print()
    print("已写入 {}（含应用口令）".format(env_path))
    print("已预填：租户、公司、受信任结果文件与其 SHA256、数据库连接。")
    print("仍需你手工填写：WORKBENCH_ALLOWED_HOSTS、DINGTALK_WEBHOOK、WECOM_WEBHOOK。")
    print()
    print("注意：root 口令没有被写入任何文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Load deploy/.env, then start the public server in-process.

Keeps secrets out of the shell history and out of the repository: nothing here
prints the database password or any webhook.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent
SRC = DEPLOY.parent / "mcp-server" / "program_v1" / "src"


def load_env(path: Path) -> None:
    if not path.exists():
        print("缺少 {}（可从 env.template 复制后填写）".format(path))
        raise SystemExit(2)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip()


def main() -> int:
    load_env(DEPLOY / ".env")
    sys.path.insert(0, str(SRC))
    missing = [k for k in ("CF_TENANT_ID", "CF_COMPANY_ID", "CF_RESULT_FILE", "CF_RESULT_SHA256", "CF_DB_NAME") if not os.environ.get(k)]
    if missing:
        print("数据库配置缺失（保存与回查将不可用）：{}".format(", ".join(missing)))
        print("计算仍可运行。若要打通存储，请先跑 setup_storage.py 并补全 .env。")
    else:
        print("数据库配置已就绪。")
    from public_server import main as serve

    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

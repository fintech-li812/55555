"""Stdio MCP entry for Qwen Work, with deploy/.env preloaded.

Qwen Work launches this process directly, so it will not see a shell-exported
environment. Loading .env here is what lets save/query reach the real database.
Amounts are still produced by the engine; Qwen only calls tools.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SRC = ROOT / "mcp-server" / "program_v1" / "src"


def load_env(path: Path) -> None:
    if not path.exists():
        sys.stderr.write("缺少 {}，将回退到隔离 QA 绑定（保存可能不可用）\n".format(path))
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip()


def main() -> None:
    load_env(DEPLOY / ".env")
    sys.path.insert(0, str(SRC))
    from app import build_mcp

    build_mcp().run(transport="stdio")


if __name__ == "__main__":
    main()

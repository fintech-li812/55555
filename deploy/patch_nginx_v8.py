#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 V8 风控工作台（127.0.0.1:8010）挂载到公网 /v8/ 下。

为什么是子路径而不是子域名
--------------------------
v8.workbench.example.com 没有 DNS 记录，加记录需要去域名控制台操作，
因此这里走子路径。V8 页面的 API 使用的是**相对**其自身 URL 的路径，
所以 /v8/ 下的 /v8/api/v8/assess 经 proxy_pass 重写后正好是上游期望的
/api/v8/assess —— 与直接访问 8010 时完全一致，两种入口都可用。

幂等：已挂载则直接退出，不重复插入。
安全：改前备份，改后 `nginx -t` 校验，校验失败自动回滚。
"""
import io
import shutil
import subprocess
import sys

PATH = "/etc/nginx/sites-enabled/workbench.example.com"
ANCHOR = "    location / {"

BLOCK = """    # V8 risk workbench (v8_server.py, 127.0.0.1:8010).
    # Mounted under /v8/ instead of a subdomain: there is no DNS record for
    # v8.workbench.example.com and adding one needs the domain console. The page calls
    # its API with paths relative to its own URL, so /v8/api/v8/assess is
    # rewritten to /api/v8/assess upstream -- identical to what the service
    # expects when reached directly on 8010.
    location = /v8 {
        return 301 /v8/;
    }

    location /v8/ {
        proxy_pass http://127.0.0.1:8010/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 10s;
        # The three-layer model runs a pure-Python simplex plus a random
        # forest; a cold assess takes several seconds.
        proxy_read_timeout 120s;
    }

"""


def main():
    src = io.open(PATH, encoding="utf-8").read()

    if "location /v8/" in src:
        print("SKIP: /v8/ 已挂载，无需重复操作")
        return 0
    if ANCHOR not in src:
        print("FAIL: 未找到锚点 %r，配置结构可能与预期不符" % ANCHOR)
        return 2

    # 备份必须落在 sites-enabled 之外。该目录被 nginx 通配符 include，
    # 在里面留任何实体文件都会被当成配置加载（.bak 同样会被加载），
    # 从而产生 duplicate listen 并使整站 nginx -t 失败。
    # sites-available 不在 include 范围内，是安全的存放位置。
    backup = "/etc/nginx/sites-available/workbench.example.com.bak-before-v8"
    shutil.copy2(PATH, backup)
    print("备份：%s" % backup)

    patched = src.replace(ANCHOR, BLOCK + ANCHOR, 1)
    with io.open(PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(patched)
    print("已写入新配置")

    rc = subprocess.call(["nginx", "-t"])
    if rc != 0:
        shutil.copy2(backup, PATH)
        print("FAIL: nginx -t 未通过，已回滚到备份")
        return 3

    rc = subprocess.call(["systemctl", "reload", "nginx"])
    if rc != 0:
        print("FAIL: reload 失败")
        return 4

    print("OK: nginx 校验通过并已重载")
    return 0


if __name__ == "__main__":
    sys.exit(main())

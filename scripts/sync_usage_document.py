#!/usr/bin/env python3
"""Overwrite the server usage document with the git-tracked source file.

部署同步入口：本地编辑 ``config/usage_document.md`` 并 push 后，服务器
``git pull`` 再运行本脚本，即可用仓库中的手册覆盖 ``data/users.db`` 里的
使用文档行。读缓存有 5 秒 TTL，无需重启服务。幂等：内容一致时不做任何
修改、不涨版本。注意：覆盖会丢弃服务器上管理员通过页面临时编辑的内容；
UI 上传的图片存于服务器 ``data/usage_document/assets/``，不随 git 分发。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.usage_document_store import (  # noqa: E402
    get_usage_document, sync_usage_document_from_source,
)


def main() -> int:
    current = get_usage_document()
    applied, document = sync_usage_document_from_source()
    if not applied:
        print(f"使用文档已是最新（version {document.version}），无需修改。")
        return 0
    print(f"已用仓库中的手册覆盖使用文档（version {current.version} -> {document.version}），"
          "约 5 秒内对运行中的服务生效，无需重启。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

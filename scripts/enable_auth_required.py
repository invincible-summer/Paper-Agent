#!/usr/bin/env python3
"""Re-enable account login (AUTH_REQUIRED) after it was turned off at runtime.

误关账号登录后的服务器端恢复入口：直接把 data/users.db 中的运行时设置行
``auth_required`` 置回 1。后端读缓存有 5 秒 TTL，无需重启即可生效。
幂等：已开启时不做任何修改。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.auth_settings_store import (  # noqa: E402
    get_auth_settings, update_auth_settings,
)


def main() -> int:
    current = get_auth_settings()
    if current.auth_required:
        print("账号登录已处于开启状态，无需修改。")
        return 0
    updated = update_auth_settings(
        {"auth_required": True},
        expected_version=current.version,
        updated_by="script:enable_auth_required",
    )
    print(f"已重新开启账号登录（version {current.version} -> {updated.version}）。"
          "约 5 秒内对运行中的服务生效，无需重启。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

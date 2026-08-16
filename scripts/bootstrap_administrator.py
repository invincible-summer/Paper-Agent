#!/usr/bin/env python3
"""Interactively create the deployment administrator without exposing secrets."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.user_store import UserStoreError, bootstrap_administrator  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="交互式初始化 Paper Agent 管理员（密码不会显示或写入日志）"
    )
    parser.add_argument("--username", default="administrator")
    parser.add_argument("--email", default="administrator@administrator")
    parser.add_argument("--display-name", default="系统管理员")
    args = parser.parse_args()

    password = getpass.getpass("请输入管理员初始密码（至少 8 位）：")
    confirmation = getpass.getpass("请再次输入管理员初始密码：")
    if password != confirmation:
        print("两次输入的密码不一致。", file=sys.stderr)
        return 2
    try:
        user, created = bootstrap_administrator(
            args.username, args.email, password, args.display_name
        )
    except UserStoreError as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1
    if created:
        print(f"管理员 {user['username']} 已创建。")
    else:
        print(f"管理员 {user['username']} 已存在；未修改其密码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

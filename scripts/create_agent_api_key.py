#!/usr/bin/env python3
"""Interactively issue a long-lived Agent API key for an administrator."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.user_store import (  # noqa: E402
    UserStoreError,
    authenticate,
    create_agent_api_key,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="交互式创建长期 Agent API Key（完整密钥只显示一次）"
    )
    parser.add_argument("--username", default="administrator")
    parser.add_argument("--name", default="清小搭生产接入")
    args = parser.parse_args()

    password = getpass.getpass("请输入管理员密码：")
    administrator = authenticate(args.username, password)
    if administrator is None:
        print("认证失败：用户名或密码错误。", file=sys.stderr)
        return 1
    if administrator.get("role") != "administrator":
        print("认证失败：该账号不是管理员。", file=sys.stderr)
        return 1
    try:
        _, raw_key = create_agent_api_key(args.name, administrator["id"])
    except UserStoreError as exc:
        print(f"创建失败：{exc}", file=sys.stderr)
        return 1

    print("Agent API Key 已创建。完整密钥只显示这一次，请立即安全保存：")
    print(raw_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Re-stamp history records owned by 'local' to a real account.

When AUTH_REQUIRED is enabled, pre-existing records (saved before multi-user,
with no user_id) are owned by the synthetic 'local' user and therefore vanish
from every account and every guest view. This script re-attributes them to
<username> so they reappear under that account.

Usage:
    python scripts/reown_local_history.py <username>

Idempotent: records already owned by a real account are left untouched.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from core.history_store import HISTORY_DIR  # noqa: E402


def _resolve_uid(username: str) -> str:
    db = ROOT / "data" / "users.db"
    if not db.exists():
        sys.exit(f"未找到 {db}（多用户账号库不存在；确认已开启 AUTH_REQUIRED 并注册过账号）")
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        sys.exit(f"未找到用户 {username!r}（users.db 中无此账号）")
    return row[0]


def main(username: str) -> None:
    target = _resolve_uid(username)
    count = 0
    skipped = 0
    for fp in sorted(HISTORY_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"  跳过（无法解析）：{fp.name}")
            continue
        owner = data.get("user_id") or "local"
        if owner != "local":
            skipped += 1
            continue
        data["user_id"] = target
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        count += 1
        print(f"  已归属 → {fp.name}")
    print(f"\n完成：{count} 条记录归属 {username}（{target}）；{skipped} 条已属其它账号，未改动。")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("用法: python scripts/reown_local_history.py <username>")
    main(sys.argv[1])

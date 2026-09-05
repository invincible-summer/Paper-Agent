"""单 worker Web 轮次锁：先获锁再读历史，防止阅读发布与聊天互相覆盖。"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import weakref

_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def session_lock(owner: str, filename: str) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), owner, Path(filename).name)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


@asynccontextmanager
async def locked_session(owner: str, filename: str):
    async with session_lock(owner, filename):
        yield

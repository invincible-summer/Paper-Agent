"""Global administrator-managed Markdown usage document storage.

The document is intentionally separate from chat history and API storage.  It
is a single public document, persisted in the web users database with an
optimistic version lock so two administrator tabs cannot silently overwrite
one another.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "users.db"
_MAX_CONTENT_CHARS = 500_000
_CACHE_TTL_SECONDS = 5.0

DEFAULT_CONTENT = """# 使用文档

欢迎使用 **Paper Agent**。这是一个面向文献检索、论文阅读和研究分析的智能体。

## 主要功能

- **论文检索**：从多个学术平台检索论文，并按相关性整理结果。
- **论文深读**：读取开放获取论文，提炼研究问题、方法、结论和局限。
- **论文谱系**：分析论文之间的引用、主题和方法演化关系。
- **文献综述**：围绕研究主题组织文献，生成结构化综述和研究方向建议。
- **多模态理解**：在可用时分析论文中的图表、公式和页面结构。
- **文件分析**：支持上传论文、文档和图片进行对话式分析。

## 使用建议

1. 先说明你的研究主题、问题或关键词。
2. 需要最新文献时，明确告诉智能体进行论文检索。
3. 指定论文后，可以继续要求深读、比较、综述或生成论文谱系。
4. 复杂任务可以拆成多个步骤，以获得更稳定的结果。

## Markdown 内容

本页面由管理员维护，支持标题、列表、表格、代码块、链接和图片等 Markdown 语法。

如需插入图片，可以在编辑框中使用：

```markdown
![图片说明](/api/v1/usage-document/assets/图片文件名.png)
```

请以实际上传后返回的图片 Markdown 为准。

## 注意事项

- 论文全文获取受论文平台开放获取政策和网络状况影响。
- 不同论文平台可能有访问频率限制。
- 请勿在对话中提交密码、API Key 或其他敏感信息。
"""


class UsageDocumentError(ValueError):
    """Expected validation or persistence error."""


class UsageDocumentVersionConflict(UsageDocumentError):
    """The document was changed after the editor loaded it."""


@dataclass(frozen=True)
class UsageDocument:
    title: str
    content: str
    version: int
    updated_by: str
    updated_at: float


_cache: tuple[str, float, UsageDocument] | None = None


def reset_cache() -> None:
    global _cache
    _cache = None


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS usage_document (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               title TEXT NOT NULL,
               content TEXT NOT NULL,
               version INTEGER NOT NULL,
               updated_by TEXT NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    conn.commit()
    return conn


def _row_to_document(row: tuple[object, ...]) -> UsageDocument:
    return UsageDocument(
        title=str(row[0] or "使用文档"),
        content=str(row[1] or ""),
        version=int(row[2]),
        updated_by=str(row[3] or "bootstrap"),
        updated_at=float(row[4] or 0.0),
    )


def _validate_content(content: str) -> str:
    if not isinstance(content, str):
        raise UsageDocumentError("文档内容必须是文本")
    if len(content) > _MAX_CONTENT_CHARS:
        raise UsageDocumentError(f"文档内容不能超过 {_MAX_CONTENT_CHARS:,} 个字符")
    return content


def get_usage_document() -> UsageDocument:
    global _cache
    now = time.time()
    if _cache and _cache[0] == str(_DB_PATH) and now - _cache[1] < _CACHE_TTL_SECONDS:
        return _cache[2]

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT title, content, version, updated_by, updated_at "
            "FROM usage_document WHERE id = 1"
        ).fetchone()
        if row is None:
            now = time.time()
            conn.execute(
                "INSERT INTO usage_document "
                "(id, title, content, version, updated_by, updated_at) "
                "VALUES (1, ?, ?, 1, 'bootstrap', ?)",
                ("使用文档", DEFAULT_CONTENT, now),
            )
            conn.commit()
            row = ("使用文档", DEFAULT_CONTENT, 1, "bootstrap", now)
    finally:
        conn.close()

    document = _row_to_document(row)
    _cache = (str(_DB_PATH), now, document)
    return document


def update_usage_document(content: str, *, expected_version: int, updated_by: str) -> UsageDocument:
    global _cache
    if expected_version < 1:
        raise UsageDocumentError("文档版本号无效")
    content = _validate_content(content)
    if not isinstance(updated_by, str) or not updated_by.strip():
        updated_by = "administrator"

    current = get_usage_document()
    now = time.time()
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE usage_document SET content = ?, version = version + 1, "
            "updated_by = ?, updated_at = ? WHERE id = 1 AND version = ?",
            (content, updated_by[:120], now, expected_version),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise UsageDocumentVersionConflict("使用文档已被其他管理员更新，请刷新后再保存")
        conn.commit()
        row = conn.execute(
            "SELECT title, content, version, updated_by, updated_at "
            "FROM usage_document WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    document = _row_to_document(row)
    _cache = (str(_DB_PATH), now, document)
    return document


def document_asdict(document: UsageDocument | None = None) -> dict:
    return asdict(document or get_usage_document())

"""PDF 原文证据与坐标；不调用 Docling/模型，不改变原文件。"""
from functools import lru_cache
import hashlib
from pathlib import Path
import re
import unicodedata


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).replace("\u00ad", ""))


@lru_cache(maxsize=32)
def _manifest(path: str, size: int, modified: int) -> dict:
    import fitz
    with fitz.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("此 PDF 需要密码，请先上传已解锁的副本")
        if not doc.page_count:
            raise ValueError("PDF 没有可阅读的页面")
        return {"fingerprint": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                "page_count": doc.page_count,
                "outline": [{"level": depth, "title": title, "page": page}
                            for depth, title, page in doc.get_toc()[:1000] if 1 <= page <= doc.page_count]}


def manifest(path: Path) -> dict:
    info = path.stat()
    return _manifest(str(path), info.st_size, info.st_mtime_ns)


def page_evidence(path: Path, page: int, quote: str = "", rects: list | None = None) -> dict:
    import fitz
    with fitz.open(path) as doc:
        if doc.needs_pass or not 1 <= page <= doc.page_count:
            raise ValueError("页面不可读或页码超出范围")
        pdf_page = doc[page - 1]
        text = pdf_page.get_text("text", sort=True)
        exact = normalize(quote)
        verified = bool(exact and exact in normalize(text))
        # search_for 返回的是未旋转 PDF 坐标；统一为未旋转可见页面的比例坐标。
        page_box = pdf_page.rect * pdf_page.derotation_matrix
        matches = pdf_page.search_for(quote) if verified else []
        canonical = []
        if matches:
            for r in matches[:100]:
                canonical.append([max(0, r.x0 / page_box.width), max(0, r.y0 / page_box.height),
                                  min(1, r.x1 / page_box.width), min(1, r.y1 / page_box.height)])
        # 同句多处出现时仅保留落在用户选区附近的匹配，不猜测另一处。
        if canonical and rects:
            selected = [r for r in canonical if any(
                abs((r[0] + r[2]) / 2 - (s[0] + s[2]) / 2) < .08
                and abs((r[1] + r[3]) / 2 - (s[1] + s[3]) / 2) < .04 for s in rects)]
            canonical = selected
        region = not quote and bool(rects)
        return {"page": page, "quote": quote, "rects": rects if region else canonical,
                "verified": verified, "precision": "region" if region else "text" if verified and canonical else "page",
                "context": text[:18000], "has_text": bool(text.strip())}

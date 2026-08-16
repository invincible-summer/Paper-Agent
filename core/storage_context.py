"""Channel-scoped storage paths.

The self-hosted web channel keeps its historical locations.  The OpenAI-
compatible channel receives a physically separate root so retention and disk
pressure automation can never traverse web data by accident.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class StoragePathError(ValueError):
    """Raised when a storage path or workspace id is unsafe."""


def _validate_workspace_id(value: str) -> str:
    if not isinstance(value, str) or not _WORKSPACE_RE.fullmatch(value):
        raise StoragePathError(
            "workspace_id must be 1-64 ASCII letters, digits, '_' or '-', "
            "and must start with a letter or digit"
        )
    return value


def _absolute(path: Path) -> Path:
    return path.expanduser().absolute()


def _assert_no_symlink(path: Path) -> None:
    """Reject existing symlinks in a path, including its root component."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise StoragePathError(f"symlink storage path is not allowed: {current}")


def _assert_contained(root: Path, candidate: Path) -> Path:
    root_abs = _absolute(root)
    candidate_abs = _absolute(candidate)
    _assert_no_symlink(root_abs)
    _assert_no_symlink(candidate_abs)
    try:
        candidate_abs.relative_to(root_abs)
    except ValueError as exc:
        raise StoragePathError("storage path escapes its root") from exc
    # resolve() also catches a symlink created between the lstat walk and here.
    try:
        candidate_abs.resolve(strict=False).relative_to(root_abs.resolve(strict=False))
    except ValueError as exc:
        raise StoragePathError("resolved storage path escapes its root") from exc
    return candidate_abs


@dataclass(frozen=True)
class StorageContext:
    channel: Literal["web", "openai_api"]
    workspace_id: str
    root_dir: Path
    state_db: Path
    metadata_db: Path
    chroma_dir: Path
    blob_dir: Path
    temp_dir: Path
    trace_dir: Path

    def __post_init__(self) -> None:
        _validate_workspace_id(self.workspace_id)
        if self.channel not in {"web", "openai_api"}:
            raise StoragePathError(f"unsupported storage channel: {self.channel}")
        root = _absolute(self.root_dir)
        object.__setattr__(self, "root_dir", root)
        for name in (
            "state_db", "metadata_db", "chroma_dir", "blob_dir", "temp_dir", "trace_dir"
        ):
            object.__setattr__(self, name, _assert_contained(root, getattr(self, name)))

    @classmethod
    def web(
        cls, workspace_id: str = "default", *, project_root: str | os.PathLike[str] | None = None
    ) -> "StorageContext":
        """Describe legacy web paths without creating or changing anything."""
        root = _absolute(Path(project_root) if project_root is not None else _PROJECT_ROOT)
        metadata = root / "data" / "metadata.db"
        return cls(
            channel="web",
            workspace_id=workspace_id,
            root_dir=root,
            state_db=metadata,  # Web has no separate state DB; preserve legacy metadata DB.
            metadata_db=metadata,
            chroma_dir=root / "data" / "chroma",
            blob_dir=root / "data" / "uploads",
            temp_dir=root / "data" / "tmp",
            trace_dir=root / "history_record" / "trace",
        )

    @classmethod
    def openai_api(
        cls, workspace_id: str = "default", *, root_dir: str | os.PathLike[str] | None = None
    ) -> "StorageContext":
        """Describe the isolated API root; call :meth:`ensure_layout` to create it."""
        configured = root_dir or os.getenv("OPENAI_API_STORAGE_ROOT")
        root = _absolute(Path(configured) if configured else _PROJECT_ROOT / "data" / "openai_api")
        return cls(
            channel="openai_api",
            workspace_id=workspace_id,
            root_dir=root,
            state_db=root / "state.db",
            metadata_db=root / "metadata.db",
            chroma_dir=root / "chroma",
            blob_dir=root / "blobs",
            temp_dir=root / "tmp",
            trace_dir=root / "traces",
        )

    def resolve_relative(self, relative_path: str | os.PathLike[str]) -> Path:
        """Resolve a stored relative path under this context, rejecting escapes."""
        relative = Path(relative_path)
        if relative.is_absolute():
            raise StoragePathError("absolute storage paths are not accepted")
        return _assert_contained(self.root_dir, self.root_dir / relative)

    def ensure_layout(self) -> None:
        """Create only the API layout with private Unix permissions.

        Web callers deliberately cannot use this helper, preventing this new layer
        from chmod-ing or creating any existing frontend storage.
        """
        if self.channel != "openai_api":
            raise StoragePathError("ensure_layout is restricted to openai_api storage")
        _assert_no_symlink(self.root_dir)
        for directory in (
            self.root_dir, self.chroma_dir, self.blob_dir, self.temp_dir, self.trace_dir
        ):
            _assert_contained(self.root_dir, directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    def secure_private_file(self, path: str | os.PathLike[str]) -> Path:
        """Validate an existing API-private file and force mode 0600."""
        if self.channel != "openai_api":
            raise StoragePathError("private-file permissions are API-only")
        candidate = _assert_contained(self.root_dir, Path(path))
        if not candidate.is_file():
            raise StoragePathError("private storage file does not exist")
        os.chmod(candidate, 0o600)
        return candidate

"""Managed blob storage on the local filesystem.

Paths are always relative to a managed root; an absolute path, a parent escape or
a symlink leaving the root is rejected. Files are immutable: content is written to
a temporary file in the same directory, flushed and fsynced, then published with
``os.replace`` so readers never observe a partial file.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredBlob:
    relative_path: str
    sha256: str
    size_bytes: int


class ManagedRootError(ValueError):
    """Raised when a path escapes the managed artifact root."""


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        if not relative_path or relative_path.strip() == "":
            raise ManagedRootError("a managed path must not be empty")
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ManagedRootError("absolute client paths are not accepted")
        if any(part == ".." for part in candidate.parts):
            raise ManagedRootError("parent traversal is not accepted")
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise ManagedRootError("the path escapes the managed root") from error
        return resolved

    async def put(self, relative_path: str, data: bytes) -> StoredBlob:
        target = self.resolve(relative_path)
        return await asyncio.to_thread(self._put_sync, target, relative_path, data)

    def _put_sync(self, target: Path, relative_path: str, data: bytes) -> StoredBlob:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".tmp-{os.getpid()}-{target.name}")
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            self._fsync_directory(target.parent)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        return StoredBlob(
            relative_path=relative_path, sha256=digest, size_bytes=len(data)
        )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Best-effort directory fsync so the rename is durable."""
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:  # pragma: no cover - platform dependent
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def get(self, relative_path: str) -> bytes:
        target = self.resolve(relative_path)
        return await asyncio.to_thread(target.read_bytes)

    async def exists(self, relative_path: str) -> bool:
        return await asyncio.to_thread(self.resolve(relative_path).is_file)

    async def delete(self, relative_path: str) -> None:
        target = self.resolve(relative_path)
        await asyncio.to_thread(lambda: target.unlink(missing_ok=True))

    async def digest(self, relative_path: str) -> tuple[str, int]:
        """Return ``(sha256, size)`` without loading the file into memory."""

        def _digest() -> tuple[str, int]:
            target = self.resolve(relative_path)
            hasher = hashlib.sha256()
            size = 0
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
                    size += len(chunk)
            return hasher.hexdigest(), size

        return await asyncio.to_thread(_digest)

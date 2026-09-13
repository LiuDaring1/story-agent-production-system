"""One-transaction SHA-256 cache for expensive finalization checks.

The cache is deliberately opt-in and process-local.  Every first lookup reads
the complete file.  A cached digest is reused only while the file identity,
size, mtime and ctime are unchanged; normal file replacement or in-place
mutation therefore invalidates the entry.  No value survives the surrounding
``hash_cache_scope``.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
from typing import Iterator


_CACHE: ContextVar[dict[tuple[str, tuple[int, ...]], str] | None] = ContextVar(
    "story_finalize_hash_cache", default=None
)


def _signature(stat: os.stat_result) -> tuple[int, ...]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def sha256_file(path: Path | str, *, chunk_size: int = 1024 * 1024) -> str:
    target = Path(path)
    before = target.stat()
    key = (str(target.resolve()), _signature(before))
    cache = _CACHE.get()
    if cache is not None and key in cache:
        return cache[key]
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if _signature(before) != _signature(after):
        raise RuntimeError(f"File changed while hashing: {target}")
    value = digest.hexdigest()
    if cache is not None:
        cache[key] = value
    return value


@contextmanager
def hash_cache_scope() -> Iterator[dict[tuple[str, tuple[int, ...]], str]]:
    cache: dict[tuple[str, tuple[int, ...]], str] = {}
    token = _CACHE.set(cache)
    try:
        yield cache
    finally:
        _CACHE.reset(token)

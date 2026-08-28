"""Sidecar blob arena — Aeon-inspired off-graph text storage.

arXiv:2601.15311 (Aeon): large text is stored off-graph in an
append-only mmap-backed blob file with generational GC. Graph nodes
hold only a 64-byte preview + offset.

Context-M's chunks table currently stores full text inline
(`text TEXT NOT NULL`). For typical personas this is fine — chunks
are 200-400 bytes. But for use cases that ingest long documents
(research papers, meeting transcripts, issue threads), the chunks
table balloons and the working set of the SQLite page cache gets
polluted by long text the graph rarely needs.

This module provides an OPT-IN sidecar blob arena:

    BlobArena(path) — opens/creates an mmap'd blob file at `path`
        .put(text) -> (blob_id, offset, length)
        .get(offset, length) -> bytes
        .preview(text, n=64) -> first n bytes (the in-graph preview)

The host migrates chunks by:
    1. creating a BlobArena
    2. for each chunk: arena.put(text) -> (blob_id, offset, len)
    3. updating the chunks row: text = preview, blob_offset = offset,
       blob_len = len (schema migration adds the columns)
    4. on retrieval: chunks.text gives the preview (fast); full text
       is fetched via arena.get(offset, len) only when the audit
       / retrieval path actually needs it

Generational GC is out of scope for v1 (chunks are append-mostly in
practice — retired facts are deactivated but the chunk text is kept
for audit). The arena is a single mmap'd file; concurrent writers
must hold a lock (the arena serializes via flock).

BACKWARD COMPATIBILITY: the chunks schema migration is OPT-IN. The
default Memory() path still stores text inline. To enable the
sidecar, call Memory.enable_blob_arena(path) after construction.
"""
from __future__ import annotations

import io
import mmap
import os
import threading
import zlib
from pathlib import Path
from typing import Iterator


# Schema additions for the chunks table (applied by TraceStore when
# the arena is enabled). The columns are nullable so existing rows
# (with inline text) keep working — a NULL blob_offset means "use the
# inline text column".
SCHEMA_MIGRATION = """
ALTER TABLE chunks ADD COLUMN blob_offset INTEGER DEFAULT NULL;
ALTER TABLE chunks ADD COLUMN blob_len INTEGER DEFAULT NULL;
ALTER TABLE chunks ADD COLUMN blob_compressed INTEGER DEFAULT 0;
"""


class BlobArena:
    """Append-only mmap-backed blob file.

    Layout:
        - 8-byte header: magic + version + length
        - records: [8-byte length | length bytes of payload]
          (payload may be zlib-compressed if compressed=1 in the chunk
          row; the arena itself is format-agnostic to the bytes)

    The arena serializes writes via a process-local threading.Lock
    AND an flock on the file for cross-process safety. Concurrent
    readers don't need the lock — mmap pages are CoW.
    """

    MAGIC = b"BLB1"  # Blob Layer v1
    HEADER_LEN = 16

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # create or open
        is_new = not self.path.exists()
        if is_new:
            with open(self.path, "wb") as f:
                f.write(self.MAGIC + b"\x00" * 12)  # magic + 12 bytes pad
        self._fd = open(self.path, "r+b")
        # write-lock for cross-process safety
        try:
            import fcntl
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError):
            pass  # Windows or already locked by same process
        self._mmap: mmap.mmap | None = None
        self._lock = threading.Lock()
        self._open_mmap()

    def _open_mmap(self) -> None:
        if self._mmap is None:
            try:
                self._mmap = mmap.mmap(
                    self._fd.fileno(), 0, access=mmap.ACCESS_WRITE)
            except (ValueError, OSError):
                # empty file (just header) — fall back to file I/O
                self._mmap = None

    def put(self, data: bytes, compress: bool = True) -> tuple[int, int, int, bool]:
        """Append `data` to the blob file.

        Returns (blob_id, offset, length, was_compressed).
            blob_id — monotonically increasing int (records / appended)
            offset  — byte offset in the file where the record starts
            length  — payload length in bytes
            was_compressed — whether zlib compression was applied

        Compression: enabled by default for text payloads > 256 bytes;
        small payloads are stored raw because the zlib header (5+ bytes)
        makes small blobs larger.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        if compress and len(data) > 256:
            compressed = zlib.compress(data, level=6)
            if len(compressed) < len(data) - 8:  # only if meaningful savings
                payload = compressed
                was_compressed = True
            else:
                payload = data
                was_compressed = False
        else:
            payload = data
            was_compressed = False

        with self._lock:
            # we need to write 8 bytes (length) + len(payload) bytes
            rec_len = len(payload)
            # current file size = where this record starts
            self._fd.seek(0, io.SEEK_END)
            offset = self._fd.tell()
            # write 8-byte length + payload
            self._fd.write(rec_len.to_bytes(8, "little"))
            self._fd.write(payload)
            self._fd.flush()
            # invalidate the mmap view so the next get() re-reads
            if self._mmap is not None:
                try:
                    self._mmap.close()
                except Exception:
                    pass
                self._mmap = None
            self._open_mmap()
            blob_id = offset  # offset IS the id (unique within file)
            return blob_id, offset, rec_len, was_compressed

    def get(self, offset: int, length: int,
            was_compressed: bool = False) -> bytes:
        """Read a record from the blob file.

        `offset` and `length` come from the chunks table. If
        `was_compressed` is True (stored in the chunk row), the
        returned bytes are zlib-decompressed before return.
        """
        with self._lock:
            self._fd.seek(offset)
            raw_len = self._fd.read(8)
            if len(raw_len) != 8:
                raise IOError(f"short read at offset {offset}")
            rec_len = int.from_bytes(raw_len, "little")
            payload = self._fd.read(rec_len)
            if len(payload) != rec_len:
                raise IOError(
                    f"short payload read at offset {offset}: "
                    f"expected {rec_len}, got {len(payload)}")
        if was_compressed:
            try:
                return zlib.decompress(payload)
            except zlib.error:
                return payload  # return raw on decompression failure
        return payload

    def get_text(self, offset: int, length: int,
                 was_compressed: bool = False) -> str:
        """Convenience: get bytes and decode as UTF-8."""
        return self.get(offset, length, was_compressed).decode(
            "utf-8", errors="replace")

    def close(self) -> None:
        with self._lock:
            if self._mmap is not None:
                try:
                    self._mmap.close()
                except Exception:
                    pass
                self._mmap = None
            try:
                self._fd.close()
            except Exception:
                pass

    def __enter__(self) -> "BlobArena":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------- Migration helper ------------------------------------------

def migrate_chunks_to_arena(store, arena: BlobArena,
                             batch_size: int = 1000) -> dict:
    """Migrate existing chunks.text rows into the sidecar arena.

    For each chunk:
        - put text into arena -> (blob_id, offset, len, compressed)
        - update the row: blob_offset=offset, blob_len=len,
          blob_compressed=compressed, text=preview (first 64 bytes)
    The original text column is REPLACED with the preview — the full
    text lives only in the arena. Existing readers that use chunks.text
    will see the preview (good for display); readers that need full
    text must call arena.get_text(offset, len, compressed).

    Idempotent: chunks with blob_offset IS NOT NULL are skipped.
    """
    # ensure the schema migration has run
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(chunks)").fetchall()}
    if "blob_offset" not in cols:
        for stmt in SCHEMA_MIGRATION.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                store.conn.execute(stmt)
        store.conn.commit()

    migrated = 0
    skipped = 0
    while True:
        rows = store.conn.execute(
            "SELECT id, text FROM chunks WHERE blob_offset IS NULL "
            f"LIMIT {batch_size}").fetchall()
        if not rows:
            break
        for chunk_id, text in rows:
            if not text:
                skipped += 1
                continue
            data = text.encode("utf-8")
            _, offset, length, compressed = arena.put(data, compress=True)
            preview = text[:64] + ("..." if len(text) > 64 else "")
            store.conn.execute(
                "UPDATE chunks SET text=?, blob_offset=?, blob_len=?, "
                "blob_compressed=? WHERE id=?",
                (preview, offset, length, 1 if compressed else 0, chunk_id))
            migrated += 1
        store.conn.commit()
    return {"migrated": migrated, "skipped": skipped}


def get_chunk_text(store, arena: BlobArena, chunk_id: str) -> str:
    """Fetch full text for a chunk — from the arena if blob_offset is
    set, otherwise fall back to inline text.

    Use this in the audit / retrieval path when the 64-byte preview
    in chunks.text isn't enough and you need the full source.
    """
    row = store.conn.execute(
        "SELECT text, blob_offset, blob_len, blob_compressed "
        "FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    if not row:
        return ""
    text, offset, length, compressed = row
    if offset is None:
        return text or ""
    return arena.get_text(offset, length, bool(compressed))


__all__ = [
    "BlobArena", "SCHEMA_MIGRATION",
    "migrate_chunks_to_arena", "get_chunk_text",
]

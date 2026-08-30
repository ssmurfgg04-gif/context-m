"""Hybrid Logical Clock — causality-consistent total order for federation.

Wall-clock milliseconds give rough ordering; a counter breaks same-ms ties
and absorbs clock skew via the receive rule (max of local and remote);
node_id makes the order total and deterministic on every replica.

String form ``wall.counter.node`` sorts lexicographically EXACTLY like the
tuple (wall is zero-padded), so CRDT stamps can be compared as plain
strings everywhere downstream.
"""

from __future__ import annotations

import threading


def _pad(wall_ms: int) -> str:
    return f"{wall_ms:019d}"


class HLC:
    """One clock per node. Thread-safe."""

    __slots__ = ("node_id", "_wall", "_count", "_lock")

    def __init__(self, node_id: str, wall_ms: int = 0, count: int = 0) -> None:
        self.node_id = node_id
        self._wall = wall_ms
        self._count = count
        self._lock = threading.Lock()

    # -- local event ------------------------------------------------------
    def tick(self, now_ms: int | None = None) -> str:
        import time
        with self._lock:
            now = int(time.time() * 1000) if now_ms is None else now_ms
            if now > self._wall:
                self._wall, self._count = now, 0
            else:
                self._count += 1
            return self._stamp()

    # -- remote event -----------------------------------------------------
    def receive(self, remote_stamp: str) -> str:
        wall, count, _node = parse_stamp(remote_stamp)
        import time
        with self._lock:
            now = int(time.time() * 1000)
            if now > self._wall and now > wall:
                self._wall, self._count = now, 0
            elif wall > self._wall:
                self._wall, self._count = wall, count + 1
            else:
                self._count = max(self._count, count) + 1
            return self._stamp()

    def now(self) -> str:
        return self.tick()

    def peek(self) -> str:
        with self._lock:
            return self._stamp()

    def _stamp(self) -> str:
        return f"{_pad(self._wall)}.{self._count:06d}.{self.node_id}"

    # -- persistence ------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            return {"node_id": self.node_id, "wall": self._wall,
                    "count": self._count}

    @classmethod
    def from_state(cls, d: dict) -> "HLC":
        return cls(d["node_id"], d["wall"], d["count"])


def parse_stamp(stamp: str) -> tuple[int, int, str]:
    wall_s, count_s, node = stamp.split(".", 2)
    return int(wall_s), int(count_s), node

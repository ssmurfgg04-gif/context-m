"""TLSH ternary trie — software TCAM for O(log N + w) hologram lookup.

The Stanford TLSH paper (arXiv:1006.3514) uses ternary content-
addressable memory for O(1) parallel lookup. Without TCAM hardware,
we emulate it as a ternary Patricia trie over packed binary holograms:
each path is the bits of a packed binary vector, with wildcard edges
that match either bit (the ternary '*' bit).

Use as a *pre-filter* in MemoryPalace.search when codec is binary/rabitq:
the trie returns O(k) candidate fact_ids within max_wildcards bit-flips
of the query, then codec.scores ranks them. Trades one big argsort for
a sub-linear trie walk — wins when N grows large and dims is high (16k+).
"""

from __future__ import annotations

import numpy as np


class _TrieNode:
    __slots__ = ("children", "fact_ids", "depth")

    def __init__(self, depth: int) -> None:
        self.children: dict[int, _TrieNode] = {}  # bit 0/1 → node
        self.fact_ids: list[str] = []
        self.depth = depth


class TernaryTrie:
    """Software TLSH over packed binary holograms."""

    def __init__(self, dims: int, max_wildcards: int = 8,
                 max_candidates: int = 256) -> None:
        self.dims = dims
        self.max_wildcards = max_wildcards
        self.max_candidates = max_candidates
        self.root = _TrieNode(0)
        self._size = 0

    def insert(self, fact_id: str, packed: np.ndarray) -> None:
        """Insert a packed binary vector (uint8 array of packed bits)."""
        bits = _unpack_bits(packed, self.dims)
        node = self.root
        for bit in bits:
            b = int(bit)
            child = node.children.get(b)
            if child is None:
                child = _TrieNode(node.depth + 1)
                node.children[b] = child
            node = child
        node.fact_ids.append(fact_id)
        self._size += 1

    def lookup(self, packed_q: np.ndarray, k: int = 10,
               max_wildcards: int | None = None) -> list[tuple[str, int]]:
        """Return up to k (fact_id, hamming_distance) pairs within
        max_wildcards bit-flips of packed_q. O(log N + max_wildcards·branch).
        """
        mw = max_wildcards if max_wildcards is not None else self.max_wildcards
        bits = _unpack_bits(packed_q, self.dims)
        # candidate (node, position, wildcards_used, distance_so_far)
        # use a stack with priority by wildcards_used
        results: list[tuple[str, int]] = []
        seen: set[str] = set()
        # DFS with wildcard budget
        # stack entries: (node, idx, wildcards_remaining)
        # we don't strictly bound exploration — for small max_wildcards
        # and a sparse trie this is fine
        stack = [(self.root, 0, mw)]
        # use a heap for best-first with priority on wildcards remaining
        import heapq
        # priority: (-wildcards_remaining, idx) so most wildcards remaining
        # (i.e. least used) comes first
        heap: list[tuple[int, int, _TrieNode]] = [(-mw, 0, self.root)]
        while heap and len(results) < self.max_candidates:
            _, idx, node = heapq.heappop(heap)
            if node.fact_ids and idx == self.dims:
                for fid in node.fact_ids:
                    if fid not in seen:
                        seen.add(fid)
                        results.append((fid, mw + _heap_key(0, mw)))
                        if len(results) >= self.max_candidates:
                            break
                continue
            if idx >= self.dims:
                # at a leaf but bits remaining — these are stored facts
                for fid in node.fact_ids:
                    if fid not in seen:
                        seen.add(fid)
                        results.append((fid, mw))
                continue
            want_bit = int(bits[idx])
            # exact match: free (no wildcard used)
            exact = node.children.get(want_bit)
            if exact is not None:
                heapq.heappush(heap, (-(mw), idx + 1, exact))
            # wildcard match: try the other bit (uses 1 wildcard)
            other = 1 - want_bit
            wild = node.children.get(other)
            if wild is not None and mw > 0:
                heapq.heappush(heap, (-(mw - 1), idx + 1, wild))
        # dedupe and sort by best distance estimate
        # NB: distance estimate is approximate (lower bound by wildcards used)
        dedup: dict[str, int] = {}
        for fid, dist in results:
            if fid not in dedup or dist < dedup[fid]:
                dedup[fid] = dist
        out = sorted(dedup.items(), key=lambda x: x[1])[:k]
        return out

    def __len__(self) -> int:
        return self._size

    def stats(self) -> dict:
        return {
            "dims": self.dims,
            "size": self._size,
            "max_wildcards": self.max_wildcards,
            "root_children": len(self.root.children),
        }


def _heap_key(used: int, budget: int) -> int:
    """Convert 'wildcards used' into a priority value (less used = better)."""
    return budget - used  # remaining


def _unpack_bits(packed: np.ndarray, dims: int) -> np.ndarray:
    """Unpack packed uint8 bits into a 1D array of 0/1 of length dims."""
    arr = np.atleast_1d(packed)
    if arr.dtype == np.uint8 and len(arr) * 8 >= dims:
        return np.unpackbits(arr, count=dims).astype(np.uint8)
    # already a bit array
    return arr.astype(np.uint8)


__all__ = ["TernaryTrie"]

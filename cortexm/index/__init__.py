"""Index backends for Context-M's memory palace.

The default backend is the page-clustered hierarchical tree (Quadrant)
shipped as a Rust wheel under ``rust/quadrant/``. This package adds
alternative backends — currently the NSG (Navigable Spreading-out Graph)
proximity-graph index, with a pure-numpy fallback when the Rust wheel is
not installed.

All backends expose the same surface:
    build(vectors: np.ndarray, ids: list[str] | None = None) -> None
    search(query: np.ndarray, k: int = 10) -> list[tuple[str, float]]
    stats() -> dict
"""

from __future__ import annotations

from cortexm.index.nsg import NsgBackend

__all__ = ["NsgBackend"]

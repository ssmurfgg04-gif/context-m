"""Alias module — the strategic plan ships the import name ``cortexm``.

    from cortexm import Memory        # identical to: from context_m import Memory
"""

from context_m import Memory, Config, __version__  # noqa: F401

__all__ = ["Memory", "Config", "__version__"]

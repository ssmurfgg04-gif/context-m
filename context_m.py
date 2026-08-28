"""Backward-compat shim. The canonical module is now ``cortexm``.

This file exists so existing scripts that did::

    from context_m import Memory

keep working after `pip install cortexm`. New code should use::

    from cortexm import Memory

The shim will be removed in a future major release; migrate at your
leisure. The shim imports lazily so it adds ~0ms to cold-start when
nobody uses the old name.
"""
from cortexm import Memory, Config, __version__

__all__ = ["Memory", "Config", "__version__"]

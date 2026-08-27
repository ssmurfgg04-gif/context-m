"""Exception hierarchy for Context-M."""

from __future__ import annotations


class ContextMError(Exception):
    """Base class."""


class StoreError(ContextMError):
    """Trace / SQLite layer failure."""


class ExtractionError(ContextMError):
    """Deterministic extraction pipeline failure."""


class SecurityError(ContextMError):
    """Provenance / hash verification failure."""


class VerificationError(SecurityError):
    """Hash or Merkle proof did not verify."""


class BranchError(ContextMError):
    """Memory-Git operation failure (unknown branch, dirty state...)."""


class MigrationError(ContextMError):
    """Import from a foreign memory system failed."""


class CodecError(ContextMError):
    """Vector codec failure (untrained PQ, corrupt record...)."""

"""Context-M — The Universal Neuro-Symbolic Memory Fabric.

Layer 1  Symbolic Trace   : bi-temporal fact graph with contradiction
                            resolution, temporal edges, Datalog-lite rules.
Layer 2  VSA Memory Palace: holographic reduced representations (HRR) with
                            INT8 / Binary-HRR / RaBitQ / PQ codecs, a
                            page-clustered tree index and a semantic
                            lookaside buffer (SLB).
Bridge  : μ=0 deterministic ingest (zero LLM calls), neuro-symbolic read
          path with cryptographic provenance on every retrieval.
Kernel  : plugin composability (Cordis-inspired). Mount verbatim,
          structured, security, or your own; new users get verbatim +
          structured by default.

Mem0-compatible surface:  ``from cortexm import Memory``
Plugin kernel:            ``from cortexm import Context, mount_default``
"""

from __future__ import annotations

__version__ = "0.5.0"

# μ=0 protocol counter: number of LLM invocations used by this process.
# The BEAM-honest protocol requires this to stay 0 during ingest & retrieval.
LLM_CALLS = 0


def _lazy_memory():
    from cortexm.api.memory import Memory

    return Memory


def __getattr__(name: str):
    if name == "Memory":
        return _lazy_memory()
    if name == "Config":
        from cortexm.config import Config

        return Config
    if name == "Pipeline":
        from cortexm.pipeline import Pipeline
        return Pipeline
    if name == "Context":
        from cortexm.kernel import Context
        return Context
    if name == "mount_default":
        return _mount_default
    if name == "LLM_CALLS":
        from cortexm import metrics

        return metrics.llm_calls()
    raise AttributeError(name)


def _mount_default(*, db_path: str = ":memory:",
                    config: "Config | None" = None,
                    embedder=None,
                    mount_verbatim: bool = True,
                    mount_structured: bool = True,
                    mount_security: bool = False) -> "Context":
    """One-liner: build a kernel with verbatim + structured (default).

    This is the recommended entry point for new users. It mounts:
      1. a "db" service (sqlite3.Connection)
      2. an "embedder" service (HashingEmbedder)
      3. a "memory" service (cortexm.api.memory.Memory)
      4. VerbatimPlugin (FTS5 + dense, MemPalace-style)
      5. StructuredPlugin (bi-temporal Trace + VSA Palace)
      6. (optional) SecurityPlugin (MINJA + MIND middleware)

    Plugins that need a service mount AFTER the service is registered.
    The kernel's mount order is the caller's responsibility; this
    helper does it correctly so users don't have to think about it.

    Usage::

        from cortexm import mount_default
        ctx = mount_default()
        v = ctx.inject("verbatim")["verbatim"]
        v.add(text="My dog's name is Charlie", user_id="alice",
              source_tx_id=1)
        hits = v.search(query="Charlie", user_id="alice", k=5)
    """
    import sqlite3
    from cortexm.kernel import Context
    from cortexm.api.memory import Memory
    from cortexm.config import Config
    from cortexm.text.embedder import HashingEmbedder
    from cortexm.plugins.verbatim import VerbatimPlugin
    from cortexm.plugins.structured import StructuredPlugin

    if config is None:
        config = Config.from_env()
        if db_path != ":memory:":
            config.db_path = db_path
    elif db_path != ":memory:":
        config.db_path = db_path

    ctx = Context()

    # 1. Build the embedder first — both tiers share it
    if embedder is None:
        embedder = HashingEmbedder(
            dims=getattr(config, "embed_dim", 768),
            labse_enabled=getattr(config, "labse_enabled", False))
    ctx.service("embedder", embedder)

    # 2. Build the Memory — owns the TraceStore (sqlite3.Connection)
    mem = Memory(config)
    ctx.service("memory", mem)

    # 3. The verbatim tier needs the SAME sqlite3 connection as the
    #    structured tier so both live in the same .db file. Pull the
    #    connection from the Memory's store.
    db_conn = getattr(mem.store, "conn", None) or \
        getattr(mem.store, "_conn", None) or sqlite3.connect(db_path)
    ctx.service("db", db_conn)

    # 4. Mount plugins in dependency order. Verbatim + structured
    #    both depend on services, not on each other, so order is
    #    flexible. Mount verbatim first so its table-creation runs
    #    before the structured tier's queries (avoids a race when
    #    the same .db is used for both).
    #
    # dispose_memory=False (the default) means: the caller owns
    # the Memory + DB. dispose() does NOT close the SQLite
    # connection. This is correct for production — users want
    # their data to survive a kernel teardown / restart. Tests
    # that want a hermetic teardown pass dispose_memory=True
    # AND drop_tables_on_dispose=True explicitly.
    if mount_verbatim:
        ctx.mount(VerbatimPlugin())
    if mount_structured:
        ctx.mount(StructuredPlugin())
    if mount_security:
        from cortexm.plugins.security import SecurityPlugin
        ctx.mount(SecurityPlugin())

    return ctx


__all__ = [
    "Memory",
    "Config",
    "Pipeline",
    "Context",
    "mount_default",
    "LLM_CALLS",
    "__version__",
]

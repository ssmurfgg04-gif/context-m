"""Regression guard: MCP query_extract must not fall back to standard search.

Covers the refactor drift where QueryTimeExtractor.__init__ requires a
keyword-only `writer` (MemoryWriter) but MCPServer._query_extract built
it without one — every call raised TypeError and silently fell back.
"""
from __future__ import annotations

from cortexm.api.memory import Memory
from cortexm.mcp.server import MCPServer


def test_query_extract_no_fallback() -> None:
    mem = Memory(db_path=":memory:")
    mem.add([{"role": "user", "content": "Alice works at Acme Corp."}],
            user_id="probe")
    srv = MCPServer(mem)
    out = srv._query_extract("where does Alice work?", user_id="probe",
                             k=3)
    assert out.get("fallback") is None, f"fell back: {out.get('error')}"
    assert out.get("path") == "query_time_pattern"
    assert out.get("extracted_count", 0) > 0

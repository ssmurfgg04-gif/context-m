"""Tests for the v2 SPARQL+REST integration.

Covers:
  - SPARQL parser v2: DISTINCT, LIMIT, OFFSET, ORDER BY, OPTIONAL,
    FILTER regex/equals/ne, edge:CAUSAL typed-edge predicates,
    multi-pattern JOINs with binding propagation
  - SPARQL executor: blob-arena object resolution, edge_triples pool
  - REST API: /v1/sparql (auth'd inline endpoint), /v1/export
    (swappable decoder), /v1/consolidate (admin trigger),
    /v1/chaos (EAM chaos-ingest)
  - REST server: --sparql-port co-hosts one Memory instance across
    both endpoints; graceful SIGTERM shutdown
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cortexm.api.memory import Memory
from cortexm.config import Config
from cortexm.server.sparql import (
    parse_sparql, execute_sparql, edge_triples, SparqlServer,
    is_edge_predicate, _merge_filter_tokens,
)


# ----------------------------------------------------------- fixtures
@pytest.fixture
def mem(tmp_path):
    cfg = Config.from_env()
    cfg.db_path = str(tmp_path / "sparql_rest.db")
    m = Memory(cfg)
    # ingest some test facts
    m.add([{"role": "user", "content":
            "My name is Alice. I work at Google as a software engineer."}],
          user_id="alice")
    m.add([{"role": "user", "content":
            "My friend Bob is 30 years old. He lives in Seattle."}],
          user_id="alice")
    yield m
    m.close()


# ----------------------------------------------------------- parser v2
class TestParserV2:
    def test_distinct(self):
        p = parse_sparql("SELECT DISTINCT ?s WHERE { ?s ?p ?o }")
        assert p["distinct"] is True
        assert p["select"] == ["?s"]

    def test_limit(self):
        p = parse_sparql("SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 5")
        assert p["limit"] == 5

    def test_offset(self):
        p = parse_sparql("SELECT ?s ?p ?o WHERE { ?s ?p ?o } OFFSET 10 LIMIT 5")
        assert p["offset"] == 10
        assert p["limit"] == 5

    def test_order_by_asc(self):
        p = parse_sparql("SELECT ?s WHERE { ?s ?p ?o } ORDER BY ?s")
        assert p["order_by"] == {"var": "?s", "desc": False}

    def test_order_by_desc(self):
        p = parse_sparql("SELECT ?s WHERE { ?s ?p ?o } ORDER BY ?s DESC")
        assert p["order_by"] == {"var": "?s", "desc": True}

    def test_filter_regex(self):
        p = parse_sparql(
            'SELECT ?s ?o WHERE { ?s "name" ?o . '
            'FILTER regex(?o, "^A", "i") }')
        assert len(p["filters"]) == 1
        assert p["filters"][0]["type"] == "regex"
        assert p["filters"][0]["pattern"] == "^A"
        assert p["filters"][0]["flags"] == "i"

    def test_filter_equals(self):
        p = parse_sparql(
            'SELECT ?s WHERE { ?s "age" ?o . FILTER(?o = "30") }')
        assert p["filters"][0] == {"type": "equals",
                                    "var": "?o", "value": "30"}

    def test_filter_ne(self):
        p = parse_sparql(
            'SELECT ?s WHERE { ?s "name" ?o . FILTER(?o != "Bob") }')
        assert p["filters"][0] == {"type": "ne",
                                    "var": "?o", "value": "Bob"}

    def test_optional_block(self):
        p = parse_sparql(
            'SELECT ?s ?o WHERE { ?s "name" ?o . '
            'OPTIONAL { ?s "nick" ?o } }')
        assert len(p["optionals"]) == 1
        assert len(p["optionals"][0]) == 1

    def test_edge_causal_predicate(self):
        p = parse_sparql(
            "SELECT ?cause ?effect WHERE { "
            "?cause edge:CAUSAL ?effect } LIMIT 5")
        assert p["patterns"] == [("?cause", "edge:CAUSAL", "?effect")]
        assert p["limit"] == 5

    def test_multi_pattern_join(self):
        # JOIN: find subjects with both name AND age
        p = parse_sparql(
            'SELECT ?s ?n ?a WHERE { ?s "name" ?n . ?s "age" ?a }')
        assert len(p["patterns"]) == 2
        # second pattern reuses ?s — binding propagates
        assert p["patterns"][1][0] == "?s"

    def test_select_star(self):
        p = parse_sparql("SELECT * WHERE { ?s ?p ?o }")
        assert p["select"] == ["?s", "?p", "?o"]

    def test_compound_query(self):
        q = ('SELECT DISTINCT ?s ?o WHERE { '
             '?s "name" ?o . FILTER regex(?o, "^A") '
             '} ORDER BY ?s LIMIT 10')
        p = parse_sparql(q)
        assert p["distinct"] is True
        assert p["select"] == ["?s", "?o"]
        assert len(p["filters"]) == 1
        assert p["order_by"]["var"] == "?s"
        assert p["limit"] == 10

    def test_merge_filter_tokens(self):
        # tokenizer splits 'FILTER' and 'regex(...)' because of
        # whitespace — _merge_filter_tokens should re-join them
        toks = ["?s", "FILTER", 'regex(?o, "^A")']
        merged = _merge_filter_tokens(toks)
        assert len(merged) == 2
        assert merged[1] == 'FILTER regex(?o, "^A")'


# ----------------------------------------------------------- executor v2
class TestExecutorV2:
    def test_limit_truncates(self):
        facts = [("alice", "name", "Alice"),
                 ("alice", "age", "30"),
                 ("bob", "name", "Bob")]
        r = execute_sparql(
            "SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 2", facts)
        assert r["n_results"] == 2

    def test_distinct_dedupes(self):
        facts = [("alice", "name", "Alice"),
                 ("alice", "name", "Alice"),
                 ("alice", "name", "Alice")]
        r = execute_sparql(
            "SELECT DISTINCT ?s ?p ?o WHERE { ?s ?p ?o }", facts)
        assert r["n_results"] == 1

    def test_filter_regex(self):
        facts = [("alice", "name", "Alice"),
                 ("bob", "name", "Bob"),
                 ("carol", "name", "Carol")]
        r = execute_sparql(
            'SELECT ?s WHERE { ?s ?p ?o . FILTER regex(?o, "^A") }',
            facts)
        # ?s is bound, but ?o is the one being filtered. We expect
        # patterns to also include ?s ?p ?o so ?o binds. The filter
        # then keeps only rows where ?o starts with "A".
        names = [b.get("?s") for b in r["results"]["bindings"]]
        assert "alice" in names

    def test_filter_equals(self):
        facts = [("alice", "name", "Alice"),
                 ("bob", "name", "Bob")]
        r = execute_sparql(
            'SELECT ?s WHERE { ?s "name" ?o . FILTER(?o = "Alice") }',
            facts)
        assert r["n_results"] == 1
        assert r["results"]["bindings"][0]["?s"] == "alice"

    def test_filter_ne(self):
        facts = [("alice", "name", "Alice"),
                 ("bob", "name", "Bob")]
        r = execute_sparql(
            'SELECT ?s WHERE { ?s "name" ?o . FILTER(?o != "Alice") }',
            facts)
        assert r["n_results"] == 1
        assert r["results"]["bindings"][0]["?s"] == "bob"

    def test_multi_pattern_join(self):
        facts = [("alice", "name", "Alice"),
                 ("alice", "age", "30"),
                 ("bob", "name", "Bob"),
                 ("bob", "age", "25")]
        r = execute_sparql(
            'SELECT ?s ?n ?a WHERE { ?s "name" ?n . ?s "age" ?a }',
            facts)
        assert r["n_results"] == 2  # alice + bob both have name + age

    def test_order_by(self):
        facts = [("carol", "name", "Carol"),
                 ("alice", "name", "Alice"),
                 ("bob", "name", "Bob")]
        r = execute_sparql(
            "SELECT ?s WHERE { ?s ?p ?o } ORDER BY ?s", facts)
        ss = [b["?s"] for b in r["results"]["bindings"]]
        assert ss == sorted(["carol", "alice", "bob"])

    def test_order_by_desc(self):
        facts = [("carol", "name", "Carol"),
                 ("alice", "name", "Alice"),
                 ("bob", "name", "Bob")]
        r = execute_sparql(
            "SELECT ?s WHERE { ?s ?p ?o } ORDER BY ?s DESC", facts)
        ss = [b["?s"] for b in r["results"]["bindings"]]
        assert ss == sorted(["carol", "alice", "bob"], reverse=True)

    def test_edge_causal_predicate(self, mem):
        # write a CAUSAL edge: "I left Google" CAUSAL the older
        # works_at fact to retire. SPARQL can now walk that.
        mem.add([{"role": "user", "content":
                 "Update: I left Google yesterday to start a new job."}],
                user_id="alice")
        edges = edge_triples(mem)
        assert any(p == "edge:CAUSAL" for (_s, p, _d) in edges)
        # query the CAUSAL graph
        fact_objs = mem.store.query_facts(active=False)
        # include inactive facts so the retraction edge source is visible
        all_facts = [(f.subject, f.relation, f.value)
                     for f in mem.store.query_facts(active=None)]
        r = execute_sparql(
            "SELECT ?cause ?effect WHERE { ?cause edge:CAUSAL ?effect }",
            all_facts, edge_triples=edges)
        assert r["n_results"] >= 1

    def test_blob_resolver_called_for_source_text_var(self):
        # 4-tuple form: (s, p, o, source_id) — the executor stashes
        # source_id under __source_id. When the SELECT projects
        # ?source_text (or ?_source), the blob_resolver dereferences
        # the arena text via that source_id.
        facts = [("alice", "name", "Alice", "chunk_123")]
        called = {"n": 0}

        def resolver(_s, _r, source_id):
            called["n"] += 1
            assert source_id == "chunk_123"
            return "RESOLVED_TEXT"
        r = execute_sparql(
            "SELECT ?s ?source_text WHERE { ?s ?p ?o }", facts,
            blob_resolver=resolver)
        assert called["n"] >= 1
        assert r["results"]["bindings"][0]["?source_text"] == "RESOLVED_TEXT"

    def test_blob_resolver_not_called_without_resolver(self):
        # no resolver → ?source_text stays empty
        facts = [("alice", "name", "Alice", "chunk_123")]
        r = execute_sparql(
            "SELECT ?s ?source_text WHERE { ?s ?p ?o }", facts)
        assert r["results"]["bindings"][0].get("?source_text") in (
            "", None)


# ----------------------------------------------------------- REST API v2
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_rest(mem, *, sparql_port=None):
    """Start the REST server in a background thread; return (httpd, port)."""
    from cortexm.server.rest import serve
    port = _free_port()
    httpd = serve(mem, host="127.0.0.1", port=port,
                  sparql_port=sparql_port, sparql_host="127.0.0.1")
    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, t


def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _http_post(url, body, headers=None):
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


class TestRestSparqlEndpoint:
    def test_openapi_lists_sparql_chaos_consolidate_export(self):
        from cortexm.server.rest import openapi_spec
        spec = openapi_spec()
        paths = set(spec["paths"].keys())
        for p in ("/v1/sparql", "/v1/chaos",
                  "/v1/consolidate", "/v1/export"):
            assert p in paths, f"{p} missing from OpenAPI spec"

    def test_sparql_endpoint_no_auth_returns_401(self, mem):
        httpd, port, t = _start_rest(mem)
        try:
            url = (f"http://127.0.0.1:{port}/v1/sparql?query="
                   f"SELECT%20%3Fs%20%3Fp%20%3Fo%20WHERE%20%7B%20%3Fs%20%3Fp%20%3Fo%20%7D")
            with pytest.raises(urllib.error.HTTPError) as exc:
                _http_get(url)
            assert exc.value.code == 401
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_sparql_endpoint_with_reader_key(self, mem):
        httpd, port, t = _start_rest(mem)
        try:
            meta = mem.keys.create("reader", label="test-reader")
            url = (f"http://127.0.0.1:{port}/v1/sparql?query="
                   "SELECT%20%3Fs%20%3Fo%20WHERE%20%7B%20%3Fs%20%22name%22%20%3Fo%20%7D")
            status, body = _http_get(url, headers={
                "Authorization": f"Bearer {meta['key']}"})
            assert status == 200
            assert body["n_results"] >= 1
            # we ingested "Alice" as a name fact
            names = [b.get("?o") for b in body["results"]["bindings"]]
            assert "Alice" in names
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_sparql_endpoint_post_body(self, mem):
        httpd, port, t = _start_rest(mem)
        try:
            meta = mem.keys.create("reader", label="test-reader")
            status, body = _http_post(
                f"http://127.0.0.1:{port}/v1/sparql",
                {"query": "SELECT ?s ?p ?o WHERE { ?s ?p ?o }"},
                headers={"Authorization": f"Bearer {meta['key']}"})
            assert status == 200
            assert body["n_results"] >= 1
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_export_endpoint_rdf(self, mem):
        httpd, port, t = _start_rest(mem)
        try:
            meta = mem.keys.create("reader", label="test-reader")
            status, body = _http_get(
                f"http://127.0.0.1:{port}/v1/export?format=rdf",
                headers={"Authorization": f"Bearer {meta['key']}"})
            assert status == 200
            assert body["format"] == "rdf"
            # RDF triples look like `:subject :relation "value" .`
            assert "." in body["content"]
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_export_endpoint_json(self, mem):
        httpd, port, t = _start_rest(mem)
        try:
            meta = mem.keys.create("reader", label="test-reader")
            status, body = _http_get(
                f"http://127.0.0.1:{port}/v1/export?format=json",
                headers={"Authorization": f"Bearer {meta['key']}"})
            assert status == 200
            # JSON decoder returns parsed array directly
            assert isinstance(body, list)
            assert len(body) >= 1
            assert "subject" in body[0]
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_consolidate_endpoint_admin_only(self, mem):
        httpd, port, t = _start_rest(mem)
        try:
            # reader can't consolidate
            reader = mem.keys.create("reader", label="r")
            with pytest.raises(urllib.error.HTTPError) as exc:
                _http_post(
                    f"http://127.0.0.1:{port}/v1/consolidate",
                    {"lifecycle": True, "dreaming": False},
                    headers={"Authorization": f"Bearer {reader['key']}"})
            assert exc.value.code == 403

            admin = mem.keys.create("admin", label="a")
            status, body = _http_post(
                f"http://127.0.0.1:{port}/v1/consolidate",
                {"lifecycle": True, "dreaming": False, "dry_run": True},
                headers={"Authorization": f"Bearer {admin['key']}"})
            assert status == 200
            assert "lifecycle" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_chaos_endpoint(self, mem):
        httpd, port, t = _start_rest(mem)
        try:
            meta = mem.keys.create("operator", label="op")
            status, body = _http_post(
                f"http://127.0.0.1:{port}/v1/chaos",
                {"text": "btw my name is Charlie and I'm 40.",
                 "user_id": "charlie"},
                headers={"Authorization": f"Bearer {meta['key']}"})
            assert status == 200
            assert body["event"] == "CHAOS_INGEST"
            assert body["stats"]["llm_calls"] == 0
            assert body["stats"]["facts_inserted"] >= 1
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_co_hosted_sparql_port(self, mem):
        """--sparql-port N: standalone endpoint shares Memory."""
        sparql_port = _free_port()
        httpd, port, t = _start_rest(mem, sparql_port=sparql_port)
        try:
            # give the SPARQL thread a moment to bind
            time.sleep(0.2)
            url = (f"http://127.0.0.1:{sparql_port}/?query="
                   "SELECT%20%3Fs%20%3Fo%20WHERE%20%7B%20%3Fs%20%22name%22%20%3Fo%20%7D")
            status, body = _http_get(url)
            assert status == 200
            assert body["n_results"] >= 1
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_cors_preflight_returns_204(self, mem):
        """OPTIONS /v1/sparql returns 204 with permissive CORS headers
        so browser-based SPARQL clients (Apache Jena fetch, rdflib
        py-sparql-client) can do cross-origin preflight."""
        httpd, port, t = _start_rest(mem)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/sparql", method="OPTIONS")
            with urllib.request.urlopen(req, timeout=5) as r:
                assert r.status == 204
                acao = r.headers.get("Access-Control-Allow-Origin")
                acam = r.headers.get("Access-Control-Allow-Methods")
                assert acao == "*"
                assert "POST" in (acam or "")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_query_too_long_rejected(self, mem):
        """SPARQL endpoint rejects queries > MAX_QUERY_BYTES to prevent
        regex-parser DoS via pathological inputs."""
        httpd, port, t = _start_rest(mem)
        try:
            meta = mem.keys.create("reader", label="r")
            # build a 100KB query — way over the 64KB limit
            giant = "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1 " + " " * 100_000
            status, body = _http_post(
                f"http://127.0.0.1:{port}/v1/sparql",
                {"query": giant},
                headers={"Authorization": f"Bearer {meta['key']}"})
            # REST pattern: 200 + error dict — but the error message
            # should mention the size limit
            assert "error" in body
            assert "exceeds" in body["error"].lower() or "too" in body["error"].lower()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_federation_digest_endpoint(self, mem):
        """/v1/federation/digest returns a CRDT digest envelope."""
        httpd, port, t = _start_rest(mem)
        try:
            meta = mem.keys.create("operator", label="op")
            status, body = _http_get(
                f"http://127.0.0.1:{port}/v1/federation/digest",
                headers={"Authorization": f"Bearer {meta['key']}"})
            assert status == 200
            assert body.get("type") == "digest"
            assert "digest" in body
            assert "sig" in body  # HMAC-SHA256 signed
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_sparql_endpoint_source_text_via_arena(self, mem, tmp_path):
        """When blob_arena is enabled, SPARQL `?source_text` resolves
        the actual chunk text from the sidecar arena file."""
        # enable blob arena on the Memory instance
        arena_path = str(tmp_path / "arena.bin")
        mem.enable_blob_arena(arena_path)
        # ingest a fact — the source chunk gets stored in the arena
        mem.add([{"role": "user", "content":
                  "My name is Dave."}], user_id="dave")
        httpd, port, t = _start_rest(mem)
        try:
            meta = mem.keys.create("reader", label="r")
            url = (f"http://127.0.0.1:{port}/v1/sparql?query="
                   + urllib.parse.quote(
                       "SELECT ?s ?source_text WHERE { ?s ?p ?o } LIMIT 5"))
            status, body = _http_get(url, headers={
                "Authorization": f"Bearer {meta['key']}"})
            assert status == 200
            assert body["n_results"] >= 1
            # the source_text should be the actual chunk text —
            # something containing "Dave"
            found_dave = False
            for r in body["results"]["bindings"]:
                txt = r.get("?source_text", "") or ""
                if "Dave" in txt:
                    found_dave = True
                    break
            assert found_dave, (
                "source_text should resolve to chunk text containing 'Dave'"
                f" — got: {body['results']['bindings']}")
        finally:
            httpd.shutdown()
            httpd.server_close()


# ----------------------------------------------------------- standalone SPARQL auth
class TestStandaloneSparqlAuth:
    def test_loopback_no_auth_required(self, mem):
        """SparqlServer bound to 127.0.0.1 defaults to no-auth
        (local-dev convenience)."""
        port = _free_port()
        srv = SparqlServer(mem, host="127.0.0.1", port=port)
        assert srv.require_auth is False
        srv.start_background()
        try:
            time.sleep(0.2)
            url = (f"http://127.0.0.1:{port}/?query="
                   "SELECT%20%3Fs%20%3Fo%20WHERE%20%7B%20%3Fs%20%22name%22%20%3Fo%20%7D")
            status, body = _http_get(url)
            assert status == 200
        finally:
            srv.stop()

    def test_non_loopback_auto_enables_auth(self, mem):
        """SparqlServer bound to 0.0.0.0 auto-enables Bearer auth to
        prevent an unauthenticated fact dump on the LAN."""
        port = _free_port()
        srv = SparqlServer(mem, host="0.0.0.0", port=port)
        assert srv.require_auth is True

    def test_non_loopback_rejects_unauthenticated(self, mem):
        """If you bind SPARQL to 0.0.0.0 without a Bearer key, expect 401."""
        # bind to a non-loopback address by using the all-zeros address
        # (the test socket binds to it on Linux without sudo)
        import socket as _socket
        port = _free_port()
        srv = SparqlServer(mem, host="0.0.0.0", port=port)
        srv.start_background()
        try:
            time.sleep(0.2)
            # try connecting without auth — should get 401
            url = (f"http://127.0.0.1:{port}/?query="
                   "SELECT%20%3Fs%20%3Fo%20WHERE%20%7B%20%3Fs%20%22name%22%20%3Fo%20%7D")
            with pytest.raises(urllib.error.HTTPError) as exc:
                _http_get(url)
            assert exc.value.code == 401
            # now provide a valid key
            meta = mem.keys.create("reader", label="r")
            status, body = _http_get(url, headers={
                "Authorization": f"Bearer {meta['key']}"})
            assert status == 200
        finally:
            srv.stop()


# ----------------------------------------------------------- ThreadingHTTPServer
class TestSparqlServerThreaded:
    def test_server_runs_in_background_thread(self, mem):
        port = _free_port()
        srv = SparqlServer(mem, host="127.0.0.1", port=port)
        srv.start_background()
        try:
            time.sleep(0.2)
            url = (f"http://127.0.0.1:{port}/?query="
                   "SELECT%20%3Fs%20%3Fp%20%3Fo%20WHERE%20%7B%20%3Fs%20%3Fp%20%3Fo%20%7D%20LIMIT%202")
            status, body = _http_get(url)
            assert status == 200
            assert body["n_results"] <= 2
        finally:
            srv.stop()

    def test_concurrent_queries_dont_block(self, mem):
        """Fire 5 concurrent queries — they should all complete quickly
        (ThreadingHTTPServer handles each in its own thread)."""
        import threading
        port = _free_port()
        srv = SparqlServer(mem, host="127.0.0.1", port=port)
        srv.start_background()
        results = []
        try:
            time.sleep(0.2)
            def one():
                url = (f"http://127.0.0.1:{port}/?query="
                       "SELECT%20%3Fs%20%3Fp%20%3Fo%20WHERE%20%7B%20%3Fs%20%3Fp%20%3Fo%20%7D%20LIMIT%202")
                try:
                    s, _ = _http_get(url)
                    results.append(s)
                except Exception as e:
                    results.append(repr(e))
            ts = [threading.Thread(target=one) for _ in range(5)]
            t0 = time.time()
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=5)
            elapsed = time.time() - t0
            assert len(results) == 5
            assert all(r == 200 for r in results)
            assert elapsed < 2.0  # 5 concurrent shouldn't take >2s
        finally:
            srv.stop()


# ----------------------------------------------------------- per-endpoint rate limit (P2 #8)
class TestPerEndpointRateLimit:
    """Per-endpoint tiered rate limiter (P2 #8 from code review).

    SPARQL queries share one Memory with the REST surface but should
    have their own rate-limit bucket so a slow graph traversal cannot
    starve /healthz probes or /v1/search traffic.
    """

    def test_tier_classification(self):
        """_tier_for_path maps known routes to the right tier."""
        from cortexm.server.rest import _tier_for_path
        assert _tier_for_path("/healthz") == "fast"
        assert _tier_for_path("/readyz") == "fast"
        assert _tier_for_path("/metrics") == "fast"
        assert _tier_for_path("/openapi.json") == "fast"
        assert _tier_for_path("/") == "fast"
        # SPARQL is its own slow tier
        assert _tier_for_path("/v1/sparql") == "slow"
        assert _tier_for_path("/v1/sparql?query=SELECT") == "slow"
        # everything else falls through to medium
        assert _tier_for_path("/v1/search") == "medium"
        assert _tier_for_path("/v1/add") == "medium"
        assert _tier_for_path("/v1/memories/abc-123") == "medium"
        # unmapped routes inherit the safe default (medium, not fast)
        assert _tier_for_path("/v1/unknown") == "medium"

    def test_buckets_isolated_per_tier(self):
        """A key hitting the 'slow' tier does NOT consume 'medium' budget."""
        from cortexm.server.rest import TieredTokenBuckets
        tb = TieredTokenBuckets({
            "fast":   (200.0, 400),
            "medium": (50.0,  100),
            "slow":   (10.0,  20),
        })
        # the same key hitting two tiers must have two independent buckets
        # — drain 'slow' to zero and verify 'medium' is unaffected
        key = "ctxm_reader_abc"
        for _ in range(20):
            assert tb.allow("slow", key)  # burst=20, all OK
        # 'slow' is now empty
        assert tb.allow("slow", key) is False
        # 'medium' for the SAME key should still have its full budget
        for _ in range(100):
            assert tb.allow("medium", key), \
                "medium tier drained by slow-tier traffic — bug"
        # and 'fast' too
        for _ in range(400):
            assert tb.allow("fast", key), \
                "fast tier drained by slow-tier traffic — bug"

    def test_sparql_does_not_starve_healthz(self, mem):
        """End-to-end: hammer /v1/sparql until 429, /healthz still 200."""
        import threading
        from cortexm.server import rest as rest_mod
        from cortexm.server.rest import serve, FabricState, build_handler
        port = _free_port()
        # tiny SPARQL tier so we hit 429 quickly: 1 rps / burst 2
        old_tiers = rest_mod.RATE_LIMIT_TIERS
        rest_mod.RATE_LIMIT_TIERS = {
            "fast":   (200.0, 400),
            "medium": (50.0,  100),
            "slow":   (1.0,   2),    # 1 rps / burst 2 — tiny for the test
        }
        # construct FabricState AFTER patching RATE_LIMIT_TIERS so it
        # picks up the tiny SPARQL bucket
        from http.server import ThreadingHTTPServer
        state = FabricState(mem)
        httpd = ThreadingHTTPServer(("127.0.0.1", port),
                                     build_handler(state))
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            meta = mem.keys.create("reader", label="rl-test")
            key = meta["key"]
            time.sleep(0.2)
            # hammer /v1/sparql until we get a 429
            sparql_codes = []
            url = (f"http://127.0.0.1:{port}/v1/sparql?query="
                   "SELECT%20%3Fs%20%3Fp%20%3Fo%20WHERE%20%7B%20%3Fs%20%3Fp%20%3Fo%20%7D%20LIMIT%201")
            for _ in range(10):
                try:
                    s, _ = _http_get(url, headers={"Authorization":
                                                  f"Bearer {key}"})
                    sparql_codes.append(s)
                except urllib.error.HTTPError as e:
                    sparql_codes.append(e.code)
            # at least one 429 on SPARQL — burst is only 2
            assert 429 in sparql_codes, \
                f"SPARQL never throttled: {sparql_codes}"
            # /healthz (no auth) should still return 200 every time
            for _ in range(20):
                s, _ = _http_get(f"http://127.0.0.1:{port}/healthz")
                assert s == 200, f"healthz starved: {s}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            rest_mod.RATE_LIMIT_TIERS = old_tiers


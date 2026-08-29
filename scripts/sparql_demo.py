#!/usr/bin/env python3
"""Demo: SPARQL endpoint over Context-M facts.

Proves the retrieval path is decoder-agnostic — same palace + Trace
substrate that powers LLM context-stuffing now serves SPARQL.

Bootstraps a small fact corpus, starts the SPARQL endpoint, runs
several real queries against it, and prints the results. ZERO LLM
calls — fully non-LLM retrieval.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cortexm.api.memory import Memory
from cortexm.config import Config
from cortexm.server.sparql import SparqlServer


def bootstrap_corpus(mem) -> None:
    """Ingest a small set of facts so SPARQL has something to query."""
    # facts about a few fictional employees
    for msg, uid in (
        ("My name is Alice Smith. I work at Google. I live in San Francisco.",
         "alice"),
        ("My name is Bob Jones. I work at Microsoft. I live in Seattle.",
         "bob"),
        ("My name is Carol Davis. I work at Google. I live in New York.",
         "carol"),
    ):
        mem.add([{"role": "user", "content": msg}], user_id=uid)


def run_query(server_url: str, query: str) -> dict:
    q = urllib.parse.urlencode({"query": query})
    url = f"{server_url}/?{q}"
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    # Ensure UTF-8 output on Windows cp1252 consoles
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"\n[sparql-demo] === Context-M SPARQL Endpoint Demo ===")
    print(f"[sparql-demo] Non-LLM retrieval path: palace + Trace -> RDFDecoder")
    print(f"[sparql-demo]                  -> SPARQL parser -> JSON Results\n")

    # 1. Bootstrap a small corpus
    db = tempfile.mktemp(suffix=".db")
    if os.path.exists(db):
        os.unlink(db)
    cfg = Config.from_env()
    cfg.db_path = db
    mem = Memory(cfg)
    bootstrap_corpus(mem)
    print(f"[sparql-demo] ingested corpus:")
    for f in mem.store.query_facts(active=True):
        print(f"  {f.subject}  {f.relation}  {f.value}")

    # 2. Start SPARQL server in a background thread
    server = SparqlServer(mem, host="127.0.0.1", port=8919)
    server.start()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)

    base_url = "http://127.0.0.1:8919"
    try:
        # 3. Run several SPARQL queries
        queries = [
            ("SELECT ?s ?p ?o WHERE { ?s ?p ?o }",
             "all triples (no filter)"),
            ("SELECT ?s ?o WHERE { ?s ?p ?o . FILTER regex(?o, \"Google\", \"i\") }",
             "filter: who works at Google?"),
            ("SELECT ?s ?p ?o WHERE { ?s ?p ?o . FILTER(?p = \"name\") }",
             "filter: all facts where relation=name"),
            ("SELECT ?s ?o WHERE { ?s ?p ?o . FILTER regex(?s, \"alice\", \"i\") }",
             "filter: all facts about alice"),
        ]
        for q, desc in queries:
            print(f"\n[sparql-demo] --- {desc} ---")
            print(f"[sparql-demo] query: {q}")
            result = run_query(base_url, q)
            print(f"[sparql-demo] {result['n_results']} result(s):")
            for row in result["results"]["bindings"]:
                line = "  ".join(f"{k}={v}" for k, v in row.items())
                print(f"  {line}")

        # 4. Honest summary
        print(f"\n[sparql-demo] === HONEST SUMMARY ===")
        print(f"  - Server is running on http://127.0.0.1:8919/sparql")
        print(f"  - ALL retrieval happened with ZERO LLM calls")
        print(f"  - The same Memory() that powers LLM context-stuffing")
        print(f"    serves SPARQL via the RDFDecoder swap")
        print(f"  - The palace + Trace are decoder-agnostic — the NSR")
        print(f"    insight that the retrieval pipeline is reusable")
        print(f"    for non-LLM workloads is now demonstrably true.")
        print(f"  - The SPARQL parser is minimal — for production use a")
        print(f"    real triple store (Apache Jena) and feed it via the")
        print(f"    RDFDecoder export endpoint.")
    finally:
        server.stop()
        mem.close()
        if os.path.exists(db):
            os.unlink(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())

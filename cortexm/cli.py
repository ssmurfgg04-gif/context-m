"""cortexm — the Context-M command line.

    cortexm serve                    # MCP server (stdio JSON-RPC)
    cortexm stats [--db PATH]
    cortexm verify [--db PATH]       # integrity audit (hashes + vectors)
    cortexm consolidate [--db PATH]
    cortexm migrate --from mem0 --path mem0.db [--db PATH]
    cortexm cost --memories 1000000  # μ=0 cost calculator
    cortexm bench --buckets 128k,1m  # BEAM-style benchmark
    cortexm export-schema [--db PATH]
    cortexm git log|branches|diff|blame
"""

from __future__ import annotations

import argparse
import json
import sys


def _memory(args):
    from cortexm.api.memory import Memory
    from cortexm.config import Config
    cfg = Config.from_env()
    if getattr(args, "db", None):
        cfg.db_path = args.db
    return Memory(cfg)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cortexm",
                                 description="Context-M memory fabric CLI")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="run the MCP server on stdio")
    p.add_argument("--db", default=None)

    p = sub.add_parser("serve-rest", help="run the REST API server (HTTP)")
    p.add_argument("--db", default=":memory:")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8900)
    p.add_argument("--pii", default=None, choices=[None, "off", "redact",
                                                    "block", "tag"])
    p.add_argument("--admin-key", default=None,
                   help="mint an admin API key at boot (printed once)")
    p.add_argument("--sparql-port", type=int, default=None,
                   help="co-host a SPARQL endpoint on this port "
                        "(shares one Memory instance with the REST API)")
    p.add_argument("--sparql-host", default="0.0.0.0",
                   help="bind SPARQL endpoint to this host")
    p.add_argument("--sparql-user-id", default=None,
                   help="scope SPARQL queries to a single user")

    for name, help_ in (("stats", "memory statistics"),
                        ("verify", "integrity audit"),
                        ("consolidate", "lifecycle consolidation"),
                        ("export-schema", "federation schema report")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", default=None)

    # extend consolidate with toggles for each pass
    # (the loop above already created 'consolidate'; we fetch it
    # by name and add extra flags)
    con = [a for a in sub.choices.values() if a.prog.endswith("consolidate")][0]
    con.add_argument("--dry-run", action="store_true",
                     help="don't commit changes — just report what would happen")
    con.add_argument("--no-lifecycle", action="store_true",
                     help="skip the lifecycle pass (short→long promotion / decay)")
    con.add_argument("--no-dreaming", action="store_true",
                     help="skip the Aeon dreaming pass (merge triples, retire stale, "
                          "defrag palace, retrain prefetcher)")
    con.add_argument("--no-cognition", action="store_true",
                     help="skip the HMS cognition pass (PatternScanner + "
                          "AbstractionEngine + GapDetector + HypothesisEngine + "
                          "AnalogyDetector). Default ON — `cortexm consolidate` "
                          "fires cognition so HYPOTHESIZED_BY edges (confidence "
                          "< 0.5) are produced on every consolidate. Use this "
                          "flag to opt out for sensitive use cases.")
    con.add_argument("--user-id", default=None,
                     help="scope the dreaming pass to one user")

    p = sub.add_parser("keys", help="API key management (RBAC)")
    p.add_argument("op", choices=["create", "list", "revoke"])
    p.add_argument("--db", default=None)
    p.add_argument("--role", default="reader",
                   choices=["admin", "operator", "reader", "auditor"])
    p.add_argument("--label", default="")
    p.add_argument("--id", default=None, help="key id to revoke")

    p = sub.add_parser("audit", help="audit log tail / verify / export")
    p.add_argument("op", choices=["tail", "verify", "export"])
    p.add_argument("--db", default=None)
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--out", default="audit.jsonl",
                   help="export path (op=export)")

    p = sub.add_parser("snapshot", help="atomic backup with manifest")
    p.add_argument("--db", default=None)
    p.add_argument("--path", required=True)

    p = sub.add_parser("erase", help="GDPR right-to-erasure")
    p.add_argument("--db", default=None)
    p.add_argument("--user-id", required=True)

    p = sub.add_parser("governance", help="governance ops")
    p.add_argument("op", choices=["retention", "state-at"])
    p.add_argument("--db", default=None)
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--when", default=None,
                   help="ISO datetime for state-at")
    p.add_argument("--user-id", default=None)

    p = sub.add_parser("migrate", help="import from mem0/zep/chroma")
    p.add_argument("--from", dest="source", required=True,
                   choices=["mem0", "zep", "chroma"])
    p.add_argument("--path", required=True)
    p.add_argument("--db", default=None)
    p.add_argument("--user-id", default="migrated")

    p = sub.add_parser("cost", help="μ=0 cost calculator")
    p.add_argument("--memories", type=int, default=1_000_000)
    p.add_argument("--ingest-per-memory", type=float, default=0.001,
                   help="competitor LLM-extract cost per memory (USD)")

    p = sub.add_parser("bench", help="BEAM-style benchmark")
    p.add_argument("--buckets", default="128k")
    p.add_argument("--micro", action="store_true")

    p = sub.add_parser("git", help="Memory Git operations")
    p.add_argument("op", choices=["log", "branches", "diff", "blame"])
    p.add_argument("rest", nargs="*")
    p.add_argument("--db", default=None)

    # Reddit deep-dive (2026-08-29): "UI" / "dashboard" / "viewer" /
    # "inspect" appeared ≥10 times across r/LocalLLaMA + r/LangChain
    # + r/agi + r/ClaudeCode. Users want a way to inspect what's in
    # memory without writing code. `cortexm inspect` is the lean,
    # CLI-native answer: dump facts / chunks / recent audit events
    # for a (user_id, agent_id, run_id) scope as pretty JSON.
    p = sub.add_parser("inspect", help="inspect memory contents (facts, "
                                        "chunks, audit tail) for a scope")
    p.add_argument("--db", default=None)
    p.add_argument("--user-id", default="default",
                   help="scope to this user (default: 'default')")
    p.add_argument("--agent-id", default=None,
                   help="scope to this agent (default: all agents)")
    p.add_argument("--run-id", default=None,
                   help="scope to this run (default: all runs)")
    p.add_argument("--limit", type=int, default=50,
                   help="cap on facts / chunks returned (default 50)")
    p.add_argument("--format", choices=["json", "text"], default="json",
                   help="output format (default json)")
    p.add_argument("--what", choices=["facts", "chunks", "audit", "all"],
                   default="all", help="what to dump (default all)")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0

    if args.cmd == "serve":
        from cortexm.mcp.server import serve
        serve(args.db)
        return 0

    if args.cmd == "serve-rest":
        from cortexm.server.rest import main as rest_main
        # argparse passthrough for the REST server
        rest_argv = ["--db", args.db, "--host", args.host,
                     "--port", str(args.port)]
        if args.pii:
            rest_argv += ["--pii", args.pii]
        if args.admin_key:
            rest_argv += ["--admin-key", args.admin_key]
        if getattr(args, "sparql_port", None):
            rest_argv += ["--sparql-port", str(args.sparql_port),
                          "--sparql-host", args.sparql_host]
            if args.sparql_user_id:
                rest_argv += ["--sparql-user-id", args.sparql_user_id]
        import sys as _sys
        _sys.argv = ["contextm-serve"] + rest_argv
        rest_main()
        return 0

    if args.cmd == "keys":
        m = _memory(args)
        try:
            if args.op == "create":
                out = m.keys.create(args.role, label=args.label)
                print(json.dumps(out, indent=2))
            elif args.op == "list":
                print(json.dumps({"keys": m.keys.list_keys()}, indent=2))
            elif args.op == "revoke":
                print(json.dumps({"revoked": m.keys.revoke(args.id or "")}))
        finally:
            m.close()
        return 0

    if args.cmd == "audit":
        m = _memory(args)
        try:
            if args.op == "verify":
                print(json.dumps(m.audit_log.verify(), indent=2))
            elif args.op == "export":
                n = m.audit_log.export_jsonl(args.out)
                print(json.dumps({"exported": n, "path": args.out}))
            else:
                print(json.dumps({"events": m.audit_log.tail(args.n)}, indent=2))
        finally:
            m.close()
        return 0

    if args.cmd == "snapshot":
        m = _memory(args)
        try:
            print(json.dumps(m.governance.snapshot(args.path), indent=2))
        finally:
            m.close()
        return 0

    if args.cmd == "erase":
        m = _memory(args)
        try:
            print(json.dumps(m.governance.erase_user(args.user_id), indent=2))
        finally:
            m.close()
        return 0

    if args.cmd == "governance":
        m = _memory(args)
        try:
            if args.op == "retention":
                out = m.governance.apply_retention(args.days,
                                                    user_id=args.user_id)
            else:
                out = {"facts": m.governance.state_at(args.when or "",
                                                       user_id=args.user_id)}
            print(json.dumps(out, indent=2, default=str))
        finally:
            m.close()
        return 0

    if args.cmd == "cost":
        print(cost_report(args.memories, args.ingest_per_memory))
        return 0

    if args.cmd == "bench":
        from cortexm.bench.run import main as bench_main
        argv = ["--buckets", args.buckets]
        if args.micro:
            argv.append("--micro")
        bench_main()
        return 0

    if args.cmd == "inspect":
        return _inspect(args)

    m = _memory(args)
    try:
        if args.cmd == "stats":
            print(json.dumps(m.stats(), indent=2, default=str))
        elif args.cmd == "verify":
            print(json.dumps(m.verify_integrity(), indent=2, default=str))
        elif args.cmd == "consolidate":
            print(json.dumps(m.consolidate(
                dry_run=getattr(args, "dry_run", False),
                lifecycle=not getattr(args, "no_lifecycle", False),
                dreaming=not getattr(args, "no_dreaming", False),
                run_cognition=not getattr(args, "no_cognition", False),
                user_id=getattr(args, "user_id", None),
            ), indent=2, default=str))
        elif args.cmd == "export-schema":
            print(json.dumps(m.export_schema_report(), indent=2, default=str))
        elif args.cmd == "migrate":
            from cortexm.migrate.importers import MIGRATORS
            out = MIGRATORS[args.source](m, args.path, user_id=args.user_id)
            print(json.dumps(out, indent=2))
        elif args.cmd == "git":
            if args.op == "log":
                for c in m.log():
                    print(f"{c['id'][:8]} {c['ts']} {c['message']} "
                          f"({json.loads(c['parents'])})")
            elif args.op == "branches":
                for b in m.branches():
                    print(f"{b['name']}: {b['head'][:8]}")
            elif args.op == "diff":
                if len(args.rest) < 2:
                    print("usage: cortexm git diff <commitA> <commitB>")
                    return 2
                print(json.dumps(m.diff(args.rest[0], args.rest[1]),
                                 indent=2))
            elif args.op == "blame":
                if not args.rest:
                    print("usage: cortexm git blame <subject> [relation]")
                    return 2
                rel = args.rest[1] if len(args.rest) > 1 else None
                for row in m.blame(args.rest[0], rel):
                    print(f"{row['commit'][:8] if row['commit'] else '--------'} "
                          f"{row['recorded_at'][:10]} {row['fact']} "
                          f"{'[active]' if row['active'] else '[retired]'}")
        return 0
    finally:
        m.close()


def cost_report(memories: int, competitor_per_memory: float) -> str:
    """The μ=0 cost asymmetry (Section: MOAT 6)."""
    ours = memories * 0.00001      # deterministic CPU-only ingest
    theirs = memories * competitor_per_memory
    lines = [
        "Context-M μ=0 Cost Calculator",
        "============================",
        f"memories:                 {memories:,}",
        f"cortex-m ingest (CPU):    ${ours:,.2f}",
        f"LLM-in-loop ingest:       ${theirs:,.2f}",
        f"cost advantage:           {theirs / max(ours, 1e-9):,.0f}x",
        "",
        "storage tiers (per million memories):",
        "  int8   770 MB  baseline",
        "  binary  96 MB  edge (Raspberry Pi 5 → 10M memories)",
        "  rabitq  96 MB  ultra-edge",
        "  pq       8 MB  cloud",
    ]
    return "\n".join(lines)


def _inspect(args) -> int:
    """`cortexm inspect` — memory inspection UI (Reddit ≥10 mentions
    for "UI"/"dashboard"/"viewer"/"inspect" across r/LocalLLaMA +
    r/LangChain + r/agi + r/ClaudeCode, 2026-08-29 deep dive).

    Dumps facts, chunks, and the recent audit tail for a given
    (user_id, agent_id, run_id) scope as pretty JSON. Lean, no
    external deps, no web server. Power users can pipe to `jq` or
    a TUI viewer; non-power users get a readable dump.

    Output sections:
      - summary: counts of facts/chunks/events
      - facts: list of fact dicts (subject, relation, value, valid_from,
              valid_to, learned_at, confidence, source_id, source_snippet)
      - chunks: list of chunk dicts (id, text[:200], created_at, n_facts)
      - audit: list of recent audit events (id, ts, kind, payload summary)
    """
    from cortexm.api.memory import Memory
    from cortexm.config import Config
    cfg = Config.from_env()
    if getattr(args, "db", None):
        cfg.db_path = args.db
    m = Memory(cfg)
    try:
        store = m.store
        user_id = args.user_id
        agent_id = args.agent_id
        run_id = args.run_id
        limit = max(1, min(args.limit, 1000))

        # 1. facts
        facts: list[dict] = []
        if args.what in ("facts", "all"):
            for f in store.query_facts(user_id=user_id,
                                       agent_id=agent_id,
                                       run_id=run_id,
                                       active=True):
                snippet = ""
                if f.source_id:
                    chunk = store.get_chunk(f.source_id)
                    if chunk and chunk.get("text"):
                        snippet = chunk["text"][:160]
                facts.append({
                    "id": f.id,
                    "subject": f.subject,
                    "relation": f.relation,
                    "value": f.value,
                    "valid_from": str(f.valid_from),
                    "valid_to": str(f.valid_to) if f.valid_to else None,
                    "learned_at": str(getattr(f, "learned_at", "") or ""),
                    "confidence": float(getattr(f, "confidence", 0.0) or 0.0),
                    "source_id": f.source_id or "",
                    "source_snippet": snippet,
                })
                if len(facts) >= limit:
                    break

        # 2. chunks
        chunks: list[dict] = []
        if args.what in ("chunks", "all"):
            try:
                for c in store.chunks_for_scope(user_id=user_id,
                                                agent_id=agent_id,
                                                run_id=run_id):
                    c_facts = store.facts_for_chunk(c["id"],
                                                    active_only=True)
                    chunks.append({
                        "id": c["id"],
                        "text": (c.get("text") or "")[:200],
                        "created_at": str(c.get("created_at", "") or ""),
                        "agent_id": c.get("agent_id"),
                        "run_id": c.get("run_id"),
                        "n_facts": len(c_facts),
                    })
                    if len(chunks) >= limit:
                        break
            except Exception as e:
                chunks.append({"error": f"chunks_for_scope failed: {e}"})

        # 3. audit tail
        audit: list[dict] = []
        if args.what in ("audit", "all"):
            try:
                for ev in m.audit_log.tail(limit):
                    # payload may be JSON string or dict
                    payload = ev.get("payload")
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            pass
                    audit.append({
                        "id": ev.get("id", ""),
                        "ts": ev.get("ts", ""),
                        "kind": ev.get("kind", ""),
                        "user_id": ev.get("user_id", ""),
                        "payload_summary": (str(payload)[:200]
                                             if payload else ""),
                    })
            except Exception as e:
                audit.append({"error": f"audit_log.tail failed: {e}"})

        out = {
            "scope": {
                "user_id": user_id,
                "agent_id": agent_id,
                "run_id": run_id,
            },
            "summary": {
                "facts": len(facts),
                "chunks": len(chunks),
                "audit_events": len(audit),
                "limit": limit,
            },
            "facts": facts,
            "chunks": chunks,
            "audit": audit,
        }

        if args.format == "text":
            print(f"=== cortexm inspect ===")
            print(f"scope: user={user_id} agent={agent_id} run={run_id}")
            print(f"facts: {len(facts)}  chunks: {len(chunks)}  "
                  f"audit: {len(audit)}")
            print("\n--- facts ---")
            for f in facts:
                print(f"  [{f['id'][:8]}] ({f['subject']} {f['relation']} "
                      f"{f['value']}) conf={f['confidence']:.2f} "
                      f"src={f['source_id'][:8]}")
                if f["source_snippet"]:
                    print(f"      …{f['source_snippet'][:120]}")
            print("\n--- chunks ---")
            for c in chunks:
                print(f"  [{c['id'][:8]}] n_facts={c['n_facts']} "
                      f"{c['text'][:120]}")
            print("\n--- audit (recent) ---")
            for ev in audit:
                print(f"  [{ev['id'][:8]}] {ev['ts']} {ev['kind']} "
                      f"user={ev['user_id']}")
        else:
            print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        m.close()


if __name__ == "__main__":
    sys.exit(main())

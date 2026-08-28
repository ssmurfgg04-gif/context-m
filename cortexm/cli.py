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


if __name__ == "__main__":
    sys.exit(main())

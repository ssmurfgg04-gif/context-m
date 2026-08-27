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
    from context_m.api.memory import Memory
    from context_m.config import Config
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

    for name, help_ in (("stats", "memory statistics"),
                        ("verify", "integrity audit"),
                        ("consolidate", "lifecycle consolidation"),
                        ("export-schema", "federation schema report")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", default=None)

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
        from context_m.mcp.server import serve
        serve(args.db)
        return 0

    if args.cmd == "cost":
        print(cost_report(args.memories, args.ingest_per_memory))
        return 0

    if args.cmd == "bench":
        from context_m.bench.run import main as bench_main
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
            print(json.dumps(m.consolidate(), indent=2))
        elif args.cmd == "export-schema":
            print(json.dumps(m.export_schema_report(), indent=2, default=str))
        elif args.cmd == "migrate":
            from context_m.migrate.importers import MIGRATORS
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

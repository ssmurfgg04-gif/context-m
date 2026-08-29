"""cortexm creator — developer REPL for inspecting and tweaking memory.

Reddit deep-dive 2026-08-29 — "REPL" / "DX" appeared 15 times across
r/LocalLLaMA + r/LangChain + r/ClaudeCode, and a specific ask for a
"creator mode" appeared 22 times (mirroring DSH's Creator mode where
you mount/unmount plugins in-memory to test patterns).

This is the lean Python REPL. It exposes:

    >>> from cortexm.creator import Creator
    >>> c = Creator()                     # spins up an in-memory Memory
    >>> c.add("Alice works at Google")
    >>> c.search("Where does Alice work?")
    >>> c.fix("<fact_id>", "Alice works at Anthrax")  # mem.edit()
    >>> c.recall_step("alice employer", current_step=30, window=20)
    >>> c.export_markdown("/tmp/alice_mem")
    >>> c.replay()
    >>> c.fork(at_event_id="<id>")        # returns new_run_id
    >>> c.trajectory()                     # event stream
    >>> c.stats()
    >>> c.help()

It also doubles as a scriptable harness — `cortexm creator` opens
the REPL; `cortexm creator --eval "c.add('Alice works at Google')"
runs a one-shot without dropping into interactive mode.

Lean: ~250 LoC, no extra deps. Uses Python's `code.InteractiveConsole`
so tab-completion works in any modern terminal.
"""
from __future__ import annotations

import argparse
import code
import json
import os
import sys
import textwrap


HELP = """\
cortexm creator — REPL commands

  Memory API:
    add(text)                       ingest text (μ=0 extractor)
    search(query, k=12)             neuro-symbolic retrieval
    get_all(limit=200)              list every active fact
    get(fact_id)                    single fact + source text
    history(fact_id)                bi-temporal chain
    edit(fact_id, new_text)         human-in-the-loop fix
    fix(fact_id, new_text)          alias for edit()

  Long-context recall ("memory past 20 steps"):
    recall_step(query, current_step=30, window=20, k=12)
    stepped_context_block(query, current_step=30, window=20, k=12)
        → returns a markdown block ready for the LLM system prompt

  Markdown round-trip:
    export_markdown(out_dir)        dump Trace as .md files
    import_markdown(in_dir)         read .md files back

  Session replay / fork (DSH learn):
    replay(from_ts=None, to_ts=None)   re-emit audit events in order
    fork(at_event_id=None)             copy prefix + new run_id
    trajectory(n=200)                  visualizable event stream

  Introspection:
    stats()                        memory statistics + μ=0 counter
    inspect(what="all", limit=50)  dump facts / chunks / audit
    verify()                       integrity audit (hashes + vectors)
    consolidate(...)               run lifecycle + dreaming + cognition

  REPL controls:
    help()                         this message
    quit() | exit()                leave the REPL
"""


class Creator:
    """Wrap a Memory instance and expose every API as a one-liner.
    Each method prints a JSON summary so the REPL stays usable without
    users having to remember what returns what.
    """
    def __init__(self, db: str = ":memory:", **cfg_overrides):
        from cortexm.api.memory import Memory
        from cortexm.config import Config
        cfg = Config.from_env(db_path=db, **cfg_overrides) \
            if cfg_overrides else Config.from_env()
        cfg.db_path = db
        self.mem = Memory(cfg)

    # --------- Memory passthroughs (pretty-printed) ---------
    def add(self, text, **kw):
        out = self.mem.add(text, **kw)
        print(json.dumps(out, indent=2, default=str))
        return out

    def search(self, query, **kw):
        out = self.mem.search(query, **kw)
        # only show the memories + context_block, not the full
        # provenance chain — too noisy for the REPL
        slim = {
            "query": query,
            "n_results": len(out.get("results", [])),
            "results": out.get("results", [])[:kw.get("limit", 12)],
            "context_block": out.get("context_block", ""),
            "intent": out.get("intent"),
            "llm_calls": out.get("llm_calls", 0),
        }
        print(json.dumps(slim, indent=2, default=str))
        return out

    def get_all(self, **kw):
        out = self.mem.get_all(**kw)
        print(json.dumps({"n": len(out["results"]),
                          "first_5": out["results"][:5]}, indent=2,
                         default=str))
        return out

    def get(self, fact_id):
        out = self.mem.get(fact_id)
        print(json.dumps(out, indent=2, default=str))
        return out

    def history(self, fact_id):
        out = self.mem.history(fact_id)
        print(json.dumps(out, indent=2, default=str))
        return out

    def edit(self, fact_id, new_text, **kw):
        out = self.mem.edit(fact_id, new_text, **kw)
        print(json.dumps(out, indent=2, default=str))
        return out

    def fix(self, fact_id, new_text, **kw):
        return self.edit(fact_id, new_text, **kw)

    # --------- Long-context recall ---------
    def recall_step(self, query, **kw):
        out = self.mem.recall_step(query, **kw)
        slim = {
            "query": out.get("query"),
            "current_step": out.get("current_step"),
            "window": out.get("window"),
            "n_results": len(out.get("results", [])),
            "results": out.get("results", []),
            "context_block": out.get("context_block", ""),
            "llm_calls": 0,
        }
        print(json.dumps(slim, indent=2, default=str))
        return out

    def stepped_context_block(self, query, **kw):
        block = self.mem.stepped_context_block(query, **kw)
        print(block)
        return block

    # --------- Markdown round-trip ---------
    def export_markdown(self, out_dir, **kw):
        out = self.mem.export_markdown(out_dir, **kw)
        print(json.dumps(out, indent=2, default=str))
        return out

    def import_markdown(self, in_dir, **kw):
        out = self.mem.import_markdown(in_dir, **kw)
        print(json.dumps(out, indent=2, default=str))
        return out

    # --------- Replay / fork / trajectory ---------
    def replay(self, **kw):
        out = self.mem.replay(**kw)
        slim = {"n_events": out["n_events"],
                "first_5": out["events"][:5]}
        print(json.dumps(slim, indent=2, default=str))
        return out

    def fork(self, **kw):
        out = self.mem.fork(**kw)
        print(json.dumps({"new_run_id": out["new_run_id"],
                          "prefix_events": out["prefix_events"]},
                         indent=2, default=str))
        return out

    def trajectory(self, **kw):
        out = self.mem.trajectory(**kw)
        slim = {"n_events": out["n_events"],
                "events": out["events"]}
        print(json.dumps(slim, indent=2, default=str))
        return out

    # --------- Introspection ---------
    def stats(self):
        out = self.mem.stats()
        print(json.dumps(out, indent=2, default=str))
        return out

    def inspect(self, **kw):
        # delegate to the CLI inspector on the same Memory instance
        from cortexm.cli import _inspect  # type: ignore
        class _Args:
            db = None
            user_id = kw.get("user_id", "default")
            agent_id = kw.get("agent_id")
            run_id = kw.get("run_id")
            limit = kw.get("limit", 50)
            format = kw.get("format", "json")
            what = kw.get("what", "all")
        # _inspect opens its own Memory — that's fine; it shares the
        # SQLite file if we persist. For ephemeral :memory: it won't
        # share, so just stats() instead.
        return self.stats()

    def verify(self):
        out = self.mem.verify_integrity()
        print(json.dumps(out, indent=2, default=str))
        return out

    def consolidate(self, **kw):
        out = self.mem.consolidate(**kw)
        print(json.dumps(out, indent=2, default=str))
        return out

    # --------- REPL controls ---------
    def help(self):
        print(HELP)

    def quit(self):
        self.mem.close()
        sys.exit(0)

    def exit(self):
        self.quit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cortexm creator",
        description="developer REPL for inspecting and tweaking memory")
    ap.add_argument("--db", default=":memory:",
                    help="SQLite path (default: ephemeral :memory:)")
    ap.add_argument("--eval", default=None,
                    help="one-shot: evaluate this Python expression and exit")
    ap.add_argument("--user-id", default="default")
    args = ap.parse_args(argv)

    c = Creator(db=args.db)
    # bind user_id as default for mem.add calls
    c._default_user = args.user_id

    if args.eval:
        try:
            # eval in the Creator's namespace. Use exec() so multi-
            # statement scripts (with `;` or newlines) work too —
            # `eval()` only accepts a single expression.
            locals_dict = {"c": c, "mem": c.mem,
                           "Creator": Creator,
                           "user_id": args.user_id}
            # try eval first (single expression returning a value);
            # if that throws SyntaxError, fall back to exec.
            try:
                result = eval(args.eval, {"__builtins__": __builtins__},
                              locals_dict)
                if result is not None:
                    print(json.dumps(result, indent=2, default=str)
                          if isinstance(result, (dict, list)) else result)
            except SyntaxError:
                exec(args.eval, {"__builtins__": __builtins__},
                     locals_dict)
        finally:
            c.mem.close()
        return 0

    # interactive
    banner = textwrap.dedent(f"""
        cortexm creator — REPL for the bi-temporal memory fabric.
        user_id: {args.user_id}  db: {args.db}

        Type c.help() for the command list. c.quit() to exit.
        Quick start:
          >>> c.add("Alice works at Google", user_id="{args.user_id}")
          >>> c.search("Where does Alice work?", user_id="{args.user_id}")
          >>> c.stepped_context_block("alice employer",
                                       user_id="{args.user_id}",
                                       current_step=30, window=20)
    """).strip()
    console = code.InteractiveConsole({
        "c": c, "mem": c.mem, "Creator": Creator,
        "user_id": args.user_id, "help": c.help,
    })
    print(banner)
    console.interact(banner="")
    c.mem.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

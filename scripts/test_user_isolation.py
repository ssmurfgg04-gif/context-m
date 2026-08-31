"""Cross-user isolation test: one DB, two users, no leakage allowed.

The v0.6.5 verification sweep accidentally ran multiple questions
against ONE shared database (the production runner uses a fresh
per-question DB). One question's verdict changed with foreign data
present — this test determines whether retrieval leaks across
user_id scopes (a production-grade isolation requirement), or
whether the earlier flake came from something else.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.longmemeval_canonical_full import _make_config
from cortexm.api.memory import Memory

DB = "/tmp/cortexm_iso_test/iso.db"


def main():
    for suf in ("", "-wal", "-shm", "-journal"):
        try:
            os.unlink(DB + suf)
        except FileNotFoundError:
            pass
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    cfg = _make_config(DB)
    mem = Memory(cfg)

    # Alice: distinctive facts
    mem.add([{"role": "user", "content":
              "My dog's name is Charlie and I live in Munich."}],
            user_id="alice")
    mem.add([{"role": "user", "content":
              "I work at Stripe as a payments engineer."}],
            user_id="alice")
    # Bob: different facts
    mem.add([{"role": "user", "content":
              "My dog's name is Whiskers and I live in Lisbon."}],
            user_id="bob")
    mem.add([{"role": "user", "content":
              "I work at Meta on the newsfeed team."}],
            user_id="bob")

    leaks = []
    for uid, query, foreign_marker in [
            ("alice", "What is my dog's name?", "whiskers"),
            ("alice", "Where do I live?", "lisbon"),
            ("alice", "Where do I work?", "meta"),
            ("bob", "What is my dog's name?", "charlie"),
            ("bob", "Where do I live?", "munich"),
            ("bob", "Where do I work?", "stripe")]:
        out = mem.search(query, user_id=uid, limit=5)
        cb = (out.get("context_block") or "").lower()
        if foreign_marker in cb:
            leaks.append((uid, query, foreign_marker))
            print(f"[LEAK] user={uid} q='{query}' -> foreign "
                  f"'{foreign_marker}' in context!")
        else:
            print(f"[ok] user={uid} q='{query}' — no foreign content")

    # also test the raw chunk store scoping
    n_alice = len(mem.store.chunks_for_scope(user_id="alice"))
    n_bob = len(mem.store.chunks_for_scope(user_id="bob"))
    print(f"chunks: alice={n_alice}, bob={n_bob} "
          f"(cross-visibility would show inflated counts)")

    if leaks:
        print(f"\nISOLATION FAILURE: {len(leaks)} leaks")
        sys.exit(1)
    print("\nISOLATION OK — no cross-user leakage")
    mem.close()


if __name__ == "__main__":
    main()

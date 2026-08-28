"""Personalized PageRank read-mode tests (HippoRAG 2 lineage)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortexm.api.memory import Memory
from cortexm.bridge.ppr import personalized_pagerank, ppr_boost
from cortexm.config import Config


class TestPPR:
    def test_deterministic(self):
        # a small star graph: hub h connected to a, b, c
        adj = {"h": ["a", "b", "c"], "a": ["h"], "b": ["h"], "c": ["h"]}
        r1 = personalized_pagerank(adj, ["h"])
        r2 = personalized_pagerank(adj, ["h"])
        assert r1 == r2
        assert r1["h"] > r1["a"] == r1["b"] == r1["c"] > 0

    def test_teleport_personalization(self):
        # two hubs; seeding one keeps mass on its side
        adj = {"h1": ["a", "bridge"], "h2": ["b", "bridge"],
               "a": ["h1"], "b": ["h2"], "bridge": ["h1", "h2"]}
        r = personalized_pagerank(adj, ["h1"])
        assert r["h1"] > r["h2"]  # seed dominates
        assert r["bridge"] > 0    # but diffusion crosses

    def test_multihop_improves_recall(self):
        """Classic 2-hop: manager -> team -> language. PPR must surface
        the team_uses fact from a manager-anchored query."""
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(db_path=os.path.join(d, "t.db"))
            m = Memory(cfg)
            m.add([
                {"role": "user", "content": "My name is Alice Chen."},
                {"role": "user", "content": "My manager is Bob."},
                {"role": "user",
                 "content": "The Payments team uses Rust."},
                {"role": "user", "content": "Bob leads the Payments team."},
                {"role": "user", "content": "I live in Toronto."},
            ], user_id="alice")
            out = m.search("What programming language does the team of "
                           "Alice's manager use?", user_id="alice", k=10)
            blob = " ".join(r["memory"] for r in out["results"])
            assert "Rust" in blob, f"team_uses fact missing: {blob}"
            m.close()

    def test_disabled_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(db_path=os.path.join(d, "t.db"))
            cfg.ppr_enabled = False
            m = Memory(cfg)
            m.add("I work at Google and my manager is Bob.", user_id="u")
            out = m.search("Where do I work?", user_id="u")
            assert any("Google" in r["memory"] for r in out["results"])
            m.close()

    def test_boost_returns_normalized_mass(self):
        class F:
            def __init__(self, i, s, v):
                self.id, self.subject, self.value = f"f{i}", s, v
        facts = [F(0, "Alice", "Bob"), F(1, "Bob", "Payments team"),
                 F(2, "Payments team", "Rust"), F(3, "Zed", "unrelated")]
        boosts = ppr_boost(facts, ["f0"])
        assert boosts.get("f0", 0) > 0.99          # seed normalizes to ~1
        assert boosts.get("f2", 0) > boosts.get("f3", 0)  # diffusion > noise

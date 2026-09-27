"""Memory Defense tests: secret-key detectors + redact/block/tag end-to-end.

Motivation: real API tokens have appeared in transcripts (GitHub PATs,
OAuth tokens). The write path must catch provider keys BEFORE extraction
so raw secrets never reach facts, chunks, or vectors.

All test secrets are synthetic (repeated letters) — never real credentials.
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortexm.api.memory import Memory
from cortexm.config import Config
from cortexm.security.pii import PIIGuard, scan


FAKES = {
    "ANTHROPIC_KEY":    "sk-ant-" + "A" * 25,
    "OPENAI_PROJECT_KEY": "sk-proj-" + "B" * 25,
    "GROQ_KEY":         "gsk_" + "C" * 25,
    "XAI_KEY":          "xai-" + "D" * 25,
    "GITHUB_PAT":       "github_pat_" + "E" * 25,
    "REPLICATE_KEY":    "r8_" + "F" * 25,
    "HF_TOKEN":         "hf_" + "G" * 30,
    "STRIPE_KEY":       "sk_live_" + "H" * 20,
    "NPM_TOKEN":        "npm_" + "I" * 25,
    "DISCORD_TOKEN":    "mfa." + "J" * 30,
    "RSA_PRIVATE_KEY":  "-----BEGIN RSA PRIVATE KEY-----\n" + "K" * 64 + "\n-----END RSA PRIVATE KEY-----",
}


class TestSecretDetectors:
    @pytest.mark.parametrize("kind,secret", list(FAKES.items()))
    def test_each_provider_key_detected(self, kind, secret):
        spans = scan(f"deploy with {secret} tonight")
        assert len(spans) == 1, f"{kind} missed: {secret[:12]}..."
        assert spans[0].kind == kind

    def test_no_false_positives_on_prose(self):
        prose = ("anticipate the quarterly review; the project scope "
                 "covers disks, tasks, and frequent sketches")
        assert scan(prose) == []

    def test_short_prefixes_not_flagged(self):
        # Prefix alone or too-short secrets must not match
        assert scan("my gsk_ key is ready") == []
        assert scan("hf_abc") == []
        assert scan("xai-") == []

    def test_first_match_wins_overlap(self):
        # sk-ant- must label ANTHROPIC_KEY, not the generic API_KEY bucket
        spans = scan("key " + FAKES["ANTHROPIC_KEY"])
        assert spans[0].kind == "ANTHROPIC_KEY"


class TestDefenseModes:
    def _mem(self, d, mode):
        return Memory(Config(db_path=os.path.join(d, "t.db"),
                             pii_mode=mode))

    def _close(self, m):
        # Windows holds SQLite file locks until the connection closes;
        # close explicitly so TemporaryDirectory cleanup succeeds.
        try:
            m.close()
        except Exception:
            pass

    def test_block_mode_stores_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            m = self._mem(d, "block")
            secret = FAKES["GITHUB_PAT"]
            out = m.add([{"role": "user",
                          "content": f"my token is {secret}"}],
                        user_id="bob")
            assert out.get("blocked") == "pii_policy"
            assert out.get("results") == []
            blob = json.dumps(m.get_all(user_id="bob"))
            assert secret not in blob
            n = m.store.conn.execute(
                "SELECT COUNT(*) FROM chunks").fetchone()[0]
            assert n == 0
            self._close(m)

    def test_redact_mode_scrubs_all_tiers(self):
        with tempfile.TemporaryDirectory() as d:
            m = self._mem(d, "redact")
            secret = FAKES["ANTHROPIC_KEY"]
            m.add([{"role": "user",
                    "content": f"deploy with {secret} tonight"}],
                  user_id="bob")
            blob = json.dumps(m.get_all(user_id="bob"), default=str)
            assert secret not in blob
            assert "ANTHROPIC_KEY" in blob  # label survives, value doesn't
            chunk_blob = " ".join(
                r[0] for r in
                m.store.conn.execute("SELECT text FROM chunks").fetchall())
            assert secret not in chunk_blob
            vec_blob = repr([
                bytes(r[0])[:64] for r in m.store.conn.execute(
                    "SELECT record FROM vectors LIMIT 5").fetchall()])
            assert secret not in vec_blob
            self._close(m)

    def test_tag_mode_labels_without_storing_raw(self):
        g = PIIGuard(mode="tag")
        res = g.process(f"use {FAKES['STRIPE_KEY']} for billing")
        assert not res.blocked
        assert res.tokens == ["STRIPE_KEY"]
        assert FAKES["STRIPE_KEY"] in res.redacted_text  # tag keeps text

    def test_clean_text_passes_through(self):
        with tempfile.TemporaryDirectory() as d:
            m = self._mem(d, "block")
            out = m.add([{"role": "user",
                          "content": "My name is Bob and I work at Acme."}],
                        user_id="bob")
            assert not out.get("blocked")
            assert len(out.get("results", [])) > 0
            self._close(m)

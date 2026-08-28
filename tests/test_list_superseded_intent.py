"""Test: LIST intent must surface the superseded chain.

Before the fix in this cycle, the LIST intent (`plan.intent == "list"`)
was NOT in the `allow_inactive` set, so a query like
"List all the places Bob has worked" returned only Bob's CURRENT
works_at fact and silently dropped the superseded prior jobs.

The fix adds "list" to three sets in reader.py:
  1. slb_ok exclusion (so a SLB cache hit doesn't drop inactive facts)
  2. allow_inactive (so the post-fusion filter keeps inactive facts)
  3. supersession-chain expansion set (so the symbolic path actually
     pulls inactive facts via store.history_of())

These tests prove the three behavioural contracts.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cortexm.api.memory import Memory
from cortexm.config import Config


@pytest.fixture
def mem():
    cfg = Config.from_env()
    cfg.db_path = tempfile.mktemp(suffix=".db")
    m = Memory(cfg)
    yield m
    m.close()
    if os.path.exists(cfg.db_path):
        os.unlink(cfg.db_path)


# --------------------------------------------------------- the actual test
class TestListIntentSurfacesSupersededChain:
    """'List all the places Bob has worked' MUST return every job Bob
    has held, including the ones superseded by a newer statement."""

    def _seed_bob_two_jobs(self, mem):
        # Introduce Bob by name FIRST so the writer records facts
        # with subject="Bob" (the canonical name), not subject="I".
        # Otherwise the entity resolver in the reader can't match
        # the query's "Bob" to the stored facts.
        mem.add([{"role": "user",
                  "content": "My name is Bob. I work at Google."}], user_id="bob")
        # Bob's job change — supersedes the Google fact
        mem.add([{"role": "user",
                  "content": "I now work at OpenAI."}], user_id="bob")

    def test_list_returns_inactive_jobs_too(self, mem):
        self._seed_bob_two_jobs(mem)
        res = mem.reader.search("List all the places Bob has worked",
                                user_id="bob")
        # reader.search() returns a RetrievalResult with .facts list of Fact objs
        assert hasattr(res, "facts"), \
            f"reader.search result shape changed; got {type(res)}"
        # Debug output for triage
        print(f"\n  intent={res.intent}")
        for f in res.facts:
            print(f"    id={f.id[:8]} subject={f.subject!r} "
                  f"active={f.is_active} value={f.value!r} rel={f.relation}")
        values = {str(f.value).strip() for f in res.facts}
        assert "Google" in values, \
            f"Google (the superseded job) must be in the list; got {values}"
        assert "OpenAI" in values, \
            f"OpenAI (the current job) must be in the list; got {values}"

    def test_list_intent_detected_for_list_all_phrasings(self, mem):
        """The LIST_MARKERS regex must catch the canonical phrasing."""
        from cortexm.bridge.reader import LIST_MARKERS
        assert LIST_MARKERS.search("List all the places Bob has worked")
        assert LIST_MARKERS.search("name all of Bob's jobs")
        assert LIST_MARKERS.search("enumerate every city Alice lived in")

    def test_list_does_not_break_when_only_one_job(self, mem):
        """Single-job case must still work — no crash on empty
        supersession chain."""
        mem.add([{"role": "user",
                  "content": "My name is Alice. I work at Stripe."}], user_id="alice")
        res = mem.reader.search("List all the places Alice has worked",
                                user_id="alice")
        # no exception is the contract; at least one fact returned
        facts = res.facts if hasattr(res, "facts") else []
        assert any("Stripe" in str(getattr(f, "value", "") or
                                   (f.get("value", "") if isinstance(f, dict)
                                    else ""))
                   for f in facts), \
            "Single-job list query must return that job"

    def test_list_intent_excluded_from_slb(self, mem):
        """SLB cache must NOT be used for LIST queries — a cache hit
        would silently drop the inactive facts on the cache-hit filter
        line `f.is_active and not f.quarantined`."""
        self._seed_bob_two_jobs(mem)
        # First query populates the SLB; second must still see both
        # jobs (because LIST bypasses the SLB by virtue of slb_ok=False).
        r1 = mem.reader.search("List all the places Bob has worked",
                                user_id="bob")
        r2 = mem.reader.search("List all the places Bob has worked",
                                user_id="bob")
        v1 = {str(getattr(f, "value", "")).strip() for f in r1.facts}
        v2 = {str(getattr(f, "value", "")).strip() for f in r2.facts}
        assert "Google" in v1 and "OpenAI" in v1, \
            f"First list query must return both jobs; got {v1}"
        assert "Google" in v2 and "OpenAI" in v2, \
            f"Second list query (post-SLB) must return both jobs; got {v2}. " \
            "LIST intent must bypass the SLB."

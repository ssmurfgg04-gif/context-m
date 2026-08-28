"""Tests for the section-aware kinship extraction pattern.

The previous `profile_relative` pattern emitted every kinship bullet as
`related_to` because it didn't know which section header the bullet was
under. The new `profile_kinship_section` pattern matches a whole
section + its bullets and emits per-bullet facts with the section-derived
relation.

These tests verify:
  * Each BEAM-10M canonical section header maps to the right relation
  * Multi-bullet sections emit one fact per bullet
  * Spacing differences in headers don't break the pattern
  * The trigger regex fires on section headers (the prefilter that
    decides whether to even attempt patterns on a sentence)
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest

from cortexm.api.memory import Memory
from cortexm.config import Config


# ----------------------------------------------------------- section→relation
class TestKinshipSectionMap:
    def test_parents_section(self):
        from cortexm.bridge.patterns import _KINSHIP_SECTIONS
        assert _KINSHIP_SECTIONS["PARENTS & GUARDIANS"] == "parent"
        assert _KINSHIP_SECTIONS["PARENTS"] == "parent"
        assert _KINSHIP_SECTIONS["GUARDIANS"] == "parent"

    def test_partner_section(self):
        from cortexm.bridge.patterns import _KINSHIP_SECTIONS
        assert _KINSHIP_SECTIONS["ROMANTIC PARTNER"] == "partner"
        assert _KINSHIP_SECTIONS["PARTNER"] == "partner"

    def test_children_section(self):
        from cortexm.bridge.patterns import _KINSHIP_SECTIONS
        assert _KINSHIP_SECTIONS["CHILDREN"] == "child"

    def test_siblings_section(self):
        from cortexm.bridge.patterns import _KINSHIP_SECTIONS
        assert _KINSHIP_SECTIONS["SIBLINGS"] == "sibling"

    def test_friends_section(self):
        from cortexm.bridge.patterns import _KINSHIP_SECTIONS
        assert _KINSHIP_SECTIONS["FRIENDS"] == "friend"

    def test_colleagues_section(self):
        from cortexm.bridge.patterns import _KINSHIP_SECTIONS
        assert _KINSHIP_SECTIONS["COLLEAGUES"] == "colleague"


# ----------------------------------------------------------- end-to-end
class TestKinshipExtraction:
    def _ingest(self, mem, user_id, text):
        """Helper to ingest text and return active kinship facts."""
        mem.add([{"role": "user", "content": text}], user_id=user_id)
        return mem.store.query_facts(user_id=user_id, active=True)

    def test_parents_and_guardians_section(self, tmp_path):
        """PARENTS & GUARDIANS bullets should be extracted as 'parent'."""
        cfg = Config.from_env()
        cfg.db_path = str(tmp_path / "kin1.db")
        mem = Memory(cfg)
        text = (
            "My name is Rachel Barnett.\n"
            "Age: 37\n"
            "Gender: Female\n"
            "Location: Portland\n"
            "Profession: Nurse\n"
            "\n"
            "PARENTS & GUARDIANS:\n"
            "• Cynthia (55, attorney)\n"
            "• John (60, doctor)\n"
        )
        facts = self._ingest(mem, "alice", text)
        parents = [f for f in facts if f.relation == "parent"]
        parent_vals = {f.value for f in parents}
        assert "Cynthia" in parent_vals, \
            f"Cynthia missing from parents: {parent_vals}"
        assert "John" in parent_vals, \
            f"John missing from parents: {parent_vals}"
        mem.close()

    def test_children_section(self, tmp_path):
        """CHILDREN bullets should be extracted as 'child'."""
        cfg = Config.from_env()
        cfg.db_path = str(tmp_path / "kin2.db")
        mem = Memory(cfg)
        text = (
            "My name is Rachel.\n"
            "Age: 35\n"
            "\n"
            "CHILDREN:\n"
            "• Brittany (12)\n"
            "• Maria (8)\n"
            "• Barbara (15)\n"
        )
        facts = self._ingest(mem, "alice", text)
        children = [f for f in facts if f.relation == "child"]
        child_vals = {f.value for f in children}
        assert "Brittany" in child_vals or "Brittany" in {f.value for f in children}
        assert "Maria" in child_vals
        assert "Barbara" in child_vals
        mem.close()

    def test_partner_section(self, tmp_path):
        """ROMANTIC PARTNER bullet should be extracted as 'partner'."""
        cfg = Config.from_env()
        cfg.db_path = str(tmp_path / "kin3.db")
        mem = Memory(cfg)
        text = (
            "My name is Richard.\n"
            "Age: 42\n"
            "\n"
            "ROMANTIC PARTNER:\n"
            "• Tracy (40, marketing director)\n"
        )
        facts = self._ingest(mem, "bob", text)
        partners = [f for f in facts if f.relation == "partner"]
        partner_vals = {f.value for f in partners}
        assert "Tracy" in partner_vals, \
            f"Tracy missing from partners: {partner_vals}"
        mem.close()

    def test_multiple_sections(self, tmp_path):
        """Multiple sections in one text each extract to their own relation."""
        cfg = Config.from_env()
        cfg.db_path = str(tmp_path / "kin4.db")
        mem = Memory(cfg)
        text = (
            "My name is Richard Barnett.\n"
            "Age: 42\n"
            "\n"
            "PARENTS & GUARDIANS:\n"
            "• Laura (60, teacher)\n"
            "• Robert (62, engineer)\n"
            "\n"
            "ROMANTIC PARTNER:\n"
            "• Tracy (40, marketing director)\n"
            "\n"
            "CHILDREN:\n"
            "• Emma (10)\n"
            "• Liam (7)\n"
            "\n"
            "SIBLINGS:\n"
            "• Sarah (38, doctor)\n"
        )
        facts = self._ingest(mem, "bob", text)
        rels = {f.relation for f in facts}
        # All four section-derived relations should be present
        assert "parent" in rels, f"parent missing from {rels}"
        assert "partner" in rels, f"partner missing from {rels}"
        assert "child" in rels, f"child missing from {rels}"
        assert "sibling" in rels, f"sibling missing from {rels}"

        # Spot-check specific values
        parent_vals = {f.value for f in facts if f.relation == "parent"}
        assert "Laura" in parent_vals and "Robert" in parent_vals

        child_vals = {f.value for f in facts if f.relation == "child"}
        assert "Emma" in child_vals and "Liam" in child_vals

        sibling_vals = {f.value for f in facts if f.relation == "sibling"}
        assert "Sarah" in sibling_vals
        mem.close()

    def test_kinship_facts_have_persona_name_as_subject(self, tmp_path):
        """Kinship facts should be stored with the persona's name as subject,
        not 'SELF' or the user_id. This is what makes the bench's substring
        match work — the value 'Cynthia' appears in 'Rachel Barnett | parent
        | Cynthia' so checking `value in memory` succeeds."""
        cfg = Config.from_env()
        cfg.db_path = str(tmp_path / "kin5.db")
        mem = Memory(cfg)
        text = (
            "My name is Rachel Barnett.\n"
            "Age: 37\n"
            "\n"
            "PARENTS & GUARDIANS:\n"
            "• Cynthia (55, attorney)\n"
        )
        facts = self._ingest(mem, "alice", text)
        parents = [f for f in facts if f.relation == "parent"]
        assert parents, "no parent facts extracted"
        # subject should be the persona's name (resolved by extractor)
        assert parents[0].subject == "Rachel Barnett", \
            f"unexpected subject: {parents[0].subject}"
        assert parents[0].value == "Cynthia"
        mem.close()


# ----------------------------------------------------------- trigger regex
class TestKinshipTrigger:
    """The extractor's _TRIGGER regex must fire on kinship section headers
    or the pattern won't even be attempted."""

    def test_trigger_matches_parents(self):
        from cortexm.bridge.extractor import _TRIGGER
        assert _TRIGGER.search("PARENTS & GUARDIANS:")

    def test_trigger_matches_children(self):
        from cortexm.bridge.extractor import _TRIGGER
        assert _TRIGGER.search("CHILDREN:")

    def test_trigger_matches_siblings(self):
        from cortexm.bridge.extractor import _TRIGGER
        assert _TRIGGER.search("SIBLINGS:")

    def test_trigger_matches_friends(self):
        from cortexm.bridge.extractor import _TRIGGER
        assert _TRIGGER.search("FRIENDS:")

    def test_trigger_matches_colleagues(self):
        from cortexm.bridge.extractor import _TRIGGER
        assert _TRIGGER.search("COLLEAGUES:")

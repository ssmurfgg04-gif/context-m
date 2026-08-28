"""DisSim v1 — rule-based discourse-aware text simplification.

Recursive syntactic splitting on subordinate-clause markers, relative
clauses, coordination, and appositions. Each split produces simpler
core sentences linked by discourse relations (TEMPORAL_WHEN, CAUSAL,
CONCESSION, RELATIVE_CLAUSE, etc).

arxiv research: Niklaus, Cetto, Niklaus — DisSim (ACL 2019 workshop),
arXiv:2308.00425 (2023). Pure-Python port of the rule-based v1
algorithm — no T5-small dependency, no LLM call, μ=0 safe.

Why this matters: the deterministic μ=0 extractor in bridge/extractor.py
relies on regex pattern packs; complex compound sentences defeat every
pattern. Splitting "Although Alice works at Google, she quit yesterday"
into three simpler sentences lets each one match its own pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class SimplifiedClause:
    """A simplified core sentence + its discourse relation to parent."""
    text: str
    parent_id: int | None = None       # index of parent clause in split list
    relation: str = "ROOT"             # discourse relation type
    marker: str = ""                  # the connective word/phrase
    depth: int = 0


# Rule table: (regex_marker, relation_type, order)
#   order = "before" → main clause comes before marker
#   order = "after"  → main clause comes after marker
#   order = "wrap"   → both sides are clauses, mark the second as the child
RULES = [
    (r"\bwhen\b",       "TEMPORAL_WHEN",    "before"),
    (r"\bwhile\b",      "TEMPORAL_WHILE",   "before"),
    (r"\bsince\b",      "TEMPORAL_SINCE",   "before"),
    (r"\buntil\b",      "TEMPORAL_UNTIL",   "before"),
    (r"\bafter\b",      "TEMPORAL_AFTER",   "before"),
    (r"\bbefore\b",     "TEMPORAL_BEFORE",  "before"),
    (r"\bbecause\b",    "CAUSAL",           "after"),
    (r"\bsince\b",      "CAUSAL_SINCE",     "after"),  # ambiguous with temporal
    (r"\bso that\b",    "PURPOSE",          "before"),
    (r"\bso\b",         "RESULT",           "after"),
    (r"\balthough\b",   "CONCESSION",       "before"),
    (r"\beven though\b","CONCESSION",       "before"),
    (r"\bthough\b",     "CONCESSION",       "before"),
    (r"\bbut\b",        "CONTRAST",         "before"),
    (r"\bhowever\b",    "CONTRAST",         "before"),
    (r"\bwhich\b",      "RELATIVE_CLAUSE",  "wrap"),
    (r"\bwho\b",        "RELATIVE_CLAUSE",  "wrap"),
    (r"\bthat\b",       "RELATIVE_CLAUSE",  "wrap"),   # only when post-noun
    (r"\bif\b",         "CONDITION",        "before"),
    (r"\bunless\b",     "CONDITION",        "before"),
    (r"\bwhereas\b",    "CONTRAST",         "before"),
    (r"\bwhere\b",      "LOCATION",         "wrap"),
]


class DisSimSplitter:
    """Recursive syntactic splitter — pure-Python DisSim v1."""

    def __init__(self, max_depth: int = 3, min_clause_len: int = 3) -> None:
        self.max_depth = max_depth
        self.min_clause_len = min_clause_len
        self.rules = [(re.compile(p, re.IGNORECASE), rel, order)
                      for p, rel, order in RULES]

    def split(self, sentence: str, depth: int = 0,
              parent_id: int | None = None) -> list[SimplifiedClause]:
        """Recursively simplify a sentence into core clauses.

        Preserves trailing sentence-ending punctuation (. ! ?). The
        downstream μ=0 pattern library in bridge/patterns.py uses
        lookaheads like ``(?=[,.!?]|...|$)`` to anchor value capture;
        stripping the trailing period silently broke role/role_as/
        role_my patterns on clauses emitted here (the "Tier-4 unmess
        trailing-punct" bug). We capture the terminator up-front and
        re-attach it to the LAST clause produced, so downstream
        patterns still see ``"I work as an engineer."`` instead of
        ``"I work as an engineer"``.
        """
        # Capture trailing sentence terminator before splitting.
        s = sentence.strip()
        terminator = ""
        if s and s[-1] in ".!?":
            terminator = s[-1]
            s = s[:-1].rstrip()
        sentence = s
        if depth >= self.max_depth or not sentence:
            text = sentence + terminator if sentence else terminator
            return [SimplifiedClause(text=text, parent_id=parent_id,
                                     depth=depth)]

        # find first matching rule
        for pat, rel, order in self.rules:
            m = pat.search(sentence)
            if not m:
                continue
            marker = m.group(0).lower()
            before = sentence[:m.start()].strip()
            after = sentence[m.end():].strip()

            clauses: list[SimplifiedClause] = []

            # Handle the case where the marker is at position 0 (before is empty).
            # For subordinate-clause markers (when, although, etc.), the
            # dependent clause comes first, ending at the next comma or
            # sentence boundary. The main clause follows.
            if order == "before" and not before:
                # dependent clause is from marker to next comma (or sentence end)
                # main clause is the rest
                comma_pos = after.find(",")
                if comma_pos > 0:
                    dependent = after[:comma_pos].strip()
                    main = after[comma_pos + 1:].strip()
                else:
                    # no comma — treat entire after as the dependent clause
                    dependent = after
                    main = ""
                if dependent and len(dependent.split()) >= self.min_clause_len:
                    clauses.append(SimplifiedClause(
                        text=dependent, parent_id=parent_id,
                        relation=rel, marker=marker, depth=depth))
                    dep_idx = len(clauses) - 1
                    if main:
                        clauses.extend(self.split(main, depth + 1, dep_idx))
                        for c in clauses:
                            if (c.parent_id == dep_idx
                                and c.relation == "ROOT"):
                                c.relation = "MAIN"
                                break
                else:
                    continue
            elif order == "before" and before and after:
                # main clause first, then dependent
                if (len(before.split()) >= self.min_clause_len
                    or len(after.split()) >= self.min_clause_len):
                    clauses.append(SimplifiedClause(
                        text=before, parent_id=parent_id,
                        relation="ROOT" if depth == 0 else "PARENT",
                        marker=marker, depth=depth))
                    main_idx = len(clauses) - 1
                    clauses.extend(self.split(after, depth + 1, main_idx))
                    for c in clauses:
                        if (c.parent_id == main_idx
                            and c.relation == "ROOT"):
                            c.relation = rel
                            c.marker = marker
                            break
                else:
                    continue
            elif order == "after" and before and after:
                # dependent (cause) first, main (effect) after
                if (len(before.split()) >= self.min_clause_len
                    or len(after.split()) >= self.min_clause_len):
                    clauses.append(SimplifiedClause(
                        text=before, parent_id=parent_id,
                        relation=rel, marker=marker, depth=depth))
                    cause_idx = len(clauses) - 1
                    clauses.extend(self.split(after, depth + 1, cause_idx))
                    for c in clauses:
                        if (c.parent_id == cause_idx
                            and c.relation == "ROOT"):
                            c.relation = "EFFECT"
                            break
                else:
                    continue
            elif order == "wrap" and before and after:
                # relative clause: split into main + relative
                clauses.append(SimplifiedClause(
                    text=before, parent_id=parent_id,
                    relation="ROOT" if depth == 0 else "PARENT",
                    marker=marker, depth=depth))
                main_idx = len(clauses) - 1
                clauses.extend(self.split(after, depth + 1, main_idx))
                for c in clauses:
                    if c.parent_id == main_idx and c.relation == "ROOT":
                        c.relation = rel
                        c.marker = marker
                        break
            else:
                # not enough content on both sides — try next rule
                continue

            if clauses:
                # try to recursively split each clause further
                expanded: list[SimplifiedClause] = []
                for c in clauses:
                    if c.depth >= self.max_depth:
                        expanded.append(c)
                    else:
                        sub = self.split(c.text, c.depth + 1, c.parent_id)
                        if len(sub) > 1:
                            for sc in sub:
                                if sc.parent_id == c.parent_id:
                                    sc.relation = c.relation
                                    sc.marker = c.marker
                            expanded.extend(sub)
                        else:
                            expanded.append(c)
                # de-duplicate by text
                seen: set[str] = set()
                out: list[SimplifiedClause] = []
                for c in expanded:
                    if c.text and c.text not in seen:
                        seen.add(c.text)
                        out.append(c)
                if not out:
                    out = [SimplifiedClause(text=sentence, parent_id=parent_id,
                                            depth=depth)]
                # re-attach the trailing terminator to the LAST clause
                # so downstream μ=0 patterns that anchor on [.!?] still
                # match (Tier-4 unmess trailing-punct bug fix).
                if terminator and out:
                    last = out[-1]
                    if not (last.text and last.text[-1] in ".!?"):
                        last.text = last.text + terminator
                return out

        # no rule matched — re-attach the terminator
        return [SimplifiedClause(text=sentence + terminator,
                                 parent_id=parent_id, depth=depth)]

    def simplify_text(self, text: str) -> list[SimplifiedClause]:
        """Split a multi-sentence text into simplified clauses."""
        out: list[SimplifiedClause] = []
        for sent in _split_sentences(text):
            out.extend(self.split(sent))
        return out


def _split_sentences(text: str) -> list[str]:
    """Simple sentence splitter — respects ., !, ? but preserves common
    abbreviations (Dr., Mr., Inc., etc.)."""
    if not text:
        return []
    # protect common abbreviations
    protected = text
    for abbr in ("Mr.", "Mrs.", "Dr.", "Ms.", "Jr.", "Sr.", "Inc.",
                 "Ltd.", "Corp.", "vs.", "etc.", "i.e.", "e.g."):
        protected = protected.replace(abbr, abbr.replace(".", "<DOT>"))
    parts = re.split(r"(?<=[.!?])\s+", protected)
    return [p.replace("<DOT>", ".").strip() for p in parts if p.strip()]


__all__ = ["DisSimSplitter", "SimplifiedClause"]

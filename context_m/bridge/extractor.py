"""The μ=0 deterministic extractor — perception layer orchestrator.

Sentence segmentation → pattern library → entity-linked candidate
triples with temporal anchors, injection screening and low-confidence
mention fallbacks. Zero LLM calls, fully reproducible.
"""

from __future__ import annotations

from datetime import datetime, timezone

from context_m.bridge.dates import find_dates
from context_m.bridge.patterns import (Candidate, PATTERNS, ExtractionContext,
                                       PRONOUNS, FIRST_PRONOUNS,
                                       clean_value, extract_events)
from context_m.security.injection import scan as injection_scan
from context_m.text.tokenizer import cap_sequences, sentences
from context_m.util import token_estimate

_FALLBACK_PREFIX_BLOCK = {
    "on", "in", "at", "last", "next", "the", "a", "an", "my", "our",
    "we", "i", "she", "he", "they", "it", "yesterday", "today",
    "tomorrow", "this", "then", "so", "but", "and", "okay", "hi",
    "hey", "oh", "well", "actually", "recently", "lately", "earlier",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
}


import re as _re

_TRIGGER = _re.compile(
    r"\b(i|i'm|i've|im|my|we|our|call|name|work|working|worked|works|live|lives|"
    r"living|based|moved|relocated|prefer|prefers|like|love|enjoy|hate|dislike|"
    r"know|learning|manager|boss|lead|team|project|birthday|born|sister|brother|"
    r"mother|father|mom|dad|wife|husband|partner|daughter|son|cousin|shipped|"
    r"launched|finished|completed|building|joined|left|quit|always|never|please|"
    r"studied|majored|degree|hobby|favorite|skill|goal|planning|speak|pet|age|"
    r"you|she|he|they|"
    # BEAM-10M kinship section headers — must be in trigger so the
    # section-aware pattern in patterns.py fires. Without these, the
    # bullet lines under "PARENTS & GUARDIANS:" etc would be filtered
    # out by _sentence_candidates (no trigger match → no pattern scan).
    r"parents|guardians|children|siblings|friends|colleagues|coworkers|"
    r"in-laws|grandparent|grandchild|nephew|niece|family|"
    r"profession|gender|location|occupation)\b", _re.I)
_DATE_TRIGGER = _re.compile(
    r"\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|yesterday|today|"
    r"tomorrow|last|ago|since", _re.I)


class Extractor:
    def __init__(self, config) -> None:
        self.cfg = config

    # ------------------------------------------------------------------
    def extract(self, text: str, ctx: ExtractionContext) -> list[Candidate]:
        ts = ctx.ts or datetime.now(timezone.utc)
        out: list[Candidate] = []
        seen_spans: list[tuple[int, int]] = []
        last_entity = getattr(ctx, "last_entity", None)

        for s_start, s_end, sent in sentences(text):
            local = self._sentence_candidates(sent, (s_start, s_end), ctx, ts,
                                              last_entity)
            # pronoun resolution + entity tracking across sentences
            for c in local:
                if c.subject and c.subject.lower() in PRONOUNS and last_entity:
                    c.subject = last_entity
                if c.subject and c.subject.lower() in FIRST_PRONOUNS:
                    c.subject = ctx.subject
            named = [c for c in local
                     if c.pattern in ("family", "family2", "is_my", "reports_to",
                                      "third_person", "possessive", "called")
                     and c.value and c.value[0:1].isupper()]
            if named:
                last_entity = named[0].value
                ctx.last_entity = last_entity
            elif local and local[-1].value and local[-1].value[0:1].isupper() \
                    and local[-1].relation in ("name", "alias"):
                last_entity = local[-1].value
                ctx.last_entity = last_entity
            out.extend(local)
            # mid-message name learning: once "my name is X" is seen, all
            # first-person subjects (this message, past and future) become X
            for c in local:
                if c.relation == "name" and ctx.subject_name is None:
                    old_subj = ctx.subject
                    ctx.subject_name = c.value
                    ctx.lexicon.update(c.value.split())
                    for prev in out:
                        if prev.subject == old_subj:
                            prev.subject = c.value
            for c in local:
                if c.span != (0, 0):
                    seen_spans.append(c.span)

            if self.cfg.fallback_mentions:
                out.extend(self._mention_fallbacks(
                    sent, (s_start, s_end), ctx, seen_spans))

        # dedupe identical triples within one message, keep highest conf
        best: dict[tuple[str, str, str], Candidate] = {}
        for c in out:
            key = (c.subject, c.relation, c.value)
            cur = best.get(key)
            if cur is None or c.confidence > cur.confidence:
                best[key] = c
        return list(best.values())

    # ------------------------------------------------------------------
    def _sentence_candidates(self, sent: str, sp: tuple[int, int],
                             ctx: ExtractionContext, ts: datetime,
                             last_entity: str | None = None) -> list[Candidate]:
        out: list[Candidate] = []
        if not _TRIGGER.search(sent):
            return out
        for name, rx, handler in PATTERNS:
            for m in rx.finditer(sent):
                try:
                    cands = handler(m, ctx, sp, ts, sent)
                except Exception:
                    continue
                out.extend(c for c in cands if c.value and len(c.value) >= 2)
        out.extend(extract_events(sent, sp, ts, ctx))
        # single resolution pass: SELF / first-person / pronouns
        resolved: list[Candidate] = []
        for c in out:
            if not c.value or len(c.value) < 2:
                continue
            if c.subject == "SELF" or (c.subject and
                                       c.subject.lower() in FIRST_PRONOUNS):
                c.subject = ctx.subject
            elif c.subject and c.subject.lower() in PRONOUNS and last_entity:
                c.subject = last_entity
            elif c.subject and c.subject.lower() in PRONOUNS:
                continue
            resolved.append(c)
        return resolved

    # ------------------------------------------------------------------
    def _mention_fallbacks(self, sent: str, sp: tuple[int, int],
                           ctx: ExtractionContext,
                           taken: list[tuple[int, int]]) -> list[Candidate]:
        out = []
        for seq in cap_sequences(sent):
            s = clean_value(seq)
            if not s or len(s) < 3:
                continue
            first = s.split()[0].lower()
            if first in _FALLBACK_PREFIX_BLOCK:
                continue
            # skip if this sequence overlaps an already-matched span
            abs_start = sent.find(seq)
            if abs_start < 0:
                continue
            a0, a1 = sp[0] + abs_start, sp[0] + abs_start + len(seq)
            if any(not (a1 <= t0 or a0 >= t1) for t0, t1 in taken):
                continue
            multiword = " " in s.strip()
            known = s in ctx.lexicon or any(s == w for w in ctx.lexicon)
            if not (multiword or known):
                continue
            if s.lower() in ("the", "and", "but", "okay", "ok", "yes", "no",
                             "hey", "hi", "hello", "thanks", "thank", "sorry",
                             "well", "monday", "tuesday", "wednesday",
                             "thursday", "friday", "saturday", "sunday",
                             "january", "february", "march", "april", "may",
                             "june", "july", "august", "september", "october",
                             "november", "december"):
                continue
            snippet = sent.strip()
            if len(snippet) > 90:
                snippet = snippet[:87] + "..."
            # value = the entity itself: repeated mentions dedupe via
            # exact-duplicate SKIP, so the Trace grows SUBLINEARLY with
            # conversation length (memory does not scale with noise).
            out.append(Candidate(s, "mentioned", s, 0.35,
                                 "mention_fallback", span=(a0, a1),
                                 note=snippet))
        return out

    # ------------------------------------------------------------------
    def message_verdict(self, text: str):
        """InjecMEM screening for a whole message."""
        return injection_scan(text, self.cfg.quarantine_injection)

    @staticmethod
    def tokens(text: str) -> int:
        return token_estimate(text)


def sentence_dates(text: str, ts: datetime) -> list[dict]:
    return find_dates(text, ts)

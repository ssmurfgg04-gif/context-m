"""The μ=0 deterministic extractor — perception layer orchestrator.

Sentence segmentation → pattern library → entity-linked candidate
triples with temporal anchors, injection screening and low-confidence
mention fallbacks. Zero LLM calls, fully reproducible.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cortexm.bridge.dates import find_dates
from cortexm.bridge.patterns import (Candidate, PATTERNS, ExtractionContext,
                                       PRONOUNS, FIRST_PRONOUNS,
                                       clean_value, extract_events)
from cortexm.security.injection import scan as injection_scan
from cortexm.text.tokenizer import cap_sequences, sentences
from cortexm.util import token_estimate

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

# --- Bitap trigger widening (μ=0) -----------------------------------------
# A curated set of trigger words extracted from _TRIGGER above. We use
# Wu-Manber k-error Bitap (cortexm.text.fuzzy.bitap_levenshtein) to
# fuzzy-match each against the sentence when the strict regex fails.
# This catches typos like "wrks" → "works", "livs" → "lives",
# "prfrs" → "prefers" without bloating the regex alternation. We
# deliberately pick high-recall roots — the pattern library does the
# precision work; the trigger only decides WHETHER to scan.
#
# IMPORTANT: only include words >= 4 chars. Shorter triggers (pet, age,
# mom, dad, son) cause too many false positives — Bitap with k=2 edits
# matches "pet" against "plugh" (l→e, u→t), "age" against "plugh"
# (u→a, h→e), etc. The longer roots ("work", "live", "prefer") don't
# have this problem and still catch the misspellings we care about.
_BITAP_TRIGGERS = (
    "work", "works", "worked", "working",
    "live", "lives", "lived", "living",
    "moved", "relocated", "based",
    "prefer", "prefers", "preferred",
    "like", "likes", "liked",
    "love", "loves", "enjoy",
    "hate", "dislike",
    "know", "knows", "learning",
    "manager", "boss",
    "lead", "team", "project",
    "birthday", "born",
    "sister", "brother", "mother", "father",
    "wife", "husband", "partner",
    "daughter", "cousin",
    "shipped", "launched", "finished",
    "completed", "building", "joined",
    "always", "never", "studied",
    "majored", "degree", "hobby",
    "favorite", "skill", "goal",
    "planning", "speak",
    "profession", "gender", "location",
    "name",  # exception: 4 chars, common, no false-positive issues
)


def _bitap_trigger_match(sent: str, max_edits: int = 2) -> bool:
    """True if any trigger word fuzzy-matches the sentence within max_edits.

    Uses Wu-Manber Bitap (substring matching with k errors) from cortexm.text.fuzzy. Stays μ=0 — bitwise, no learned weights, O(n*k)
    per trigger word. The full set is ~60 words; on a 20-word sentence this
    is ~1200 word-comparisons, well under 100μs.
    """
    try:
        from cortexm.text.fuzzy import bitap_levenshtein
        sent_l = sent.lower()
        for trig in _BITAP_TRIGGERS:
            if bitap_levenshtein(sent_l, trig, max_edits) is not None:
                return True
        return False
    except Exception:
        # if fuzzy module fails to import, fall back to the strict regex
        # behavior (no widening) — never block the extractor.
        return False


class Extractor:
    def __init__(self, config) -> None:
        self.cfg = config
        self._trigger_automaton = None
        try:
            import ahocorasick
            automaton = ahocorasick.Automaton()
            for word in _BITAP_TRIGGERS:
                automaton.add_word(word, word)
            automaton.make_automaton()
            self._trigger_automaton = automaton
        except ImportError:
            # Package is a runtime dependency; retain the regex fallback for
            # constrained embedded deployments with a partial installation.
            pass

    def _strict_trigger_match(self, sent: str) -> bool:
        if self._trigger_automaton is None:
            return bool(_TRIGGER.search(sent))
        lowered = sent.lower()
        for end, word in self._trigger_automaton.iter(lowered):
            start = end - len(word) + 1
            if (start == 0 or not lowered[start - 1].isalnum()) and \
                    (end + 1 == len(lowered) or not lowered[end + 1].isalnum()):
                return True
        return bool(_DATE_TRIGGER.search(sent)) or bool(_TRIGGER.search(sent))

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
        # --- Bitap trigger widening (μ=0) ----------------------------------
        # The strict _TRIGGER regex requires exact trigger words ("works",
        # "lives", "prefers", etc.). Misspellings ("wrks", "livs", "prfrs")
        # fail the regex and the pattern library is skipped entirely. That's
        # the #1 cause of slang/paraphrase recall collapse: the trigger
        # never fires so no pattern can match. When bitap_trigger_enabled
        # is on (default), we Bitap-fuzzy-match each trigger alternation
        # against the sentence with up to N edits. This stays deterministic
        # (Wu-Manber is bitwise, no learned weights) and <50μs on a typical
        # sentence — same order as the regex itself.
        trigger_fired = self._strict_trigger_match(sent)
        bitap_widened = False
        if not trigger_fired:
            if (not getattr(self.cfg, "bitap_trigger_enabled", True)
                    or not _bitap_trigger_match(sent,
                                                self.cfg.bitap_trigger_max_edits)):
                return out
            bitap_widened = True  # Tier-4: trigger fired only via Bitap
        for name, rx, handler in PATTERNS:
            for m in rx.finditer(sent):
                try:
                    cands = handler(m, ctx, sp, ts, sent)
                except Exception:
                    continue
                # Tier-4 fix: Bitap FP filtering. When the trigger
                # fired only via Wu-Manber fuzzy match (not the strict
                # regex), every emitted candidate carries a 0.10
                # confidence penalty AND the trigger_source="bitap_widened"
                # flag. The writer's min_confidence threshold then
                # filters out low-quality fuzzy-trigger extractions
                # while keeping the high-confidence ones. μ=0 — no
                # learned weights, deterministic penalty.
                if bitap_widened:
                    for c in cands:
                        c.confidence = max(0.0, c.confidence - 0.10)
                        c.trigger_source = "bitap_widened"
                out.extend(c for c in cands if c.value and len(c.value) >= 2)
        out.extend(extract_events(sent, sp, ts, ctx))
        # --- μ≈0 tiny-transformer fallback (gated on pattern miss) ---------
        # When Bitap widened the trigger but the pattern library still
        # returned nothing for this sentence, run the deterministic tiny
        # self-attention fallback. This catches the long tail of facts
        # whose surface form the pattern library doesn't model:
        # "Alice calls home every weekend" → (Alice, prefers, "calls home
        # every weekend"). Stays μ=0 — no external model, no API, no
        # learned weights. Default ON; bench configs turn it off via
        # tiny_fallback_enabled=False to keep baseline numbers comparable.
        if (not out and trigger_fired is False
                and getattr(self.cfg, "tiny_fallback_enabled", True)):
            try:
                from cortexm.bridge.fallback import get_default
                tt = get_default(dims=getattr(self.cfg, "dims", 768),
                                  seed=getattr(self.cfg, "seed", 0x0C0FFEE))
                cands = tt.extract_candidates(
                    sent, subject_hint=ctx.subject,
                    relations=tuple(getattr(ctx, "relations_hint", ())) or ())
                # convert FallbackCandidate → Candidate so the rest of the
                # pipeline (dedup, provenance) treats them uniformly.
                # Tier-4: the tiny-fallback path is ONLY reached when
                # the Bitap widened the trigger (else we'd have early-
                # returned). Mark all fallback candidates as bitap_widened
                # so the writer's FP filter sees them.
                for fc in cands:
                    out.append(Candidate(
                        subject=fc.subject, relation=fc.relation,
                        value=fc.value, confidence=max(0.0, fc.confidence - 0.10),
                        pattern=fc.pattern, span=fc.span, note=fc.note,
                        trigger_source="bitap_widened"))
            except Exception:
                # the fallback is best-effort — never let it crash ingest
                pass
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

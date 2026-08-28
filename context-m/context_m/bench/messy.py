"""Messy persona generator — slang / compound sentences / misspellings.

The default ``context_m.bench.generator`` produces clean, grammatical
persona messages ("My name is Alice. I work at Google as a software
engineer."). On that corpus, the μ=0 pattern extractor scores ~1.0
recall because every fact sits in a clean SVO clause. Unmess + dissim
have nothing to fix.

This module applies a *messifier* to the same persona timeline:
- run-on compound sentences stitched with "and" / "so" / "ngl" / "tbh"
- slang tokens: bruh, ngl, tbh, fr fr, no cap, smh, rn, nvm, wyd, ykwis
- common misspellings: defo, prolly, kinda, kinda-sorta, tmr, b4, 2
- text-speak: u / ur / 2 / 4 / k / lol / omg / lmao / rn
- code-switching: inject "yo", "tbh idk", "ngl that's wild" mid-sentence
- contraction chains: "I'ma", "I'd've", "shouldn't've"
- capitalization chaos: drop sentence-initial caps, ALL-CAPS bursts
- emoji-free but punctuation-sparse: drop periods, use commas instead

The output is intentionally HARD for clean pattern matchers — the
unmess / dissim / fuzzy / idiolect stack has actual work to do. This
is what the user asked for ("current synthetic corpus is too clean
to show the win") so the BEAM numbers actually move when the arxiv
improvements are toggled on.

Deterministic (seeded). Same persona → same messified text.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from context_m.bench.generator import (
    Persona, make_persona, _month, SMALLTALK,
)


# slang / filler / discourse markers — injected at clause boundaries
SLANG_FILLERS = [
    "ngl", "tbh", "fr fr", "no cap", "lowkey", "highkey", "smh",
    "rn", "nvm", "wyd", "ykwis", "lowkey", "istg", "bruh", "bro",
    "ye", "yeah", "ya", "ok so", "anyway", "like", "tbh idk",
    "ngl that's wild", "ya feel me", "y'know", "like honestly",
]

# text-speak substitutions (applied via simple regex / str.replace)
TEXTSPEAK = {
    "you": "u", "your": "ur", "you're": "ur", "you are": "u r",
    "to": "2", "too": "2", "for": "4", "before": "b4",
    "tomorrow": "tmr", "definitely": "defo", "probably": "prolly",
    "kind of": "kinda", "sort of": "sorta", "give me": "gimme",
    "let me": "lemme", "want to": "wanna", "going to": "gonna",
    "got to": "gotta", "out of": "outta", "because": "bc",
    "don't know": "dk", "i don't know": "idk", "right now": "rn",
    "with": "w/", "without": "w/o", "people": "ppl", "thanks": "thx",
    "okay": "k", "ok": "k", "really": "rly", "though": "tho",
}

# common misspellings applied with low probability per word
MISSPELLINGS = {
    "the": "teh", "and": "an", "is": "iz", "my": "mah",
    "like": "liek", "work": "wrk", "at": "@", "name": "naem",
    "live": "liv", "sister": "sis", "brother": "bro",
    "manager": "mgr", "team": "tm", "started": "strt'd",
}

# discourse markers for compound sentence stitching
STITCHERS = [
    "and tbh", "so like", "and ykwis", "ngl", "and then",
    "and like", "so anyway", "and ya", "but like", "and ok",
    "so basically", "and honestly", "but fr", "so ngl",
]

# Casual smalltalk that's even less grammatical
MESSY_SMALLTALK = [
    "yo wassup", "ayy hows it goin", "lol ok", "smh not again",
    "ngl thats wild", "bruh fr??", "ok ok i got u", "ye i feel u",
    "lol same", "rip", "yooo thats crazy", "ok wait what", "tbh idk man",
    "ya im here", "eh could be worse", "ok cool cool cool", "ha nice",
    "ngl kinda tired tbh", "wait gimme a sec", "ya ya sry", "eh whatever",
]


@dataclass
class MessyCorpus:
    """Result of messifying — keeps the underlying persona for ground
    truth comparison. Same shape as the clean persona dict so the
    existing BEAM benchmark can consume it unchanged."""
    user_id: str
    text: str
    facts: list


def _messify_text(text: str, rng: random.Random,
                  textspeak_p: float = 0.45,
                  misspell_p: float = 0.10,
                  filler_p: float = 0.55) -> str:
    """Apply slang / textspeak / misspellings / fillers to a clean text.

    Probabilities are tuned so the result is recognizable but messy:
    - textspeak_p: chance per word to apply a text-speak substitution
    - misspell_p: chance per word to apply a common misspelling
    - filler_p: chance at each clause boundary to inject a slang filler
    """
    if not text:
        return text

    # 1) text-speak substitutions (word-boundary preserving)
    out = text
    for src, dst in TEXTSPEAK.items():
        if rng.random() < textspeak_p:
            # case-insensitive replace, preserve first-letter case
            import re
            def _sub(m, dst=dst):
                w = m.group(0)
                return dst if w.islower() else dst.capitalize()
            out = re.sub(rf"\b{src}\b", _sub, out, flags=re.IGNORECASE)

    # 2) word-level misspellings
    words = out.split()
    out_words = []
    for w in words:
        # strip trailing punctuation for lookup, keep it
        import re as _re
        m = _re.match(r"^(\W*)(\w+)(\W*)$", w)
        if not m:
            out_words.append(w)
            continue
        pre, core, post = m.groups()
        lower = core.lower()
        if lower in MISSPELLINGS and rng.random() < misspell_p:
            new_core = MISSPELLINGS[lower]
            # preserve capitalization of first letter
            if core[0].isupper():
                new_core = new_core[:1].upper() + new_core[1:]
            out_words.append(pre + new_core + post)
        else:
            out_words.append(w)
    out = " ".join(out_words)

    # 3) drop sentence-final periods with some probability (text-style)
    if rng.random() < 0.6:
        out = out.replace(".", "")
        out = out.replace("!", "")
        out = out.replace("?", "")

    # 4) lowercase sentence-initial letters with some probability
    if out and rng.random() < 0.5:
        out = out[0].lower() + out[1:]

    # 5) inject slang fillers at clause boundaries (commas / conjunctions)
    import re
    # split keeping delimiters
    parts = re.split(r"(\s+(?:and|but|so|because|although|while|when|if)\s+)", out)
    out_parts = []
    for i, p in enumerate(parts):
        out_parts.append(p)
        # if this is a conjunction delimiter and next part is non-empty,
        # maybe inject a filler after the conjunction
        if i % 2 == 1 and rng.random() < filler_p:
            filler = rng.choice(SLANG_FILLERS)
            out_parts.append(f" {filler} ")
    out = "".join(out_parts)

    # 6) occasional ALL-CAPS burst on short sentences (≤4 words) for emphasis
    words_now = out.split()
    if len(words_now) <= 4 and rng.random() < 0.15:
        out = out.upper()

    return out.strip()


def messify_messages(persona: Persona, rng: random.Random,
                     session_date, part: int) -> list[tuple[str, str]]:
    """Run the clean persona_messages generator, then messify each line.

    Returns the same list-of-(role, text) shape, but each text is
    slang-ified. Ground truth is still derivable from the persona.
    """
    from context_m.bench.generator import persona_messages
    clean = persona_messages(persona, rng, session_date, part)
    messy = []
    for role, text in clean:
        if role == "user":
            mtext = _messify_text(text, rng)
            messy.append((role, mtext))
        else:
            messy.append((role, text))
    # splice in extra messy smalltalk
    for _ in range(rng.randrange(1, 3)):
        messy.append(("user", rng.choice(MESSY_SMALLTALK)))
    return messy


def make_messy_persona(rng: random.Random, idx: int, t0) -> Persona:
    """Same persona generator as the clean one — personas themselves
    don't need to be messy, only the surface text does."""
    return make_persona(rng, idx, t0)


# Tiny end-to-end demo used by the BEAM bench harness when `--messy` is
# passed — converts a clean persona dict into a messy one.
def messify_persona_dict(p: dict, rng: random.Random) -> dict:
    """Take a {user_id, text, facts} dict from the clean persona
    generator and produce a messified copy. Facts list is preserved
    (ground truth unchanged)."""
    return {
        "user_id": p["user_id"],
        "text": _messify_text(p["text"], rng),
        "facts": p["facts"],
    }


__all__ = [
    "MessyCorpus", "make_messy_persona", "messify_messages",
    "messify_persona_dict", "_messify_text",
    "SLANG_FILLERS", "TEXTSPEAK", "MISSPELLINGS", "STITCHERS",
    "MESSY_SMALLTALK",
]

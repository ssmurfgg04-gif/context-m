"""The 10 BEAM memory abilities — probe builders + deterministic judge.

Ability taxonomy follows BEAM (arXiv:2510.27246, Table 1):
Abstention, Contradiction Resolution, Event Ordering, Information
Extraction, Instruction Following, Knowledge Update, Multi-Hop
Reasoning, Preference Following, Summarization, Temporal Reasoning.

Scoring is context-sufficiency ("nugget") based: a probe is answered
iff the retrieved memory block contains the ground-truth nuggets an
LLM reader would need to answer correctly — the same philosophy as
BEAM's nugget design, with a deterministic judge so the whole harness
runs offline under the μ=0 protocol (zero LLM calls, including the
judge). A pluggable LLM judge/reader slot is documented in
bench/harness.py for canonical-protocol replication.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from context_m.util import month_name, normalize

ABILITIES = ["AB", "CR", "EO", "IE", "IF", "KU", "MH", "PF", "SZ", "TR"]
ABILITY_NAMES = {
    "AB": "Abstention", "CR": "Contradiction Resolution",
    "EO": "Event Ordering", "IE": "Information Extraction",
    "IF": "Instruction Following", "KU": "Knowledge Update",
    "MH": "Multi-Hop Reasoning", "PF": "Preference Following",
    "SZ": "Summarization", "TR": "Temporal Reasoning",
}

FACT_LINE = re.compile(r"^- \((.+?), ([\w]+), (.+?)\) \[valid ([^;→]+)→([^;]*?);", re.M)


@dataclass
class Probe:
    ability: str
    question: str
    user_id: str
    entity: str
    expected: dict = field(default_factory=dict)


def _current(items):
    """Latest still-valid item from a (value, start, end) timeline."""
    live = [it for it in items if it[2] is None]
    return live[-1] if live else items[-1]


def _prior(items, current):
    vals = [it for it in items if it[0] != current[0]]
    return vals[-1] if vals else None


# ---------------------------------------------------------------- builders
def build_probes(personas, rng) -> list[Probe]:
    probes: list[Probe] = []
    for p in personas:
        name = p.first
        cur_emp = _current(p.employers)
        prev_emp = _prior(p.employers, cur_emp)
        cur_city = _current(p.cities)
        prev_city = _prior(p.cities, cur_city)

        # --- IE: single-fact recall ------------------------------------
        m, d = p.birthday
        probes += [
            Probe("IE", q, p.user_id, p.full_name,
                  {"contains_any": [f"{month_name(m).lower()} {d}"]})
            for q in (f"What is {name}'s birthday?",
                      f"When was {name} born?")
        ]
        probes.append(Probe("IE", f"Who is {name}'s sister?", p.user_id,
                            p.full_name,
                            {"contains_any": [p.family[0][0].split()[0].lower()]}))
        probes.append(Probe("IE", f"What does {name} do for a living?",
                            p.user_id, p.full_name,
                            {"contains_any": [p.roles[0][0].lower()]}))
        probes.append(Probe("IE", f"What is {name}'s full name?",
                            p.user_id, p.full_name,
                            {"contains_any": [p.full_name.lower()]}))
        probes.append(Probe("IE", f"What does {name} do in their free time?",
                            p.user_id, p.full_name,
                            {"contains_any": [p.hobbies[0].lower()]}))

        # --- CR: contradiction resolution -------------------------------
        probes += [
            Probe("CR", q, p.user_id, p.full_name,
                  {"contains_any": [cur_emp[0].lower()]})
            for q in (f"Where does {name} work now?",
                      f"What is {name}'s current employer?",
                      f"Which company does {name} currently work at?")
        ]
        if prev_emp:
            probes.append(Probe("CR", f"Has {name} always worked at {prev_emp[0]}?",
                                p.user_id, p.full_name,
                                {"contains_any": [cur_emp[0].lower()],
                                 "also_any": [prev_emp[0].lower()]}))

        # --- EO: event ordering ------------------------------------------
        evs = sorted(p.events, key=lambda e: e[0])
        pairs = [(evs[i], evs[j]) for i in range(len(evs))
                 for j in range(i + 1, len(evs))][:6]
        for a, b in pairs:
            probes.append(Probe(
                "EO", f"Which happened first: {a[1]} or {b[1]}?",
                p.user_id, p.full_name,
                {"events": [(a[1], a[0]), (b[1], b[0])]}))

        # --- KU: knowledge update / supersession --------------------------
        if prev_emp:
            probes += [
                Probe("KU", f"Is {name} still working at {prev_emp[0]}?",
                      p.user_id, p.full_name,
                      {"current": cur_emp[0].lower(), "old": prev_emp[0].lower()})
                for _ in range(2)
            ]
        probes.append(Probe("KU", f"Where does {name} live these days?",
                            p.user_id, p.full_name,
                            {"contains_any": [cur_city[0].lower()]}))

        # --- MH: multi-hop ------------------------------------------------
        mgr, team = p.manager
        tname, tech = p.team_tech
        probes += [
            Probe("MH", q, p.user_id, p.full_name,
                  {"contains_any": [tech.lower()], "also_any": [mgr.lower()]})
            for q in (
                f"What programming language does the team of {name}'s manager use?",
                f"{name}'s manager leads a team — which language does that team use?",
            )
        ]

        # --- PF: preference following ------------------------------------
        by_cat: dict[str, list] = {}
        for cat, v, s, e in p.prefs:
            by_cat.setdefault(cat, []).append((v, s, e))
        for cat, items in by_cat.items():
            cur = _current(items)
            probes += [
                Probe("PF", q, p.user_id, p.full_name,
                      {"contains_any": [cur[0].lower()]})
                for q in (f"What {cat} does {name} prefer now?",
                          f"If you were picking {cat} for {name}, what would you choose?")
            ]

        # --- SZ: summarization (set F1) ----------------------------------
        probes.append(Probe(
            "SZ", f"List all the projects {name} has worked on.",
            p.user_id, p.full_name,
            {"set": [pr[0].lower() for pr in p.projects]}))

        # --- TR: temporal reasoning ----------------------------------------
        if prev_city:
            probes += [
                Probe("TR", f"Where did {name} live before {cur_city[0]}?",
                      p.user_id, p.full_name,
                      {"contains_any": [prev_city[0].lower()]})
            ]
        old_job = p.employers[0]
        if old_job[2]:
            probes.append(Probe(
                "TR", f"Where did {name} work in {old_job[1][:4]}?",
                p.user_id, p.full_name,
                {"contains_any": [old_job[0].lower()]}))
        ev = evs[0]
        probes.append(Probe(
            "TR", f"What did {name} do in {ev[0][:7].replace('-', ' ')}?",
            p.user_id, p.full_name,
            {"contains_any": [ev[1].lower()]}))

        # --- IF: instruction following -------------------------------------
        probes += [
            Probe("IF", q, p.user_id, p.full_name,
                  {"contains_any": [p.instruction[1].lower()]})
            for q in (f"In what language should you respond to {name}?"
                      if "french" in p.instruction[1].lower()
                      else f"How should answers to {name} be formatted?",
                      f"What standing instruction did {name} give you?")
        ]

        # --- AB: abstention (never-mentioned attributes) --------------------
        probes += [
            Probe("AB", q, p.user_id, p.full_name,
                  {"forbidden": [kw], "entity": p.full_name.lower()})
            for q, kw in (
                (f"What is {name}'s favorite podcast?", "podcast"),
                (f"What is {name}'s middle name?", "middle name"),
                (f"What is {name}'s blood type?", "blood"),
                (f"What is {name}'s zodiac sign?", "zodiac"),
            )
        ]
    return probes


# ---------------------------------------------------------------- judge
def _norm(s: str) -> str:
    return normalize(s)


def _contains(context: str, key: str) -> bool:
    k = _norm(key)
    return k in _norm(context) if k else False


def _content_words(s: str) -> set[str]:
    from context_m.text.tokenizer import content_words
    return set(content_words(s))


def _fuzzy_event_present(context: str, desc: str) -> bool:
    """Event description findable in context (word-overlap based)."""
    target = _content_words(desc)
    if not target:
        return False
    ctx_norm = _norm(context)
    if _norm(desc) in ctx_norm:
        return True
    # any context segment sharing >= half the content words
    for line in context.split("\n"):
        words = _content_words(line)
        if words and len(words & target) >= max(1, len(target) // 2):
            return True
    return False


def parse_fact_lines(context: str) -> list[dict]:
    out = []
    for m in FACT_LINE.finditer(context):
        out.append({"subject": m.group(1), "relation": m.group(2),
                    "value": m.group(3), "valid_from": m.group(4),
                    "valid_to": m.group(5).strip() or None})
    return out


def judge(probe: Probe, context: str) -> tuple[float, dict]:
    """Deterministic context-sufficiency judge. Returns (score, detail)."""
    exp = probe.expected
    facts = parse_fact_lines(context)

    if probe.ability == "AB":
        forbidden = exp["forbidden"]
        if any(kw in context.lower() for kw in forbidden):
            return 0.0, {"reason": f"fabricated/irrelevant '{forbidden}'"}
        # a 'likes/prefers' fact presented as an answer to the persona also fails
        ent_first = probe.entity.split()[0].lower()
        for f in facts:
            if (f["relation"] in ("likes", "prefers", "name", "alias")
                    and ent_first in f["subject"].lower()
                    and any(kw in f["value"].lower() for kw in forbidden)):
                return 0.0, {"reason": "offered unrelated fact as answer"}
        return 1.0, {"reason": "correct abstention"}

    if probe.ability == "EO":
        evs = exp["events"]
        ok = all(_fuzzy_event_present(context, d) for d, _ in evs)
        if ok:
            # verify the ORDERING note, if present, is correct
            note = re.search(r"ORDERING: (.+?) \((\d{4}-\d{2}-\d{2})\) happened before (.+?) \((\d{4}-\d{2}-\d{2})\)", context)
            if note:
                got = (note.group(2), note.group(4))
                want = (evs[0][1], evs[1][1])
                if got != want:
                    return 0.0, {"reason": "ordering note inverted"}
            return 1.0, {"reason": "both events with dates present"}
        return 0.0, {"reason": "event(s) missing from context"}

    if probe.ability == "KU" and "current" in exp:
        cur, old = exp["current"], exp["old"]
        has_cur = _contains(context, cur)
        has_left = any(f["relation"] == "left" and _contains(f["value"], old)
                       for f in facts)
        has_expired = any(
            f["relation"] in ("works_at", "lives_in") and _contains(f["value"], old)
            and f["valid_to"] and f["valid_to"] not in ("∞", "")
            for f in facts)
        if has_cur and (has_left or has_expired):
            return 1.0, {"reason": "current value + supersession evidence"}
        if has_cur:
            return 0.5, {"reason": "current value only"}
        return 0.0, {"reason": "missing current value"}

    if probe.ability == "SZ":
        want = set(exp["set"])
        got = set()
        for f in facts:
            for w in want:
                if _contains(f["value"], w) or _contains(f["subject"], w):
                    got.add(w)
        # also allow raw mentions anywhere in the context block
        for w in want:
            if _contains(context, w):
                got.add(w)
        if not want:
            return 1.0, {"reason": "empty set"}
        inter = len(got & want)
        prec = inter / len(got) if got else 0.0
        rec = inter / len(want)
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        return (1.0 if f1 >= 0.5 else 0.0), {"f1": round(f1, 3),
                                              "retrieved": sorted(got)}

    # containment-based abilities: IE / CR / MH / PF / TR / IF
    keys = exp.get("contains_any", [])
    also = exp.get("also_any", [])
    if not keys or any(_contains(context, k) for k in keys):
        if also and not any(_contains(context, k) for k in also):
            return 0.5, {"reason": "primary nugget only"}
        return 1.0, {"reason": "nugget(s) present"}
    return 0.0, {"reason": "nugget missing"}

"""Out-of-distribution (OOD) benchmark — breaking the circularity.

The in-distribution (ID) benchmark generates conversations from the SAME
template families the μ=0 extractor's patterns were authored against, so ID
scores are an upper bound for template-shaped text, not a capability claim.
This module measures the honest generalization gap:

  1. EXTRACTION RECALL per style — ground-truth facts from persona
     registries are re-rendered by an independent LLM in styles the pattern
     author never saw (paraphrase / negation / indirect / informal /
     non_english / code_switch). The μ=0 extractor runs on the renderings;
     recall is matched against the ground-truth match keys.
  2. END-TO-END RETRIEVAL — the OOD corpus (renderings + the same
     distractor machinery) flows through the full memory fabric and is
     probed by the SAME probe builder + judges as the ID benchmark, so
     ID-vs-OOD deltas are apples-to-apples.
  3. LLM-JUDGE CROSS-CHECK — probe/context pairs are exported in the
     canonical BEAM judge format for independent LLM grading.

Renderer omissions (facts the LLM failed to convey) are tracked separately
from extraction failures so neither layer can silently blame the other.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from cortexm.bench.abilities import ABILITIES, build_probes, judge
from cortexm.bench.generator import (Corpus, distractor_paragraph,
                                       smalltalk_message)
from cortexm.util import month_name, normalize, token_estimate

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)

# Match-tier vocabulary: how hard each fact is to extract from re-phrased text.
CATEGORY_TIERS = {
    "core": "explicit entity facts (name, employer, city, family, prefs)",
    "relational": "multi-hop chains (manager->team->tech)",
    "temporal": "dated events and interval changes",
}


def _d(y, m, day=1):
    return f"{y:04d}-{m:02d}-{day:02d}"


def _mn(iso_date: str) -> str:
    return month_name(int(iso_date[5:7]))


# ---------------------------------------------------------------- manifest
def export_manifest(personas: list, t0: datetime = T0) -> dict:
    """Persona ground truth as LLM-renderable fact manifests.

    Mirrors the generator's session structure (parts 0-7) so the SAME probes
    remain answerable; facts are stated as semantic text, never as the
    generator's template phrasings.
    """
    out = []
    for p in personas:
        e0, e1 = p.employers[0], p.employers[1]
        e_last = p.employers[-1]
        c0, c1 = p.cities
        sister_full = p.family[0][0]
        sessions: list[dict] = []

        def S(part: int, facts: list[dict]) -> None:
            date = t0 + timedelta(days=part * 21)
            sessions.append({"session": part, "date": date.date().isoformat(),
                             "facts": [
                                 {"id": f"s{part}f{i}", "text": t,
                                  "type": ty, "match": m}
                                 for i, (ty, t, m) in enumerate(facts)]})

        # ---- part 0: introduction
        f0 = [
            ("name", f"The user's full name is {p.full_name}.",
             [p.full_name, p.first]),
            ("employment", f"The user works at {e0[0]} as a {p.roles[0][0]}.",
             [e0[0]]),
            ("city", f"The user lives in {c0[0]}.", [c0[0]]),
            ("birthday",
             f"The user's birthday is {month_name(p.birthday[0])} "
             f"{p.birthday[1]}.",
             [f"{month_name(p.birthday[0]).lower()} {p.birthday[1]}"]),
            ("family",
             f"The user has a sister named {sister_full}.",
             [sister_full.split()[0]]),
        ]
        if p.nickname:
            f0.append(("alias", f"The user goes by the nickname "
                       f"\"{p.nickname}\".", [p.nickname]))
        _instr_keys = (["french"] if "french" in p.instruction[1].lower()
                       else ["short", "concise", "brief"])
        f0.append(("instruction",
                   f"The user gave the assistant a standing instruction: "
                   f"\"{p.instruction[0]}\"", _instr_keys))
        f0.append(("employment_since",
                   f"The user has been at {e0[0]} since {_mn(e0[1])} {e0[1][:4]}.",
                   [e0[0]]))
        S(0, f0)

        # ---- part 1: preferences + skills
        f1 = []
        for i in range(0, len(p.prefs), 2):
            cat, v_old = p.prefs[i][0], p.prefs[i][1]
            v_new = p.prefs[i + 1][1] if i + 1 < len(p.prefs) else v_old
            f1.append(("preference",
                       f"For {cat}, the user used to like {v_old} but now "
                       f"prefers {v_new}.", [v_old, v_new]))
        for s in p.skills:
            f1.append(("skill", f"The user knows {s}.", [s]))
        f1.append(("hobby", f"In their free time the user enjoys "
                   f"{p.hobbies[0]}.", [p.hobbies[0]]))
        if len(p.hobbies) > 1:
            f1.append(("hobby", f"The user also enjoys {p.hobbies[1]}.",
                       [p.hobbies[1]]))
        S(1, f1)

        # ---- part 2: job change
        f2 = []
        m_end = int(e0[2][5:7]) if e0[2] else 6
        f2.append(("left_job",
                   f"The user left {e0[0]} in {_mn(_d(2024, m_end))} "
                   f"{e0[2][:4] if e0[2] else ''}.".strip(), [e0[0]]))
        m_new = int(e1[1][5:7])
        f2.append(("joined_job",
                   f"The user joined {e1[0]} in {_mn(e1[1])} {e1[1][:4]} "
                   f"as a {p.roles[0][0]}.", [e1[0]]))
        if len(p.employers) > 2:
            mid = p.employers[1]
            m_mid = int(mid[2][5:7]) if mid[2] else 6
            f2.append(("left_job",
                       f"The user later left {mid[0]} in {_mn(_d(2024, m_mid))} "
                       f"{mid[2][:4] if mid[2] else ''}.".strip(), [mid[0]]))
            f2.append(("employment",
                       f"These days the user works at {e_last[0]}.",
                       [e_last[0]]))
        S(2, f2)

        # ---- part 3: relocation + family
        m_move = int(c1[1][5:7])
        f3 = [
            ("moved",
             f"The user moved to {c1[0]} in {_mn(c1[1])} {c1[1][:4]}, "
             f"previously living in {c0[0]}.", [c1[0], c0[0]]),
        ]
        S(3, f3)

        # ---- part 4: work structure (multi-hop)
        mgr, team = p.manager
        tname, tech = p.team_tech
        S(4, [
            ("manager", f"The user's manager is {mgr}.", [mgr]),
            ("manager_team", f"{mgr} manages the {tname} team.", [tname]),
            ("team_tech", f"The {tname} team uses {tech}.", [tech]),
            ("on_team", f"The user is on the {tname} team.", [tname]),
        ])

        # ---- part 5: projects
        f5 = []
        for name, start, end in p.projects:
            if end:
                f5.append(("project",
                           f"The user worked on {name}, finished in "
                           f"{_mn(end)} {end[:4]}.", [name]))
            else:
                f5.append(("project",
                           f"The user is currently working on {name}.", [name]))
        S(5, f5)

        # ---- parts 6/7: dated events + preference flip
        f6 = []
        for date, desc in p.events[:2]:
            f6.append(("event",
                       f"On {month_name(int(date[5:7]))} {int(date[8:10])}, "
                       f"{date[:4]}, the user {desc}.", [desc]))
        S(6, f6)
        f7 = []
        for date, desc in p.events[2:]:
            f7.append(("event",
                       f"On {month_name(int(date[5:7]))} {int(date[8:10])}, "
                       f"{date[:4]}, the user {desc}.", [desc]))
        cat = p.prefs[2][0] if len(p.prefs) > 2 else "coffee"
        vals = [v for (c, v, s, e) in p.prefs if c == cat]
        if len(vals) >= 2:
            f7.append(("preference",
                       f"The user has since switched to {vals[-1]} for {cat}.",
                       [vals[-1]]))
        S(7, f7)

        out.append({"user_id": p.user_id, "full_name": p.full_name,
                    "sessions": sessions})
    return {"personas": out}


# ------------------------------------------------------------ corpus build
def build_ood_corpus(rendered: dict, persona, t0: datetime = T0,
                     target_tokens: int = 120_000, seed: int = 7) -> Corpus:
    """Assemble an evaluation Corpus from one rendered persona-style row."""
    rng = random.Random(seed)
    sessions = []
    total = 0
    for s in sorted(rendered.get("sessions", []), key=lambda x: x.get("session", 0)):
        date = t0 + timedelta(days=int(s.get("session", 0)) * 21
                              + rng.randrange(0, 5))
        msgs = [("user", str(t)) for t in s.get("messages", []) if str(t).strip()]
        for _, txt in msgs:
            total += token_estimate(txt)
        sessions.append((persona.user_id, date, msgs))
    # distractor volume — same machinery as the ID generator
    guard = 0
    while total < target_tokens and guard < 200_000:
        guard += 1
        uid, date, msgs = sessions[rng.randrange(len(sessions))]
        if rng.random() < 0.45:
            txt = distractor_paragraph(rng)
        else:
            txt = smalltalk_message(rng) + " " + smalltalk_message(rng)
        k = rng.randrange(0, max(1, len(msgs)))
        msgs.insert(k, ("user", txt))
        total += token_estimate(txt)
    return Corpus(bucket="ood", target_tokens=target_tokens, sessions=sessions,
                  personas=[persona], total_tokens=total,
                  generation_seconds=0.0)


# ------------------------------------------------- extraction-layer recall
def extraction_recall(rendered: dict, persona, config) -> dict:
    """Run the μ=0 extractor over one rendered persona-style row.

    Returns per-fact match results against the manifest match keys — the
    direct, honest measure of pattern generalization.
    """
    from cortexm.bridge.extractor import Extractor
    from cortexm.bridge.patterns import ExtractionContext

    manifest = export_manifest([persona], T0)["personas"][0]
    manifest_facts = {f["id"]: f for s in manifest["sessions"]
                      for f in s["facts"]}
    extractor = Extractor(config)
    candidates = []
    name = None
    for s in sorted(rendered.get("sessions", []),
                    key=lambda x: x.get("session", 0)):
        ts = T0 + timedelta(days=int(s.get("session", 0)) * 21)
        for text in s.get("messages", []):
            ctx = ExtractionContext(user_id=persona.user_id, ts=ts,
                                    speaker="user", subject_name=name,
                                    lexicon=set())
            try:
                cands = extractor.extract(str(text), ctx)
            except Exception:
                cands = []
            candidates.extend(cands)
            for c in cands:
                if c.relation == "name" and name is None:
                    name = c.value
    blob = " \n ".join(
        f"{normalize(c.subject)} | {normalize(c.relation)} | {normalize(c.value)}"
        for c in candidates if c.pattern != "mention_fallback")
    matches = {}
    for fid, f in manifest_facts.items():
        hit = any(normalize(k) and normalize(k) in blob for k in f["match"])
        matches[fid] = {"hit": hit, "type": f["type"]}
    conveyed = set(rendered.get("conveyed", []))
    total = hit = 0
    per_type: dict[str, list[int]] = {}
    for fid, r in matches.items():
        if fid not in conveyed:
            continue  # renderer omitted it — not an extraction failure
        total += 1
        hit += int(r["hit"])
        per_type.setdefault(r["type"], [0, 0])
        per_type[r["type"]][1] += 1
        per_type[r["type"]][0] += int(r["hit"])
    return {
        "user_id": persona.user_id,
        "recall": round(hit / total, 4) if total else None,
        "n_ground_truth": total,
        "n_renderer_omitted": len(manifest_facts) - len(conveyed & set(manifest_facts)),
        "n_candidates": len([c for c in candidates
                             if c.pattern != "mention_fallback"]),
        "per_type": {k: round(v[0] / v[1], 4) for k, v in per_type.items() if v[1]},
        "missed": [fid for fid, r in matches.items()
                   if not r["hit"] and fid in conveyed],
        "definition": "entity-level recall: share of ground-truth facts "
                       "whose key appears in any non-fallback candidate; "
                       "temporal precision is measured end-to-end by the "
                       "TR/EO probes, not here",
    }


# ------------------------------------------------------- end-to-end eval
@dataclass
class OODResult:
    style: str
    overall: float = 0.0
    per_ability: dict = field(default_factory=dict)
    n_questions: int = 0
    ingest: dict = field(default_factory=dict)
    extraction: dict = field(default_factory=dict)
    details: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def run_ood_eval(corpus: Corpus, personas: list, style: str,
                 db_path: str = ":memory:", max_probes: int | None = None,
                 judge_fn=None, enrich_fn=None) -> OODResult:
    """Ingest an OOD corpus and evaluate with the standard probe/judge pair.

    With ``enrich_fn(memory) -> report`` the probe set is evaluated twice —
    before and after async LLM enrichment — quantifying the graceful-
    degradation fallback's recovery on OOD text.
    """
    import time

    from cortexm import metrics
    from cortexm.api.memory import Memory
    from cortexm.config import Config

    t0 = time.time()
    res = OODResult(style=style)
    cfg = Config(db_path=db_path) if db_path != ":memory:" else Config()
    cfg.apply_rules_each_add = False
    memory = Memory(cfg)
    metrics.reset_counters()

    t_ing = time.time()
    n_msgs = 0
    for user_id, date, msgs in corpus.sessions:
        payload = [{"role": role, "content": text, "timestamp": date}
                   for role, text in msgs]
        n_msgs += len(msgs)
        memory.add(payload, user_id=user_id, timestamp=date)
    memory.apply_rules()
    ingest_s = time.time() - t_ing
    stats = memory.stats()
    res.ingest = {
        "wall_seconds": round(ingest_s, 2),
        "messages": n_msgs,
        "tokens": corpus.total_tokens,
        "tokens_per_second": int(corpus.total_tokens / max(ingest_s, 1e-9)),
        "llm_calls": metrics.counters()["llm_calls"],
        "u0_protocol": stats["u0_protocol"],
        "facts": stats["facts"],
    }

    def _eval() -> tuple[float, dict, list]:
        rng = random.Random(hash((style, "probes")) & 0xFFFFFFFF)
        probes = build_probes(personas, rng)
        by_ability: dict[str, list] = {a: [] for a in ABILITIES}
        for p in probes:
            by_ability[p.ability].append(p)
        if max_probes:
            for a in ABILITIES:
                by_ability[a] = by_ability[a][:max_probes]
        n_q = sum(len(v) for v in by_ability.values())
        score_fn = judge_fn or judge
        per_ability = {a: 0.0 for a in ABILITIES}
        counts = {a: len(by_ability[a]) for a in ABILITIES}
        details = []
        for ability in ABILITIES:
            for probe in by_ability[ability]:
                out = memory.search(probe.question, user_id=probe.user_id, k=12)
                score, detail = score_fn(probe, out["context_block"])
                per_ability[ability] += score
                details.append({"ability": ability, "question": probe.question,
                                "score": score, "detail": detail,
                                "context": out["context_block"][:600]})
        overall = round(sum(per_ability.values()) / max(n_q, 1), 4)
        per_ability = {a: round(per_ability[a] / max(counts[a], 1), 4)
                       for a in ABILITIES if counts[a]}
        return overall, per_ability, details

    res.overall, res.per_ability, res.details = _eval()
    res.n_questions = len(res.details)
    res.extraction = {"facts": stats["facts"]}

    if enrich_fn is not None:
        rep = enrich_fn(memory)
        res.ingest["enrichment"] = rep
        post_overall, post_per_ability, _post = _eval()
        res.enriched = {"overall": post_overall,
                        "per_ability": post_per_ability,
                        "report": rep}  # type: ignore[attr-defined]

    memory.close()
    res.ingest["wall_seconds_total"] = round(time.time() - t0, 2)
    return res


# ---------------------------------------------------- LLM-judge item export
def describe_expected(probe) -> list[str]:
    """Human-readable nugget description for the canonical LLM judge."""
    exp = probe.expected
    if probe.ability == "AB":
        return [f"NOTHING — '{probe.question}' concerns an attribute never "
                f"mentioned; correct behaviour is abstention."]
    if "events" in exp:
        return [f"Event '{d}' occurred on {iso}." for d, iso in exp["events"]]
    if "set" in exp:
        return ["The complete set of projects: " + ", ".join(exp["set"]) + "."]
    if "current" in exp and "old" in exp:
        return [f"Current employer: {exp['current']}.",
                f"Previous employer (superseded): {exp['old']}."]
    out = []
    for k in exp.get("contains_any", []):
        out.append(f"The answer must mention: {k}.")
    for k in exp.get("also_any", []):
        out.append(f"Supporting evidence should also mention: {k}.")
    return out or ["(no nugget description)"]


def export_judge_items(result: OODResult, personas: list,
                       per_ability_limit: int = 4) -> list[dict]:
    """Probe/context pairs in the canonical judge JSONL format.

    The deterministic judge's score is attached (det_score) so agreement
    between the two graders can be computed after the LLM pass.
    """
    items = []
    per_ability: dict[str, int] = {}
    probe_by_q = {pr.question: pr for pr in build_probes(personas, random.Random(1234))}
    for i, d in enumerate(result.details):
        if per_ability.get(d["ability"], 0) >= per_ability_limit:
            continue
        per_ability[d["ability"]] = per_ability.get(d["ability"], 0) + 1
        probe = probe_by_q.get(d["question"])
        items.append({
            "id": f"{result.style}-{i}",
            "ability": d["ability"],
            "question": d["question"],
            "expected": describe_expected(probe) if probe
                        else ["(probe metadata unavailable)"],
            "context": d["context"],
            "det_score": d["score"],
        })
    return items

"""BEAM-style corpus generator — synthetic long-horizon conversations.

Mirrors the BEAM methodology (arXiv:2510.27246): auto-generated,
coherent, topically diverse multi-session conversations with
probing questions across 10 memory abilities. Deterministic (seeded);
persona timelines carry ground-truth registries used by the judge.

Buckets: 128K / 500K / 1M / 10M estimated tokens. Signal conversations
(persona sessions) are embedded in distractor noise (smalltalk +
long-form topical documents with competing capitalized entities) so the
answers are genuine needles — retrieval must beat brute-force context
stuffing, exactly the regime BEAM-10M targets.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from cortexm.util import month_name, token_estimate

FIRST_NAMES = ["Alice", "Maya", "Priya", "Dana", "Elena", "Marcus", "Tom",
               "Nadia", "Omar", "Julia", "Ken", "Sofia", "Ravi", "Chloe"]
LAST_NAMES = ["Johnson", "Chen", "Sharma", "Kovač", "Rossi", "Williams",
              "Tanaka", "Silva", "Novak", "Brooks", "Garcia", "Osei"]
ORGS = ["Google", "Anthropic", "Stripe", "Microsoft", "Netflix", "Shopify",
        "Figma", "Databricks", "Vercel", "Ramp", "Notion", "Linear"]
CITIES = ["Toronto", "Lisbon", "Austin", "Berlin", "Nairobi", "Seattle",
          "Denver", "Prague", "Osaka", "Melbourne", "Zurich", "Austin"]
ROLES = ["software engineer", "product manager", "data scientist",
         "designer", "engineering manager", "researcher", "DevOps engineer"]
TECH = ["Rust", "Python", "Go", "TypeScript", "Kotlin", "Swift"]
TEAMS = ["Platform", "Search", "Payments", "Growth", "Infra", "Mobile"]
PROJECTS = ["Project Falcon", "Project Atlas", "Project Beacon", "Project Cedar",
            "Project Delta", "Project Echo", "Project Fable", "Project Granite"]
EVENTS = [("deployed the payment service", 3), ("ran the marathon", 8),
          ("adopted a cat", 2), ("spoke at a conference", 6),
          ("rebuilt the CI pipeline", 4), ("started piano lessons", 5),
          ("organized a hackathon", 9), ("published a blog post", 3)]
PREF_CATS = [
    ("coffee", ["espresso", "oat milk lattes", "cold brew", "green tea",
                "black coffee", "matcha"]),
    ("music", ["jazz", "techno", "classical", "indie rock", "lo-fi beats"]),
    ("food", ["ramen", "pizza", "thai curry", "sushi", "tacos"]),
    ("editor theme", ["dark mode", "light mode"]),
]
HOBBIES = ["hiking", "rock climbing", "photography", "baking sourdough",
           "kayaking", "chess", "birdwatching", "pottery"]
DISTRACTOR_TOPICS = [
    "the weather", "a traffic jam", "a new smartphone release", "the stock market",
    "a soccer match", "a Netflix series", "a recipe for lasagna", "airport delays",
    "a coffee shop queue", "a podcast about history", "a piano concert",
    "a neighbor's garden", "a marathon on TV", "a book about sailing",
]
WIKI_ENTITIES = [
    "Mount Kilimanjaro", "the Baltic Sea", "Kafka Museum", "Antarctic Treaty",
    "Hyperloop One", "Voyager Program", "Silk Road", "Lake Baikal",
    "Gutenberg Press", "Mariana Trench", "Aurora Borealis", "Great Barrier Reef",
    "Florence Cathedral", "Sahara Desert", "Yellowstone Park", "Aztec Calendar",
]
WIKI_VERBS = ["was documented", "attracted researchers", "made headlines",
              "was rediscovered", "inspired a documentary", "broke records",
              "was surveyed", "hosted a festival", "was renovated",
              "surprised scientists"]
SMALLTALK = [
    "Hey! How's it going today?",
    "That sounds interesting, tell me more.",
    "Oh wow, I didn't know that.",
    "Thanks for the reminder!",
    "Ha, that's funny.",
    "Okay, got it.",
    "Sounds like a plan.",
    "Good morning! Ready for another day?",
    "I was just thinking about something similar.",
    "Cool, I'll keep that in mind.",
]


@dataclass
class Persona:
    user_id: str
    full_name: str
    first: str
    nickname: str | None
    employers: list = field(default_factory=list)   # (org, start, end|None)
    roles: list = field(default_factory=list)
    cities: list = field(default_factory=list)      # (city, start, end|None)
    prefs: list = field(default_factory=list)       # (cat, value, start, end|None)
    skills: list = field(default_factory=list)
    family: list = field(default_factory=list)      # (name, relation)
    manager: tuple | None = None                    # (name, team)
    team_tech: tuple | None = None                  # (team, tech)
    projects: list = field(default_factory=list)    # (name, start, end|None)
    events: list = field(default_factory=list)      # (date, desc)
    birthday: tuple | None = None
    hobbies: list = field(default_factory=list)
    instruction: tuple | None = None                # (text, expected)


def _d(y: int, m: int, day: int = 1) -> str:
    return f"{y:04d}-{m:02d}-{day:02d}"


def make_persona(rng: random.Random, idx: int, t0: datetime) -> Persona:
    first = FIRST_NAMES[(idx * 5 + rng.randrange(3)) % len(FIRST_NAMES)]
    last = LAST_NAMES[(idx * 7 + rng.randrange(3)) % len(LAST_NAMES)]
    full = f"{first} {last}"
    p = Persona(user_id=f"user{idx}", full_name=full, first=first,
                nickname=first[:3].lower() if rng.random() < 0.5 else None)

    # employment timeline: 2-3 orgs
    n_jobs = rng.choice([2, 3])
    orgs = rng.sample(ORGS, n_jobs + 1)
    y = t0.year - 2
    start = _d(y, rng.randrange(1, 10))
    for i in range(n_jobs):
        end = _d(y + 1 + i, rng.randrange(1, 12)) if i < n_jobs - 1 else None
        p.employers.append((orgs[i], start, end))
        start = end or start
    p.roles.append((rng.choice(ROLES), p.employers[0][1], None))

    # residence: 2 cities
    cs = rng.sample(CITIES, 2)
    p.cities = [(cs[0], _d(t0.year - 2, rng.randrange(1, 10)),
                 _d(t0.year - 1, rng.randrange(1, 12))),
                (cs[1], _d(t0.year - 1, rng.randrange(1, 12)), None)]

    # preferences with flips
    for cat, vals in PREF_CATS[:rng.choice([2, 3])]:
        v = rng.sample(vals, min(2, len(vals)))
        p.prefs.append((cat, v[0], _d(t0.year - 2, rng.randrange(1, 12)),
                        _d(t0.year - 1, rng.randrange(1, 12))))
        p.prefs.append((cat, v[1], _d(t0.year - 1, rng.randrange(1, 12)), None))

    p.skills = rng.sample(TECH, 3)
    sib = FIRST_NAMES[(idx * 3 + 7) % len(FIRST_NAMES)]
    sib_last = LAST_NAMES[(idx * 11 + 5) % len(LAST_NAMES)]
    p.family = [(f"{sib} {sib_last}", "sister")]
    mgr = FIRST_NAMES[(idx * 9 + 2) % len(FIRST_NAMES)]
    if mgr == first:
        mgr = FIRST_NAMES[(idx * 9 + 3) % len(FIRST_NAMES)]
    team = rng.choice(TEAMS)
    p.manager = (mgr, team)
    p.team_tech = (team, rng.choice(TECH))
    p.projects = [(proj, _d(t0.year - 1 + i % 2, rng.randrange(1, 12)),
                   None if i % 2 else _d(t0.year, rng.randrange(1, 6)))
                  for i, proj in enumerate(rng.sample(PROJECTS, 3))]
    evs = rng.sample(EVENTS, 4)
    p.events = [(_d(t0.year - 1 + i % 2, rng.randrange(1, 12), rng.randrange(1, 28)),
                 ev[0]) for i, ev in enumerate(evs)]
    p.birthday = (rng.randrange(1, 13), rng.randrange(1, 28))
    p.hobbies = rng.sample(HOBBIES, 2)
    p.instruction = ("Please always respond in French.", "French") \
        if rng.random() < 0.6 else ("Always keep my answers short and concise.", "short")
    return p


def _month(m: int) -> str:
    return month_name(m)


def persona_messages(p: Persona, rng: random.Random, session_date: datetime,
                     part: int) -> list[tuple[str, str]]:
    """Surface the persona's life in natural, varied messages."""
    msgs: list[tuple[str, str]] = []
    A = lambda t: msgs.append(("user", t))  # noqa: E731

    if part == 0:
        # introduction session
        A(f"My name is {p.full_name}.")
        A(rng.choice([
            f"I work at {p.employers[0][0]} as a {p.roles[0][0]}.",
            f"I'm a {p.roles[0][0]} at {p.employers[0][0]}.",
            f"I work as a {p.roles[0][0]} at {p.employers[0][0]}.",
        ]))
        A(f"I live in {p.cities[0][0]}.")
        b = p.birthday
        A(f"My birthday is {_month(b[0])} {b[1]}.")
        A(f"My sister {p.family[0][0]} lives nearby.")
        if p.nickname:
            A(f"But call me {p.nickname.capitalize() if len(p.nickname) > 2 else p.nickname}.")
        A(p.instruction[0])
        # state the first employer WITH its start date so the employment
        # interval is recoverable from the text (TR "where did X work in
        # YYYY" probes); otherwise valid_from defaults to the session date
        # and the historical window is unanswerable from the corpus.
        _e0 = p.employers[0]
        A(f"I've been working at {_e0[0]} since "
          f"{_month(int(_e0[1][5:7]))} {_e0[1][:4]}.")
    if part == 1:
        # preferences + skills
        # state EVERY preference category (old -> new), so every PF probe
        # is answerable from the conversation; part 7's "switched to X"
        # then acts as a genuine re-statement / flip of the current value.
        for i in range(0, len(p.prefs), 2):
            cat, v_old = p.prefs[i][0], p.prefs[i][1]
            v_new = p.prefs[i + 1][1] if i + 1 < len(p.prefs) else v_old
            A(f"I prefer {v_new} over {v_old} for {cat}.")
        for s in p.skills:
            A(rng.choice([f"I know {s}.", f"I code in {s}.",
                          f"I've been learning {s}."]))
        A(f"In my free time I {p.hobbies[0]}.")
    if part == 2:
        # job change
        old = p.employers[0]
        new = p.employers[1]
        m_end = int(old[2][5:7]) if old[2] else 6
        A(f"I left {old[0]} in {_month(m_end)}.")
        m_new = int(new[1][5:7])
        A(rng.choice([
            f"I joined {new[0]} on {_month(m_new)} 5th, {new[1][:4]}.",
            f"I started working at {new[0]} in {_month(m_new)} {new[1][:4]}.",
            f"I'm now at {new[0]} as a {p.roles[0][0]}.",
        ]))
        if len(p.employers) > 2:
            mid, last = p.employers[1], p.employers[2]
            m_mid = int(mid[2][5:7]) if mid[2] else 6
            A(f"I left {mid[0]} in {_month(m_mid)} {mid[2][:4]}.")
            A(f"These days I work at {last[0]}.")
    if part == 3:
        # move + family third-person
        c_old, c_new = p.cities[0], p.cities[1]
        m_move = int(c_new[1][5:7])
        A(f"We moved to {c_new[0]} in {_month(m_move)} {c_new[1][:4]}.")
        A(f"My sister {p.family[0][0]} works at {rng.choice(ORGS)}.")
    if part == 4:
        # work structure: manager + team + tech (multi-hop chain)
        mgr, team = p.manager
        tname, tech = p.team_tech
        A(f"My manager is {mgr}.")
        A(f"{mgr} manages the {tname} team.")
        A(f"The {tname} team uses {tech} for everything.")
        A(f"I'm on the {tname} team now.")
    if part == 5:
        # projects
        for name, start, end in p.projects:
            if end:
                m = int(end[5:7])
                A(rng.choice([
                    f"We shipped {name} in {_month(m)} {end[:4]}.",
                    f"I finished {name} last month.",
                ]))
            else:
                A(rng.choice([
                    f"I'm working on {name}.",
                    f"We're building {name} right now.",
                    f"I work on {name} with a few friends.",
                ]))
    if part == 6:
        # dated events
        for date, desc in p.events[:2]:
            m, day = int(date[5:7]), int(date[8:10])
            A(rng.choice([
                f"On {_month(m)} {day}, {date[:4]} I {desc}.",
                f"I {desc} on {_month(m)} {day}, {date[:4]}.",
            ]))
        A(rng.choice(SMALLTALK))
    if part == 7:
        for date, desc in p.events[2:]:
            m, day = int(date[5:7]), int(date[8:10])
            A(rng.choice([
                f"On {_month(m)} {day}, {date[:4]}, I {desc}.",
                f"I {desc} on {_month(m)} {day}, {date[:4]}.",
            ]))
        # preference flip
        cat = p.prefs[2][0] if len(p.prefs) > 2 else "coffee"
        vals = [v for (c, v, s, e) in p.prefs if c == cat]
        if len(vals) >= 2:
            A(rng.choice([
                f"Actually, I've switched to {vals[-1]}.",
                f"These days I prefer {vals[-1]}.",
                f"I'm more of a {vals[-1]} person now.",
            ]))
    # conversational padding
    for _ in range(rng.randrange(1, 3)):
        A(rng.choice(SMALLTALK))
    return msgs


def distractor_paragraph(rng: random.Random) -> str:
    """Long-form topical distractor with competing capitalized entities."""
    e1, e2 = rng.sample(WIKI_ENTITIES, 2)
    verb1, verb2 = rng.sample(WIKI_VERBS, 2)
    year = rng.randrange(1960, 2024)
    topic = rng.choice(DISTRACTOR_TOPICS)
    return (
        f"I read an article about {e1} yesterday. Apparently {e1} {verb1} in {year}, "
        f"and researchers compared it with {e2}, which {verb2} a decade earlier. "
        f"The article also covered {topic}, and mentioned that {e2} remains a popular "
        f"subject among historians. A guidebook author wrote that visiting {e1} takes "
        f"about three days, while {e2} can be explored in an afternoon. "
        f"Local officials say tourism around {e1} doubled since {year + 10}."
    )


def smalltalk_message(rng: random.Random) -> str:
    return rng.choice(SMALLTALK)


@dataclass
class Corpus:
    bucket: str
    target_tokens: int
    sessions: list = field(default_factory=list)   # (user_id, date, messages)
    personas: list = field(default_factory=list)
    total_tokens: int = 0
    generation_seconds: float = 0.0


BUCKETS = {
    "128k": dict(target=128_000, personas=1, sessions=8, docs_factor=0.9),
    "500k": dict(target=500_000, personas=2, sessions=12, docs_factor=1.0),
    "1m": dict(target=1_000_000, personas=3, sessions=16, docs_factor=1.0),
    "10m": dict(target=10_000_000, personas=6, sessions=40, docs_factor=1.0),
}


def generate(bucket: str, seed: int = 42, t0: datetime | None = None) -> Corpus:
    import time
    t_start = time.time()
    cfg = BUCKETS[bucket]
    rng = random.Random(seed)
    t0 = t0 or datetime(2026, 3, 1, tzinfo=timezone.utc)
    personas = [make_persona(rng, i, t0) for i in range(cfg["personas"])]
    sessions = []
    total = 0

    n_sessions = cfg["sessions"]
    for p in personas:
        for s in range(n_sessions):
            date = t0 + timedelta(days=s * 21 + rng.randrange(0, 5))
            msgs = persona_messages(p, rng, date, s % 8)
            if s == 0:
                msgs = [("assistant", "Hi! I'm your assistant. Nice to meet you!")] + msgs
            for r, txt in msgs:
                total += token_estimate(txt)
            sessions.append((p.user_id, date, msgs))

    # distractor volume to reach the bucket target (measured, not estimated)
    if cfg["target"] - total > 0:
        step = max(1, len(sessions) // 24)
        guard = 0
        while total < cfg["target"] and guard < 500_000:
            guard += 1
            made = 0
            for i in range(0, len(sessions), step):
                if total >= cfg["target"]:
                    break
                uid, date, msgs = sessions[i]
                k = rng.randrange(0, max(1, len(msgs)))
                if rng.random() < 0.45:
                    txt = distractor_paragraph(rng)
                else:
                    txt = smalltalk_message(rng) + " " + smalltalk_message(rng)
                msgs.insert(k, ("user", txt))
                total += token_estimate(txt)
                made += 1
            if made == 0:
                step = max(1, step - 1)
            elif guard % 5 == 0 and step > 1:
                step = max(1, step // 2)
            # occasionally append pure-noise sessions
            if rng.random() < 0.15:
                batch = []
                for _ in range(rng.randrange(6, 14)):
                    txt = (distractor_paragraph(rng) if rng.random() < 0.5
                           else smalltalk_message(rng) + " " + smalltalk_message(rng))
                    batch.append(("user", txt))
                    total += token_estimate(txt)
                sessions.append((sessions[rng.randrange(len(sessions))][0],
                                 t0 + timedelta(days=rng.randrange(400)), batch))

    return Corpus(bucket=bucket, target_tokens=cfg["target"], sessions=sessions,
                  personas=personas, total_tokens=total,
                  generation_seconds=time.time() - t_start)

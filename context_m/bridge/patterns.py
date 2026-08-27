"""The μ=0 extraction pattern library.

High-precision syntactic patterns over Subject-Relation-Value triples:
first-person (user), third-person (entities), and assistant-turn
(second-person) forms, with temporal anchors resolved by bridge.dates.
Zero LLM calls — this is the deterministic perception layer that makes
BEAM-honest μ=0 ingest possible while competitors burn LLM extraction
at write time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from context_m.bridge.dates import find_dates

# ---------------------------------------------------------------- helpers

# Case-sensitive (scoped — patterns compile with re.I for verbs, but
# entity values must keep their capitalization semantics): first word
# capitalized, subsequent words capitalized or internal to the name.
ORG = r"(?-i:[A-Z][\w&'.,-]*)(?:\s+(?-i:[A-Z])[\w&'.,-]*)*"
NAME = r"(?-i:[A-Z][a-zA-Z'-]+)(?:\s+(?-i:[A-Z])[a-zA-Z'-]+)?"
LOWERPH = r"[a-zA-Z][\w' +#.-]{1,44}"

PRONOUNS = {"she", "he", "they", "it", "her", "him", "them"}
FIRST_PRONOUNS = {"i", "we", "me", "us", "my", "our"}

ROLE_BLOCK = {
    "tired", "happy", "sad", "sorry", "sure", "okay", "ok", "fine",
    "glad", "ready", "here", "back", "busy", "excited", "curious",
    "confused", "hungry", "free", "done", "good", "great", "well",
    "not", "just", "still", "new", "all", "so", "very", "really",
}

FAMILY_MAP = {
    "sister": "sibling", "brother": "sibling", "twin": "sibling",
    "mother": "parent", "mom": "parent", "father": "parent", "dad": "parent",
    "wife": "spouse", "husband": "spouse", "partner": "spouse",
    "daughter": "child", "son": "child", "cousin": "friend",
}

IS_MY_MAP = {
    "sister": "sibling", "brother": "sibling", "mother": "parent",
    "mom": "parent", "father": "parent", "dad": "parent", "wife": "spouse",
    "husband": "spouse", "partner": "spouse", "friend": "friend",
    "colleague": "friend", "teammate": "friend", "cousin": "friend",
    "manager": "reports_to", "boss": "reports_to",
}


@dataclass
class Candidate:
    subject: str
    relation: str
    value: str
    confidence: float
    pattern: str
    span: tuple[int, int] = (0, 0)
    valid_from: str | None = None
    valid_to: str | None = None
    retraction: bool = False
    note: str = ""


@dataclass
class ExtractionContext:
    user_id: str = "default"
    agent_id: str | None = None
    run_id: str | None = None
    ts: datetime | None = None
    speaker: str = "user"          # user | assistant | system
    subject_name: str | None = None  # learned canonical name of the user
    lexicon: set[str] = field(default_factory=set)

    @property
    def subject(self) -> str:
        return self.subject_name or f"user:{self.user_id}"


def clean_value(v: str) -> str:
    v = v.strip().strip('"\',.!?;:')
    v = re.sub(r"^(?:the|a|an)\s+", "", v, flags=re.I)
    v = re.sub(r"\s+", " ", v)
    v = v.strip().rstrip(".,!?;:")
    return v.strip()


def date_in(text: str, ts: datetime | None) -> str | None:
    if ts is None:
        return None
    ds = find_dates(text, ts)
    return ds[0]["iso"] if ds else None


# ---------------------------------------------------------------- patterns
# Each entry: (name, regex, handler(match, ctx, sent_span) -> list[Candidate])
# m.group("val") etc. Handlers return candidates with subject placeholder
# "SELF" resolved by the extractor to ctx.subject.

PATTERNS: list[tuple[str, re.Pattern, object]] = []


def pattern(name: str, rx: str):
    compiled = re.compile(rx, re.I)

    def deco(fn):
        PATTERNS.append((name, compiled, fn))
        return fn

    return deco


# --- identity -------------------------------------------------------------
@pattern("name_intro", rf"\bmy name is\s+(?P<val>{NAME})")
def _name(m, ctx, sp, ts, sent):
    full = clean_value(m.group("val"))
    out = []
    if ctx.subject_name is None or ctx.subject_name.lower() != full.lower():
        out.append(Candidate("SELF", "name", full, 0.95, "name_intro", (sp[0] + m.start(), sp[0] + m.end())))
    parts = full.split()
    if len(parts) > 1 and len(parts[0]) > 2:
        out.append(Candidate(full, "alias", parts[0], 0.9, "name_intro_alias"))
    return out


@pattern("called", rf"\b(?:i am called|i'm called|call me|i go by|you can call me)\s+(?P<val>{NAME})")
def _called(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    rel = "name" if ctx.subject_name is None else "alias"
    target = ctx.subject_name or "SELF"
    return [Candidate(target, rel, v, 0.9, "called")]


# --- employment ------------------------------------------------------------
WORK_AT = rf"(?P<val>{ORG})"
@pattern("works_at",
         rf"\bi\s+(?:(?:now|currently|these days)\s+)?(?:work|worked|working|'m working|am working)\s+(?:at|for)\s+(?:the\s+)?{WORK_AT}")
def _works(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    vf = date_in(sent, ts) if re.search(r"\b(joined|started|got a job)\b", sent, re.I) else None
    return [Candidate("SELF", "works_at", v, 0.92, "works_at", valid_from=vf)]


@pattern("joined_org",
         rf"\bi\s+(?:joined|started(?:\s+working)?\s+at|got a job at|moved to a job at)\s+{WORK_AT}")
def _joined(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    return [Candidate("SELF", "works_at", v, 0.92, "joined_org",
                      valid_from=date_in(sent, ts))]


@pattern("at_org", rf"\bi'?m\s+(?:now\s+|currently\s+|these days\s+)?at\s+(?P<val>{ORG})")
def _at_org(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    return [Candidate("SELF", "works_at", v, 0.85, "at_org")]


@pattern("left_org",
         rf"\bi\s+(?:left|quit|resigned from|was laid off from|departed)\s+(?P<val>{ORG})")
def _left(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    vf = date_in(sent, ts)
    return [Candidate("SELF", "left", v, 0.9, "left_org", valid_from=vf,
                      retraction=True)]


@pattern("no_longer", rf"\bi\s+(?:no longer|don'?t|do not)\s+work\s+(?:at|for)\s+(?P<val>{ORG})")
def _no_longer(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    return [Candidate("SELF", "left", v, 0.88, "no_longer", retraction=True)]


@pattern("no_longer_at", rf"\bi'?m\s+no longer\s+(?:at|with)\s+(?P<val>{ORG})")
def _no_longer_at(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    return [Candidate("SELF", "left", v, 0.88, "no_longer_at", retraction=True)]


@pattern("role", rf"\bi\s+(?:work\s+as|am|'m)\s+(?:a|an|the)\s+(?P<val>[a-z][a-z /-]{{2,40}}?)(?=[,.!?]|\s+(?:at|in|on|with|for|and|but|where)\b)")
def _role(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    if v.lower() in ROLE_BLOCK:
        return []
    return [Candidate("SELF", "role", v, 0.85, "role")]


@pattern("role_as", rf"\bas\s+(?:a|an)\s+(?P<val>[a-z][a-z /-]{{2,40}}?)(?=[,.!?]|\s+(?:at|in|on|with|for)\b)")
def _role_as(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    if v.lower() in ROLE_BLOCK:
        return []
    return [Candidate("SELF", "role", v, 0.8, "role_as")]


@pattern("role_my", rf"\bmy\s+(?:job|role|title|position)\s+is\s+(?:a|an|the)?\s*(?P<val>[a-z][a-z /-]{{2,40}}?)(?=[,.!?]|\s+(?:at|in|on|with|for)\b)")
def _role_my(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    return [Candidate("SELF", "role", v, 0.9, "role_my")]


@pattern("reports_to", rf"\bmy\s+(?:manager|boss|lead|supervisor|team lead)\s+is\s+(?P<val>{NAME})")
def _mgr(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "reports_to", clean_value(m.group("val")), 0.9, "reports_to")]


@pattern("i_report", rf"\bi\s+report\s+to\s+(?P<val>{NAME})")
def _i_report(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "reports_to", clean_value(m.group("val")), 0.9, "i_report")]


@pattern("member_of", rf"\bi'?m\s+(?:now\s+)?(?:on|part of)\s+the\s+(?P<val>[A-Z][\w-]*(?:\s+[\w-]+)*?)\s*(?:team|group|squad|org)?\b")
def _member(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    if not v:
        return []
    team = v if v.lower().endswith(("team", "group", "squad")) else f"{v} team"
    return [Candidate("SELF", "member_of", team, 0.88, "member_of")]


@pattern("joined_team", rf"\bi\s+joined\s+the\s+(?P<val>[A-Z][\w-]*(?:\s+[\w-]+)*?)\s+(?:team|group|squad)\b")
def _joined_team(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    return [Candidate("SELF", "member_of", f"{v} team", 0.9, "joined_team",
                      valid_from=date_in(sent, ts))]


# --- residence -------------------------------------------------------------
@pattern("lives_in", rf"\bi\s+(?:live|lived|'m living|am living|'m based|am based|reside)\s+in\s+(?P<val>{ORG})")
def _lives(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "lives_in", clean_value(m.group("val")), 0.92, "lives_in")]


@pattern("im_in", rf"\bi'?m\s+(?:currently\s+|now\s+)?in\s+(?P<val>{ORG})")
def _im_in(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    if v.lower() in ("fact", "love", "trouble", "debt", "charge", "love with"):
        return []
    return [Candidate("SELF", "lives_in", v, 0.7, "im_in")]


@pattern("moved_to", rf"\b(?:i|we)\s+(?:moved|relocated)\s+to\s+(?P<val>{ORG})")
def _moved(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    return [Candidate("SELF", "moved_to", v, 0.92, "moved_to",
                      valid_from=date_in(sent, ts))]


# --- preferences -----------------------------------------------------------
LIKE_TAIL = r"(?=[,.!?]|\s+(?:but|though|when|because|and|so|especially)\b|$)"
@pattern("likes", rf"\bi\s+(?:(?:really|absolutely|totally)\s+)?(?:love|loved|like|liked|enjoy|enjoyed)\s+(?P<val>[a-zA-Z][\w' +#.-]{{1,44}}?){LIKE_TAIL}")
def _likes(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    if v.lower() in ("it", "that", "this", "them", "you"):
        return []
    return [Candidate("SELF", "likes", v, 0.85, "likes")]


@pattern("fan_of", rf"\bi'?m\s+(?:a|an)\s+(?:big\s+)?fan\s+of\s+(?P<val>{LOWERPH})")
def _fan(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "likes", clean_value(m.group("val")), 0.85, "fan_of")]


@pattern("favorite", rf"\bmy favorite\s+[\w ]{{2,24}}\s+is\s+(?P<val>{LOWERPH})")
def _favorite(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "likes", clean_value(m.group("val")), 0.88, "favorite")]


@pattern("dislikes", rf"\bi\s+(?:hate|hated|dislike|can'?t stand|don'?t like|do not like)\s+(?P<val>[a-zA-Z][\w' +#.-]{{1,44}}?){LIKE_TAIL}")
def _dislikes(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "dislikes", clean_value(m.group("val")), 0.85, "dislikes")]


@pattern("prefers", rf"\bi\s+(?:'d\s+)?(?:prefer|preferre?d)\s+(?P<val>[a-zA-Z][\w' +#.-]{{1,44}}?)(?:\s+(?:over|to|than)\s+[\w' -]+)?{LIKE_TAIL}")
def _prefers(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    cat = re.search(r"\bfor\s+([a-z][a-z ]{2,20})", sent[m.start():])
    if cat:
        v = f"{v} (for {clean_value(cat.group(1))})"
    return [Candidate("SELF", "prefers", v, 0.9, "prefers")]


@pattern("pref_change", rf"\b(?:actually|these days|nowadays|lately|recently)[,.]?\s*(?:i\s+)?(?:prefer|like|love|drink|use|order)\s+(?P<val>[a-zA-Z][\w' +#.-]{{1,44}}?)(?=[,.!?]|$|\s+(?:but|though|and)\b)")
def _pref_change(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "prefers", clean_value(m.group("val")), 0.88, "pref_change")]


@pattern("switched_to", rf"\b(?:i'?ve|i have)\s+(?:switched|moved)\s+to\s+(?P<val>[a-zA-Z][\w' +#.-]{{1,44}}?)(?=[,.!?]|$|\s+(?:but|though|and)\b)")
def _switched(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "prefers", clean_value(m.group("val")), 0.88, "switched_to")]


@pattern("more_of_a", rf"\bi'?m\s+more\s+of\s+a\s+(?P<val>{LOWERPH})\s+(?:person|guy|girl|fan|drinker|person\s+now)")
def _more_of(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "prefers", clean_value(m.group("val")), 0.82, "more_of_a")]


# --- skills & education ----------------------------------------------------
@pattern("skill_know", rf"\bi\s+(?:know|code in|write|program in|build with)\s+(?P<val>[A-Za-z+#.][\w+#.]*(?:\s+(?:and|&)\s+[A-Za-z+#.][\w+#.]*)*)")
def _skill(m, ctx, sp, ts, sent):
    out = []
    for part in re.split(r"\s+(?:and|&)\s+", clean_value(m.group("val"))):
        if len(part) > 1 and part.lower() not in ("it", "that", "this", "them"):
            out.append(Candidate("SELF", "has_skill", part, 0.85, "skill_know"))
    return out


@pattern("skill_learning", rf"\bi(?:'ve|\s+have)\s+been\s+learning\s+(?P<val>[A-Za-z+#.][\w+#.]*)|\bi'?m\s+learning\s+(?P<val2>[A-Za-z+#.][\w+#.]*)")
def _skill_learn(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val") or m.group("val2") or "")
    return [Candidate("SELF", "has_skill", v, 0.8, "skill_learning")] if v else []


@pattern("skill_prof", rf"\bi'?m\s+(?:proficient|skilled|experienced)\s+in\s+(?P<val>[\w+#. ]{{2,40}})")
def _skill_prof(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "has_skill", clean_value(m.group("val")), 0.85, "skill_prof")]


@pattern("studied_at", rf"\bi\s+studied\s+(?P<major>[a-z][a-z ]{{2,40}}?)\s+at\s+(?P<val>{ORG})")
def _studied(m, ctx, sp, ts, sent):
    major = clean_value(m.group("major"))
    org = clean_value(m.group("val"))
    return [Candidate("SELF", "studied", f"{major} at {org}", 0.88, "studied_at")]


@pattern("majored", rf"\bi\s+majored\s+in\s+(?P<val>[a-z][a-z ]{{2,40}})|\bmy degree is in\s+(?P<val2>[a-z][a-z ]{{2,40}})")
def _majored(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val") or m.group("val2") or "")
    return [Candidate("SELF", "studied", v, 0.85, "majored")] if v else []


@pattern("speaks", rf"\bi\s+speak\s+(?P<val>[A-Za-z]+(?:\s+and\s+[A-Za-z]+)*)")
def _speaks(m, ctx, sp, ts, sent):
    out = []
    for part in re.split(r"\s+and\s+", clean_value(m.group("val"))):
        out.append(Candidate("SELF", "speaks", part, 0.85, "speaks"))
    return out


# --- personal ---------------------------------------------------------------
@pattern("birthday", rf"\bmy birthday is\s+(?P<val>[^,.!?]{{3,30}})|\bi was born on\s+(?P<val2>[^,.!?]{{3,30}})")
def _birthday(m, ctx, sp, ts, sent):
    raw = m.group("val") or m.group("val2") or ""
    v = clean_value(raw)
    return [Candidate("SELF", "birthday", v, 0.9, "birthday")]


@pattern("age", r"\bi'?m\s+(\d{1,2})\s+years?\s+old")
def _age(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "age", m.group(1), 0.9, "age")]


@pattern("family", rf"\bmy\s+(?P<rel>sister|brother|mother|mom|father|dad|wife|husband|partner|daughter|son|cousin|twin)(?:'s name)?\s+(?:is|is called)\s+(?P<val>{NAME})")
def _family(m, ctx, sp, ts, sent):
    rel = FAMILY_MAP.get(m.group("rel").lower(), "friend")
    return [Candidate("SELF", rel, clean_value(m.group("val")), 0.9, "family")]


@pattern("family2", rf"\bmy\s+(?P<rel>sister|brother|mother|mom|father|dad|wife|husband|partner|daughter|son|cousin|twin)\s+(?P<val>{NAME})\b")
def _family2(m, ctx, sp, ts, sent):
    rel = FAMILY_MAP.get(m.group("rel").lower(), "friend")
    return [Candidate("SELF", rel, clean_value(m.group("val")), 0.88, "family2")]


@pattern("is_my", rf"\b(?P<val>{NAME})\s+is\s+my\s+(?P<rel>sister|brother|mother|mom|father|dad|wife|husband|partner|friend|colleague|teammate|cousin|manager|boss)")
def _is_my(m, ctx, sp, ts, sent):
    rel = IS_MY_MAP.get(m.group("rel").lower(), "friend")
    return [Candidate("SELF", rel, clean_value(m.group("val")), 0.88, "is_my")]


@pattern("pet", rf"\bmy\s+(?P<kind>dog|cat|bird|rabbit)\s+is\s+(?:named\s+|called\s+)?(?P<val>{NAME})")
def _pet(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "has_pet", f"{m.group('kind')} named {clean_value(m.group('val'))}",
                      0.88, "pet")]


@pattern("hobby", rf"\bmy hobby is\s+(?P<val>[a-z][a-z ]{{2,40}})|\bin my free time\s+i\s+(?P<val2>[a-z][a-z ]{{2,40}})")
def _hobby(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val") or m.group("val2") or "")
    return [Candidate("SELF", "hobby", v, 0.8, "hobby")] if v else []


@pattern("goal", rf"\bmy goal is to\s+(?P<val>[a-z][a-z ]{{2,50}})|\bi'?m planning to\s+(?P<val2>[a-z][a-z ]{{2,50}})")
def _goal(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val") or m.group("val2") or "")
    return [Candidate("SELF", "goal", v, 0.75, "goal")] if v else []


# --- projects & events -------------------------------------------------------
@pattern("works_on", rf"\bi'?m\s+(?:currently\s+)?working\s+on\s+(?P<val>{ORG})|\bi\s+work\s+on\s+(?P<val2>{ORG})")
def _works_on(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val") or m.group("val2") or "")
    return [Candidate("SELF", "works_on", v, 0.88, "works_on")] if v else []


@pattern("building", rf"\b(?:we'?re|we are|i'?m|i am)\s+(?:currently\s+)?building\s+(?P<val>{ORG})")
def _building(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "works_on", clean_value(m.group("val")), 0.85, "building")]


@pattern("completed", rf"\b(?:we|i)\s+(?:shipped|launched|finished|completed|released|deployed)\s+(?:the\s+)?(?P<val>{ORG})")
def _completed(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    return [Candidate("SELF", "completed", v, 0.88, "completed",
                      valid_to=date_in(sent, ts))]


@pattern("used_to_work", rf"\bi used to\s+(?:work|working)\s+(?:at|for)\s+(?P<val>{ORG})")
def _used_work(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    return [Candidate("SELF", "works_at", v, 0.7, "used_to_work",
                      valid_to=date_in(sent, ts) or None)]


@pattern("used_to_live", rf"\bi used to\s+live\s+in\s+(?P<val>{ORG})")
def _used_live(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    return [Candidate("SELF", "lives_in", v, 0.7, "used_to_live",
                      valid_to=date_in(sent, ts) or None)]


# --- third person -------------------------------------------------------------
TP_VERBS = {
    "works at": "works_at", "work at": "works_at", "worked at": "works_at",
    "joined": "works_at", "left": "left", "quit": "left",
    "lives in": "lives_in", "live in": "lives_in", "moved to": "moved_to",
    "manages": "manages", "manage": "manages", "managing": "manages",
    "uses": "uses", "use": "uses", "using": "uses",
    "prefers": "prefers", "prefer": "prefers",
    "likes": "likes", "like": "likes",
    "studied at": "studied_at", "reports to": "reports_to",
    "leads": "manages", "lead": "manages",
}
TP_RX = (rf"\b(?P<subj>{NAME})\s+(?P<verb>"
         + "|".join(re.escape(k) for k in sorted(TP_VERBS, key=len, reverse=True))
         + rf")\s+(?:the\s+)?(?P<val>{ORG}(?:\s+(?:team|group|squad|org|department|division))?)")
PATTERNS.append(("third_person", re.compile(TP_RX, re.I),
                 lambda m, ctx, sp, ts, sent: [
                     Candidate(clean_value(m.group("subj")),
                               TP_VERBS[m.group("verb").lower()],
                               clean_value(m.group("val")), 0.85, "third_person",
                               retraction=TP_VERBS[m.group("verb").lower()] == "left")]))


# Mem0/Zep-style migrated summaries: "User prefers oat milk lattes.",
# "User knows Rust." — competitor exports state facts in exactly this
# third-person shape, so the migration path needs to catch them.
SUMMARY_VERBS = {
    "prefers": "prefers", "likes": "likes", "knows": "knows",
    "uses": "uses", "lives in": "lives_in", "works at": "works_at",
    "speaks": "speaks", "owns": "owns", "enjoys": "likes",
    "hates": "dislikes", "dislikes": "dislikes",
    "studied": "studied", "plays": "plays",
}
_SUMMARY_RX = (rf"\b(?P<subj>User|[A-Z][a-z]{{2,}})\s+(?P<verb>"
               + "|".join(re.escape(k) for k in
                          sorted(SUMMARY_VERBS, key=len, reverse=True))
               + rf")\s+(?P<val>[a-zA-Z][\w' +#.-]{{1,44}}?)"
               + rf"(?=[.!?]|$|,|\s+(?:but|and|however|so)\b)")
PATTERNS.append(("user_summary", re.compile(_SUMMARY_RX),
                 lambda m, ctx, sp, ts, sent: [
                     Candidate(clean_value(m.group("subj")),
                               SUMMARY_VERBS[m.group("verb").lower()],
                               clean_value(m.group("val")), 0.80,
                               "user_summary")]))


@pattern("team_uses", rf"\bthe\s+(?P<val>[A-Z][\w-]*(?:\s+[\w-]+)*?)\s+team\s+uses?\s+(?P<tech>[A-Za-z+#.][\w+#.]*)")
def _team_uses(m, ctx, sp, ts, sent):
    team = f"{clean_value(m.group('val'))} team"
    return [Candidate(team, "uses", clean_value(m.group("tech")), 0.88, "team_uses")]


@pattern("possessive", rf"\b(?P<subj>{NAME})'s\s+(?P<rel>sister|brother|manager|boss|team|birthday|role|job|dog|cat|wife|husband)\s+is\s+(?P<val>[^,.!?]{{2,40}})")
def _possessive(m, ctx, sp, ts, sent):
    subj = clean_value(m.group("subj"))
    rel = m.group("rel").lower()
    raw = m.group("val")
    rel_map = {"sister": "sibling", "brother": "sibling", "manager": "reports_to",
               "boss": "reports_to", "team": "member_of", "birthday": "birthday",
               "role": "role", "job": "role", "dog": "has_pet", "cat": "has_pet",
               "wife": "spouse", "husband": "spouse"}
    val = date_in(raw, ts) if rel == "birthday" and ts else clean_value(raw)
    return [Candidate(subj, rel_map.get(rel, "mentioned"), val, 0.85, "possessive")]




@pattern("role_at_org",
         rf"\bi'?m\s+(?:a|an)\s+(?P<role>[a-z][a-z /-]{{2,40}}?)\s+at\s+(?P<val>{ORG})")
def _role_at_org(m, ctx, sp, ts, sent):
    out = [Candidate("SELF", "works_at", clean_value(m.group("val")), 0.9,
                     "role_at_org")]
    # "I'm a software engineer at Netflix" carries BOTH the org and the
    # occupation — the role must not be silently dropped, or "what does
    # X do for a living?" becomes unanswerable.
    rv = clean_value(m.group("role"))
    if rv.lower() not in ROLE_BLOCK:
        out.append(Candidate("SELF", "role", rv, 0.85, "role_at_org"))
    return out


@pattern("been_working", rf"\bi'?ve\s+been\s+(?:working\s+)?at\s+(?P<val>{ORG})")
def _been_working(m, ctx, sp, ts, sent):
    # "I've been working at X since March 2024" — the since-clause dates
    # the START of the employment interval (not the session date).
    vf = date_in(sent, ts) if re.search(r"\bsince\b", sent, re.I) else None
    return [Candidate("SELF", "works_at", clean_value(m.group("val")), 0.9,
                      "been_working", valid_from=vf)]


@pattern("instruction", rf"\b(?:please\s+)?(?:always|never)\s+(?P<val>[a-z][a-z ]{{3,60}}?)(?=[.!?]|$)")
def _instruction(m, ctx, sp, ts, sent):
    v = clean_value(m.group("val"))
    if v.lower() in ROLE_BLOCK:
        return []
    return [Candidate("SELF", "instruction", m.group(0).strip().rstrip(".!").lower(),
                      0.78, "instruction")]


@pattern("from_now_on", rf"\bfrom now on[,.]?\s*(?:please\s+)?(?P<val>[a-z][a-z ]{{3,60}}?)(?=[.!?]|$)")
def _from_now_on(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "instruction", "from now on " + clean_value(m.group("val")),
                      0.78, "from_now_on")]


# --- assistant (second person about the user) --------------------------------
@pattern("you_work", rf"\byou\s+(?:mentioned\s+)?(?:work|worked|working)\s+(?:at|for)\s+(?P<val>{ORG})")
def _you_work(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "works_at", clean_value(m.group("val")), 0.75, "you_work")]


@pattern("you_live", rf"\byou\s+(?:live|lived)\s+in\s+(?P<val>{ORG})")
def _you_live(m, ctx, sp, ts, sent):
    return [Candidate("SELF", "lives_in", clean_value(m.group("val")), 0.75, "you_live")]


# --- generic event with date --------------------------------------------------
EVENT_START = re.compile(r"\b(?:i|we)\s+(?P<vp>[a-z][a-z' -]{2,60})", re.I)


import re as _re2
_DATE_GATE = _re2.compile(
    r"\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|yesterday|today|"
    r"tomorrow|last|ago", _re2.I)


def extract_events(sent: str, sp: tuple[int, int], ts: datetime | None,
                   ctx: ExtractionContext) -> list[Candidate]:
    if ts is None or not _DATE_GATE.search(sent):
        return []
    dates = find_dates(sent, ts)
    if not dates:
        return []
    m = EVENT_START.search(sent)
    if not m:
        return []
    vp = m.group("vp").strip()
    # strip a trailing date surface from the verb phrase
    for d in dates:
        surf = d["surface"].strip()
        if surf and surf.lower() in vp.lower():
            vp = re.sub(re.escape(surf), "", vp, flags=re.I).strip()
    _MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
    for _ in range(3):
        vp = re.sub(r"\s+(?:on|in|at|since|ago|back|last|this|next)\s*$", "", vp, flags=re.I).strip()
        vp = re.sub(rf"\s+(?:{_MONTHS})\s*$", "", vp, flags=re.I).strip()
        vp = re.sub(r"\s+\d{1,4}(?:st|nd|rd|th)?\s*$", "", vp).strip()
        vp = re.sub(r"\s+(?:week|month|year|day)s?\s*$", "", vp, flags=re.I).strip()
    vp = re.sub(r"\s+", " ", vp)
    if len(vp) < 4 or vp.split()[0] in ("will", "would", "hope", "want", "plan"):
        return []
    # conversational fluff is not a memorable life event
    _JUNK = re.compile(
        r"^(?:read|reads|watch|watches|watched|see|saw|seen|hear|heard|"
        r"think|thinks|thought|look|looked|talk|talked|remember|"
        r"was|were|am|be|been)\b", re.I)
    if _JUNK.match(vp):
        return []
    return [Candidate("SELF", "event", vp, 0.7, "event",
                      valid_from=dates[0]["iso"],
                      span=(sp[0] + m.start(), sp[0] + m.end()))]

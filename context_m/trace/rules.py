"""Datalog-lite rules engine — deterministic forward chaining.

The plan specifies a lightweight Datalog engine (not CLIPS) for
forward-chaining over the Trace. Rules use a tiny Datalog syntax::

    head(X, Y) :- body1(X, Z), body2(Z, Y).

An atom ``rel(a, b)`` denotes the fact triple ``(a, rel, b)``.
Uppercase-initial tokens are variables. The engine joins body atoms
over active facts and materializes derived facts (``is_derived=True``)
which are themselves indexed in the VSA palace — inference bound into
the neuro-symbolic bridge, not bolted on.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

from context_m.trace.fact import Fact
from context_m.trace.store import TraceStore
from context_m.util import iso, new_id

RULE_RE = re.compile(r"^([\w]+)\s*\(([^)]*)\)\s*:-\s*(.+)$")
ATOM_RE = re.compile(r"([\w]+)\s*\(([^)]*)\)")
Arg = str
Atom = tuple[str, Arg, Arg]          # (relation, subject_arg, value_arg)


def _mk_atom(rel: str, args: str) -> Atom:
    parts = [p.strip() for p in args.split(",") if p.strip()]
    if len(parts) == 1:
        return (rel, parts[0], "_")
    if len(parts) == 2:
        return (rel, parts[0], parts[1])
    # 3-arg form: rel(subject, relation, value) — relation arg must equal rel
    return (rel, parts[0], parts[2])


def parse_rule(text: str) -> "Rule":
    text = text.strip().rstrip(".")
    m = RULE_RE.match(text)
    if not m:
        raise ValueError(f"invalid rule: {text!r}")
    head = _mk_atom(m.group(1), m.group(2))
    body = [_mk_atom(a.group(1), a.group(2)) for a in ATOM_RE.finditer(m.group(3))]
    if not body:
        raise ValueError(f"rule with empty body: {text!r}")
    return Rule(head=head, body=body, source=text)


@dataclass
class Rule:
    head: Atom
    body: list[Atom]
    source: str = ""

    def render(self) -> str:
        def atom(a):
            return f"{a[0]}({a[1]}, {a[2]})"
        return f"{atom(self.head)} :- {', '.join(atom(b) for b in self.body)}."


DEFAULT_RULES = [
    parse_rule("reports_to(X, Y) :- manages(Y, X)."),
    parse_rule("team_uses(X, L) :- member_of(X, T), uses(T, L)."),
    parse_rule("lives_in(X, C) :- moved_to(X, C)."),
    parse_rule("same_person(N, X) :- alias(X, N)."),
]


class RuleEngine:
    def __init__(self, store: TraceStore, rules: list[Rule] | None = None) -> None:
        self.store = store
        self.rules = rules if rules is not None else list(DEFAULT_RULES)
        self._rel_cache: dict[str, list[Fact]] = {}

    def _facts_of(self, rel: str) -> list[Fact]:
        if rel not in self._rel_cache:
            self._rel_cache[rel] = self.store.query_facts(relation=rel, active=True,
                                                          derived=False)
        return self._rel_cache[rel]

    def _join(self, rule: Rule) -> list[dict[str, str]]:
        bindings: list[dict[str, str]] = [{}]
        atoms = sorted(rule.body, key=lambda a: len(self._facts_of(a[0])))
        for rel, s_arg, v_arg in atoms:
            nxt: list[dict[str, str]] = []
            for b in bindings:
                for f in self._facts_of(rel):
                    # scope isolation: a rule joins premises from ONE user
                    # scope (None = global premise may join any scope).
                    bu = b.get("_user")
                    if bu is not None and f.user_id not in (None, bu):
                        continue
                    nb = dict(b)
                    if not self._bind(nb, s_arg, f.subject):
                        continue
                    if not self._bind(nb, v_arg, f.value):
                        continue
                    if f.user_id is not None:
                        nb["_user"] = f.user_id
                    nxt.append(nb)
            bindings = nxt
            if not bindings:
                break
        return bindings

    @staticmethod
    def _bind(b: dict[str, str], arg: str, val: str) -> bool:
        if arg == "_":
            return True
        if arg[0].isupper():
            if arg in b and b[arg] != val:
                return False
            b[arg] = val
            return True
        return arg == val

    def _subst(self, head: Atom, b: dict[str, str]) -> tuple[str, str, str] | None:
        rel, s_arg, v_arg = head
        s = b.get(s_arg) if s_arg[0:1].isupper() else (s_arg if s_arg != "_" else None)
        v = b.get(v_arg) if v_arg[0:1].isupper() else (v_arg if v_arg != "_" else None)
        if not s or not v:
            return None
        return (s, rel, v)

    def apply(self, now: _dt.datetime | None = None, max_iterations: int = 3) -> list[Fact]:
        """Run rules to fixpoint; return newly derived facts.

        Derived facts inherit the USER SCOPE of their premises — without
        this, ``team_uses(X, L) :- member_of(X, T), uses(T, L)`` derives a
        fact under ``default`` that user0's reader can never see (scope
        filter drops it), silently losing every multi-hop answer."""
        now = now or _dt.datetime.now(_dt.timezone.utc)
        derived_new: list[Fact] = []
        seen: set[tuple] = set()
        for _ in range(max_iterations):
            added = 0
            for rule in self.rules:
                for b in self._join(rule):
                    sub = self._subst(rule.head, b)
                    if not sub:
                        continue
                    scope = b.get("_user") or "default"
                    s, rel, v = sub
                    key = (s, rel, v, scope)
                    if key in seen:
                        continue
                    seen.add(key)
                    if self.store.query_facts(subject=s, relation=rel, value=v,
                                              user_id=scope, active=True):
                        continue
                    f = Fact(
                        id=new_id(), subject=s, relation=rel, value=v,
                        valid_from=iso(now)[:10], tx_from=iso(now),
                        user_id=scope,
                        confidence=0.75, memory_type="long_term",
                        is_derived=True,
                        provenance={"rule": rule.source, "bindings": b})
                    self.store.insert_fact(f)
                    derived_new.append(f)
                    self._rel_cache.pop(rel, None)
                    added += 1
            if added == 0:
                break
        return derived_new

    def invalidate(self) -> None:
        self._rel_cache.clear()

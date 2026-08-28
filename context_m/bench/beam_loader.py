"""BEAM-10M dataset loader — fetches real conversations from HF.

BEAM-10M (Mohammadta/BEAM-10M) is a LongMemEval-style benchmark:
each row is a long, multi-session conversation with:
  - conversation_id
  - user_profile (user_info + user_relationships) — ground-truth facts
  - narratives — a structured timeline of events
  - chat — 10 plans, each with 10 batches of ~60 turns
  - probing_questions — questions to test memory of the conversation
  - plans — alternate plans structure

The HuggingFace datasets API is rate-limited from many sandboxes
(429 Too Many Requests from CloudFront). The datasets-server endpoint
(hosted separately) IS reachable. This loader pulls via datasets-server
so the bench works even when direct HF datasets API is blocked.

Usage:
    from context_m.bench.beam_loader import load_beam_rows
    rows = load_beam_rows(n=2)  # fetch first 2 conversations
    for r in rows:
        print(r['conversation_id'])
        print(r['user_profile']['user_info'])
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Iterator


DATASETS_SERVER = "https://datasets-server.huggingface.co"
DATASET_NAME = "Mohammadta/BEAM-10M"
CONFIG = "default"
SPLIT = "10M"


def _fetch_rows(offset: int = 0, length: int = 1) -> dict:
    """Fetch BEAM-10M rows via the datasets-server endpoint."""
    url = (f"{DATASETS_SERVER}/rows?dataset={DATASET_NAME}"
           f"&config={CONFIG}&split={SPLIT}"
           f"&offset={offset}&length={length}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "context-m-bench/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_beam_rows(n: int = 2, *, cache_dir: str | None = None) -> list[dict]:
    """Load N BEAM-10M conversations.

    Each row is a dict with keys: conversation_id, conversation_seed,
    narratives, user_profile, conversation_plan, user_questions, chat,
    probing_questions, plans.

    The dataset has 10 rows total. We fetch via the datasets-server
    endpoint in batches of 1 (each row is ~100MB so single-row fetches
    are safer than bulk).

    Optionally caches to `cache_dir` to avoid re-downloading.
    """
    cache = Path(cache_dir) if cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for i in range(n):
        # check cache first
        cached_path = cache / f"beam_row_{i}.json" if cache else None
        if cached_path and cached_path.exists():
            data = json.loads(cached_path.read_text())
        else:
            data = _fetch_rows(offset=i, length=1)
            if cached_path:
                cached_path.write_text(json.dumps(data))
        if "rows" in data and data["rows"]:
            rows.append(data["rows"][0]["row"])
    return rows


def parse_user_facts(row: dict) -> list[dict]:
    """Extract ground-truth facts from a BEAM row's user_profile.

    The user_profile has:
      user_info: free text "Name: Jennifer Mccall / Age: 59 / ..."
      user_relationships: free text with bullet points

    We extract structured facts via regex (zero LLM calls).
    """
    import re
    up = row.get("user_profile", {})
    info = up.get("user_info", "") or ""
    rels = up.get("user_relationships", "") or ""

    facts: list[dict] = []
    conv_id = row.get("conversation_id", "unknown")
    user_id = f"beam_{conv_id}"

    # name
    m = re.search(r"Name:\s*([^\n]+)", info)
    if m:
        facts.append({"subject": user_id, "relation": "name",
                       "value": m.group(1).strip()})
    # age
    m = re.search(r"Age:\s*(\d+)", info)
    if m:
        facts.append({"subject": user_id, "relation": "age",
                       "value": m.group(1).strip()})
    # gender
    m = re.search(r"Gender:\s*([^\n]+)", info)
    if m:
        facts.append({"subject": user_id, "relation": "gender",
                       "value": m.group(1).strip()})
    # location
    m = re.search(r"Location:\s*([^\n]+)", info)
    if m:
        facts.append({"subject": user_id, "relation": "location",
                       "value": m.group(1).strip()})
    # profession
    m = re.search(r"Profession:\s*([^\n]+)", info)
    if m:
        facts.append({"subject": user_id, "relation": "profession",
                       "value": m.group(1).strip()})

    # relationships — extract names + relations
    # relationship sections start with all-caps headers like
    # "PARENTS & GUARDIANS:" "ROMANTIC PARTNER:" "CHILDREN:" etc.
    cur_section = None
    for line in rels.split("\n"):
        line = line.strip()
        if not line:
            continue
        # detect header — line is all caps + colon
        if line.endswith(":") and line[:-1] == line[:-1].upper():
            cur_section = line[:-1]
            continue
        # bullet line — extract name + age
        m = re.match(r"•\s*([^\(]+)\s*\(.*?\)", line)
        if m and cur_section:
            name = m.group(1).strip().rstrip(",")
            # convert section to relation
            rel_map = {
                "PARENTS & GUARDIANS": "parent",
                "ROMANTIC PARTNER": "partner",
                "CHILDREN": "child",
                "SIBLINGS": "sibling",
                "FRIENDS": "friend",
                "COLLEAGUES": "colleague",
            }
            relation = rel_map.get(cur_section, cur_section.lower())
            facts.append({"subject": user_id, "relation": relation,
                           "value": name})
    return facts


def parse_chat_turns(row: dict) -> list[dict]:
    """Flatten a row's chat history into a list of user turns.

    Each turn has: content, id, index, question_type, role, time_anchor.
    We only keep role='user' turns (these are the messages the user
    actually said — what Context-M needs to extract facts from).
    """
    chat = row.get("chat", [])
    if not isinstance(chat, list):
        return []
    turns_out: list[dict] = []
    for plan_obj in chat:
        if not isinstance(plan_obj, dict):
            continue
        # plan_obj has one key like "plan-1"
        for plan_name, batches in plan_obj.items():
            if not isinstance(batches, list):
                continue
            for batch in batches:
                if not isinstance(batch, dict):
                    continue
                turns = batch.get("turns", [])
                if not isinstance(turns, list):
                    continue
                for turn_list in turns:
                    if not isinstance(turn_list, list):
                        continue
                    for turn in turn_list:
                        if not isinstance(turn, dict):
                            continue
                        if turn.get("role") == "user":
                            turns_out.append({
                                "content": turn.get("content", ""),
                                "time_anchor": turn.get("time_anchor"),
                                "question_type": turn.get("question_type"),
                                "plan": plan_name,
                                "batch": batch.get("batch_number"),
                            })
    return turns_out


def beam_rows_to_personas(rows: list[dict], *,
                           max_turns_per_persona: int = 100,
                           include_profile: bool = True) -> list[dict]:
    """Convert BEAM rows into the persona dict format the bench expects.

    Returns list of: {user_id, text, facts} where:
      user_id — derived from conversation_id
      text — concatenation of user_profile (if include_profile=True)
             + the first N user turns. The user_profile contains the
             ground-truth facts (Name, Age, Location, etc.) which the
             μ=0 extractor should pull from the explicit "Name: X"
             lines; the chat turns are the conversational context.
      facts — ground-truth structured facts parsed from user_profile
              (separate from text so the bench can check recall)
    """
    personas = []
    for row in rows:
        conv_id = row.get("conversation_id", "unknown")
        user_id = f"beam_{conv_id}"
        facts = parse_user_facts(row)
        turns = parse_chat_turns(row)
        # cap turns to keep ingest tractable
        turns = turns[:max_turns_per_persona]
        # build the ingest text: profile first (so facts are stated
        # explicitly), then the conversation turns
        parts = []
        if include_profile:
            up = row.get("user_profile", {})
            info = up.get("user_info", "")
            rels = up.get("user_relationships", "")
            if info:
                parts.append(info)
            if rels:
                parts.append(rels)
        # append the chat turns
        for t in turns:
            if t.get("content"):
                parts.append(t["content"])
        text = "\n".join(parts)
        personas.append({
            "user_id": user_id,
            "text": text,
            "facts": facts,
            "n_turns": len(turns),
            "conversation_id": conv_id,
        })
    return personas


__all__ = [
    "load_beam_rows",
    "parse_user_facts",
    "parse_chat_turns",
    "beam_rows_to_personas",
    "DATASET_NAME",
]

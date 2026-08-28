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

BULK DOWNLOAD PATHS (added for full-dataset benchmarks):
  - Local cache directory of per-row JSON files (preferred — produced
    by scripts/download_beam_full.sh OR by the .github/workflows/
    beam-cache.yml workflow that runs on a GitHub Actions runner)
  - Local parquet file (if you have downloaded the full parquet via
    `huggingface-cli download Mohammadta/BEAM-10M` on a runner whose
    IP is not rate-limited; the file is ~342MB). Pass the path via
    the BEAM_PARQUET env var or the parquet_path argument.
  - datasets-server /rows endpoint as fallback (works in sandboxes)

Usage:
    from context_m.bench.beam_loader import load_beam_rows
    rows = load_beam_rows(n=10)  # fetch all 10 conversations
    for r in rows:
        print(r['conversation_id'])
        print(r['user_profile']['user_info'])
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Iterator


DATASETS_SERVER = "https://datasets-server.huggingface.co"
DATASET_NAME = "Mohammadta/BEAM-10M"
CONFIG = "default"
SPLIT = "10M"
TOTAL_ROWS = 10  # the 10M split has 10 conversations, each ~10M tokens


def _fetch_rows(offset: int = 0, length: int = 1) -> dict:
    """Fetch BEAM-10M rows via the datasets-server endpoint."""
    url = (f"{DATASETS_SERVER}/rows?dataset={DATASET_NAME}"
           f"&config={CONFIG}&split={SPLIT}"
           f"&offset={offset}&length={length}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "context-m-bench/1.0"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_parquet(parquet_path: str | Path, n: int = TOTAL_ROWS) -> list[dict]:
    """Load rows from a local parquet file (requires pyarrow or pandas).

    Each parquet row has the same shape as the datasets-server /rows
    endpoint's `row` field. We return just the row dicts (no envelope).
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:
        try:
            import pandas as pd
        except ImportError:
            raise ImportError(
                "loading BEAM-10M from parquet requires pyarrow or "
                "pandas — install with `pip install pyarrow` or "
                "`pip install pandas`")
        df = pd.read_parquet(parquet_path)
        rows = []
        for _, r in df.head(n).iterrows():
            rows.append(r.to_dict() if hasattr(r, "to_dict") else dict(r))
        return rows
    table = pq.read_table(parquet_path)
    n = min(n, table.num_rows)
    rows = []
    # batch-convert to Python dicts (column-at-a-time is faster than row)
    cols = {name: table.column(name).to_pylist() for name in table.column_names}
    for i in range(n):
        rows.append({name: cols[name][i] for name in cols})
    return rows


def load_beam_rows(n: int = 2, *, cache_dir: str | None = None,
                   parquet_path: str | None = None) -> list[dict]:
    """Load N BEAM-10M conversations.

    Resolution order (first available wins):
      1. parquet_path argument (or BEAM_PARQUET env var) — a single
         parquet file containing all 10 rows
      2. cache_dir/beam_row_<i>.json — per-row JSON files (the format
         produced by download_beam_full.sh and beam-cache.yml)
      3. datasets-server /rows endpoint — streamed on demand

    Each row is a dict with keys: conversation_id, conversation_seed,
    narratives, user_profile, conversation_plan, user_questions, chat,
    probing_questions, plans.

    The full 10M dataset has 10 conversations totaling ~975MB in
    memory. We fetch one row at a time (each is ~50-110MB).
    """
    # 1. parquet path?
    pq_path = parquet_path or os.environ.get("BEAM_PARQUET")
    if pq_path and Path(pq_path).exists():
        return _load_parquet(pq_path, n=min(n, TOTAL_ROWS))

    cache = Path(cache_dir) if cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    n = min(n, TOTAL_ROWS)
    for i in range(n):
        # 2. cached row?
        cached_path = cache / f"beam_row_{i}.json" if cache else None
        if cached_path and cached_path.exists():
            try:
                data = json.loads(cached_path.read_text())
                if "rows" in data and data["rows"]:
                    rows.append(data["rows"][0]["row"])
                    continue
                # some manifests store the row directly without envelope
                if "conversation_id" in data:
                    rows.append(data)
                    continue
            except (json.JSONDecodeError, KeyError):
                # corrupt cache — re-fetch
                cached_path.unlink(missing_ok=True)
        # 3. fetch from datasets-server
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

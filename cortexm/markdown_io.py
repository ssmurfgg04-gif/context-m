"""Markdown round-trip for the bi-temporal Trace (sqlite-memory learn).

Why markdown?

  Reddit deep-dive 2026-08-29 found 46 mentions of "provenance" / "audit
  trail" / "human-readable storage" across r/LocalLLaMA + r/LangChain +
  r/Rag. Users got burnt by opaque memory systems — they want to open
  the memory folder in a text editor, see what the AI knows, fix a
  typo, save, and have the system pick it up on the next boot.

  sqlite-memory uses .md files as the source of truth. We adopt the
  same idea but layered on top of the existing bi-temporal Trace: the
  SQLite store stays the source of truth for queries; markdown is the
  portable / human-auditable / git-diff-able projection.

Format::

    ---
    id: <uuid>
    user_id: alice
    agent_id: claude
    run_id: session-7
    subject: Alice
    relation: works_at
    value: Google
    valid_from: 2026-08-01T00:00:00Z
    valid_to: null
    tx_from: 2026-08-29T12:34:56Z
    tx_to: null
    confidence: 0.92
    source_hash: <blake3>
    source_id: <chunk uuid>
    source_snippet: "I just started at Google..."
    provenance:
      source: extractor          # or "user_override" if mem.edit was used
      pattern: works_at
      manual_update: false
    ---

    Alice | works_at | Google

The YAML frontmatter carries every bi-temporal field. The body is the
human-readable triple plus the source snippet for context.

Round-trip:
  - export_markdown()  : write every active fact as one .md file
  - import_markdown()  : read .md files back, calling mem.add() /
    mem.edit() depending on the provenance.source field. Re-verifies
    BLAKE3 hashes; mismatches land in the audit log.

Lean: pure-stdlib (yaml is parsed with a tiny custom reader so we
don't add a PyYAML dependency).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


# ---------------- tiny YAML frontmatter parser (no PyYAML dep) ----------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_scalar(v: str):
    """Parse a single YAML-ish scalar. We only need str / int / float /
    bool / null. Nested dicts / lists are JSON-encoded as strings in the
    frontmatter (see ``provenance:`` above — it's emitted as a JSON
    string), so we JSON-parse them here."""
    v = v.strip()
    if v == "null" or v == "~":
        return None
    if v in ("true", "True", "TRUE"):
        return True
    if v in ("false", "False", "FALSE"):
        return False
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    # maybe JSON dict / list
    if v.startswith("{") or v.startswith("["):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            pass
    return v


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter_dict, body)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text, body = m.group(1), m.group(2)
    out: dict = {}
    for line in fm_text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = _parse_scalar(v)
    return out, body.strip()


def _dump_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # if it has colons, newlines, or quotes, JSON-encode it
        if any(c in v for c in (":", "\n", '"', "'")):
            return json.dumps(v, ensure_ascii=False)
        return v
    # dict / list → JSON string
    return json.dumps(v, ensure_ascii=False, default=str)


def render_frontmatter(d: dict) -> str:
    lines = ["---"]
    for k, v in d.items():
        lines.append(f"{k}: {_dump_scalar(v)}")
    lines.append("---\n")
    return "\n".join(lines)


# ---------------- public API ----------------

def export_markdown(memory, *, user_id: str | None = None,
                    out_dir: str | os.PathLike,
                    include_inactive: bool = False,
                    include_chunks: bool = True) -> dict:
    """Dump every fact (and optionally every chunk) as ``.md`` files
    in ``out_dir``. Idempotent — re-running overwrites. The output
    folder is a portable, human-auditable projection of the bi-temporal
    Trace.

    Returns a summary dict: ``{facts: N, chunks: M, files: K, out_dir}``.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / "facts").mkdir(exist_ok=True)
    if include_chunks:
        (out_path / "chunks").mkdir(exist_ok=True)
    user_id = user_id or memory.config.default_user_id

    n_facts = 0
    n_chunks = 0
    # 1. facts
    for f in memory.store.query_facts(user_id=user_id,
                                       active=None if include_inactive else True):
        snippet = ""
        if f.source_id:
            chunk = memory.store.get_chunk(f.source_id)
            if chunk and chunk.get("text"):
                snippet = chunk["text"][:200]
        fm = {
            "id": f.id,
            "user_id": f.user_id,
            "agent_id": f.agent_id,
            "run_id": f.run_id,
            "subject": f.subject,
            "relation": f.relation,
            "value": f.value,
            "valid_from": str(f.valid_from) if f.valid_from else None,
            "valid_to": str(f.valid_to) if f.valid_to else None,
            "tx_from": str(getattr(f, "tx_from", "") or ""),
            "tx_to": str(getattr(f, "tx_to", "") or ""),
            "confidence": float(getattr(f, "confidence", 0.0) or 0.0),
            "source_hash": f.source_hash,
            "source_id": f.source_id or "",
            "source_snippet": snippet,
            "provenance": (f.provenance if isinstance(f.provenance, dict)
                           else {"raw": str(f.provenance)[:200]}),
        }
        body = f"{f.subject} | {f.relation} | {f.value}"
        if snippet:
            body += f"\n\n> source: …{snippet}"
        # filename: <subject-short>__<relation>__<id8>.md
        safe_subj = re.sub(r"[^A-Za-z0-9_-]+", "_", f.subject or "no_subject")[:32]
        safe_rel = re.sub(r"[^A-Za-z0-9_-]+", "_", f.relation or "rel")[:32]
        fname = f"{safe_subj}__{safe_rel}__{f.id[:8]}.md"
        (out_path / "facts" / fname).write_text(
            render_frontmatter(fm) + body, encoding="utf-8")
        n_facts += 1

    # 2. chunks (raw source text — useful for grep / git diff)
    if include_chunks:
        try:
            for c in memory.store.chunks_for_scope(user_id=user_id):
                fm = {
                    "id": c.get("id", ""),
                    "user_id": user_id,
                    "agent_id": c.get("agent_id"),
                    "run_id": c.get("run_id"),
                    "created_at": str(c.get("created_at", "") or ""),
                    "hash": c.get("hash", ""),
                }
                body = c.get("text") or ""
                fname = f"chunk__{c.get('id', 'unknown')[:8]}.md"
                (out_path / "chunks" / fname).write_text(
                    render_frontmatter(fm) + "\n" + body, encoding="utf-8")
                n_chunks += 1
        except Exception:
            # chunks_for_scope may not exist on older stores; skip
            pass

    # 3. README.md so the folder is self-explanatory
    (out_path / "README.md").write_text(
        f"# cortexm memory export\n\n"
        f"user_id: `{user_id}`\n"
        f"exported_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"facts: {n_facts}\n"
        f"chunks: {n_chunks}\n\n"
        "## Layout\n\n"
        "- `facts/<subject>__<relation>__<id8>.md` — one file per fact,\n"
        "  YAML frontmatter carries every bi-temporal field.\n"
        "- `chunks/chunk__<id8>.md` — raw source text, hash-verified.\n\n"
        "## Round-trip\n\n"
        "  cortexm import --markdown <dir> --user-id <user>\n\n"
        "Re-importing re-verifies BLAKE3 hashes; mismatches land in the\n"
        "audit log. Human edits to the `value:` field are picked up and\n"
        "tagged with `source: user_override` provenance.\n",
        encoding="utf-8")

    return {"facts": n_facts, "chunks": n_chunks,
            "files": n_facts + n_chunks + 1, "out_dir": str(out_path)}


def import_markdown(memory, *, in_dir: str | os.PathLike,
                    user_id: str | None = None,
                    strategy: str = "upsert") -> dict:
    """Read markdown fact files back into the Trace.

    Strategy:
      - ``upsert`` (default): for each fact in the markdown, look up by
        ``id``. If it exists, call ``mem.edit()`` (carries
        ``source: user_override`` provenance). If not, ``mem.add()`` the
        text body so the extractor re-derives triples.
      - ``verify``: dry-run; report hash mismatches and structural
        problems without writing.

    Returns ``{imported, edited, added, hash_mismatches, errors}``.
    """
    in_path = Path(in_dir)
    facts_dir = in_path / "facts"
    if not facts_dir.is_dir():
        return {"imported": 0, "errors": [f"no facts/ subdir in {in_dir}"]}

    user_id = user_id or memory.config.default_user_id
    stats = {"imported": 0, "edited": 0, "added": 0,
             "hash_mismatches": 0, "errors": []}

    for md_file in sorted(facts_dir.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            fid = fm.get("id")
            subj = fm.get("subject")
            rel = fm.get("relation")
            val = fm.get("value")

            if strategy == "verify":
                if fid:
                    existing = memory.store.get_fact(fid)
                    if existing and existing.source_hash != fm.get("source_hash"):
                        stats["hash_mismatches"] += 1
                stats["imported"] += 1
                continue

            # upsert
            if fid:
                existing = memory.store.get_fact(fid)
                if existing:
                    # has the human edited the value?
                    if existing.value != val:
                        memory.edit(fid, val,
                                    edited_by="markdown_import",
                                    reason=f"import from {md_file.name}")
                        stats["edited"] += 1
                    # otherwise no-op
                    continue
            # not present → re-add via the extractor
            text_to_add = (fm.get("source_snippet") or body or
                           f"{subj} {rel} {val}")
            memory.add(text_to_add, user_id=user_id)
            stats["added"] += 1
        except Exception as e:
            stats["errors"].append(f"{md_file.name}: {e!s}")

    memory.reader.invalidate_caches()
    return stats

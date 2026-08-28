"""Async LLM enrichment fallback — graceful degradation off the μ=0 path.

The μ=0 protocol guarantees the SYNCHRONOUS ingest path never calls an LLM.
That determinism is the product's cost/speed moat, but 60 hand-written
patterns will inevitably miss messy, indirect, or non-English phrasing.
This module is the plan's acknowledged fallback: AFTER a chunk is stored
(and its BLAKE3 hash sealed), chunks whose text yielded ZERO pattern
candidates are queued for a second-pass LLM extraction. Enriched facts:

  * carry provenance {"pattern": "llm_enrichment", "extractor_model": ...}
    so every enriched fact is auditable and distinguishable from μ=0 facts;
  * are confidence-capped (default 0.85) so deterministic facts always win
    conflicts against enriched ones;
  * still pass the full InjecMEM / MINJA quarantine + lifecycle pipeline;
  * bump metrics.llm_calls so the μ=0 audit trail stays honest — an auditor
    sees exactly which phase spent LLM budget.

Usage (explicit, never automatic):
    report = memory.enrich(user_id="alice")            # sync
    report = memory.enrich_async(user_id="alice")      # background thread
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass

from context_m import metrics
from context_m.bridge.patterns import Candidate
from context_m.bridge.writer import MemoryWriter
from context_m.util import normalize

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))  # .../context-m
DEFAULT_EXTRACTOR_JS = os.path.join(_REPO_ROOT, "benchmarks", "llm",
                                    "extract_facts.mjs")
DEFAULT_NODE = os.environ.get("CORTEXM_NODE_BIN", "node")
SDK_FALLBACKS = [
    "/home/z/.bun/install/global/node_modules/z-ai-web-dev-sdk/dist/index.js",
]

ENRICH_CONFIDENCE_CAP = 0.85


@dataclass
class EnrichmentReport:
    chunks_total: int = 0
    chunks_eligible: int = 0        # zero pattern candidates
    llm_calls: int = 0
    llm_tokens: int = 0
    facts_extracted: int = 0
    facts_committed: int = 0
    quarantined: int = 0
    seconds: float = 0.0
    extractor_model: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class NodeLLMExtractor:
    """Subprocess bridge to benchmarks/llm/extract_facts.mjs (z-ai SDK).

    Injectable and replaceable: tests pass a plain Python callable with the
    same (texts, subjects) -> list[list[dict]] contract instead.
    """

    def __init__(self, script: str | None = None, concurrency: int = 4) -> None:
        self.script = script or DEFAULT_EXTRACTOR_JS
        self.concurrency = concurrency

    def __call__(self, texts: list[str], subjects: list[str | None]) -> list[list[dict]]:
        if not texts:
            return []
        items = [{"id": i, "text": t, "subject": s or None}
                 for i, (t, s) in enumerate(zip(texts, subjects))]
        with tempfile.TemporaryDirectory(prefix="cortexm-enrich-") as td:
            inp = os.path.join(td, "in.jsonl")
            outp = os.path.join(td, "out.jsonl")
            with open(inp, "w", encoding="utf-8") as fh:
                fh.writelines(json.dumps(it) + "\n" for it in items)
            env = dict(os.environ)
            env.setdefault("LLM_CONCURRENCY", str(self.concurrency))
            env.setdefault("LLM_CACHE_DIR",
                           os.path.join(_REPO_ROOT, "benchmarks", "llm", ".cache"))
            proc = subprocess.run(
                [DEFAULT_NODE, self.script, inp, outp, "--enrich"],
                capture_output=True, text=True, timeout=1800, env=env)
            if proc.returncode != 0 or not os.path.exists(outp):
                raise RuntimeError(f"enrichment extractor failed: "
                                   f"{proc.stderr[-500:]}")
            rows = [json.loads(l) for l in open(outp, encoding="utf-8")
                    if l.strip()]
        rows.sort(key=lambda r: int(r.get("id", 0)))
        out: list[list[dict]] = []
        for r in rows:
            facts = [f for f in (r.get("facts") or [])
                     if isinstance(f, dict) and f.get("subject")
                     and f.get("relation") and f.get("value")]
            out.append(facts)
        # keep alignment with input length
        while len(out) < len(texts):
            out.append([])
        return out


def _default_extractor():
    return NodeLLMExtractor()


def find_eligible_chunks(writer: MemoryWriter, user_id: str | None,
                         limit: int | None = None) -> list[dict]:
    """Chunks where the μ=0 extractor produced no REAL candidates.

    Low-confidence ``mention_fallback`` mentions (pattern="mention_fallback",
    conf 0.35) fire on almost any capitalized text — they are lexicon noise,
    not signal. A chunk is enrichment-eligible only when no genuine pattern
    matched, which is exactly the "pattern confidence dropped" condition the
    strategic plan describes for the async LLM fallback.
    """
    from context_m.bridge.patterns import ExtractionContext
    from context_m.bridge.extractor import Extractor

    extractor = writer.extractor if hasattr(writer, "extractor") else Extractor(writer.cfg)
    eligible: list[dict] = []
    for chunk in writer.store.all_chunks(user_id):
        ctx = ExtractionContext(
            user_id=chunk.get("user_id") or "default",
            agent_id=chunk.get("agent_id"), run_id=chunk.get("run_id"),
            ts=_parse_ts(chunk.get("ts")),
            speaker="assistant" if chunk.get("source") in ("assistant", "ai", "bot") else "user",
            subject_name=writer._name_of(chunk.get("user_id") or "default"),
            lexicon=writer._lexicon(chunk.get("user_id") or "default"))
        try:
            cands = extractor.extract(chunk["text"], ctx)
            real = [c for c in cands if c.pattern != "mention_fallback"]
        except Exception:
            real = []
        if not real and len(chunk["text"].strip()) >= 20:
            eligible.append(chunk)
            if limit and len(eligible) >= limit:
                break
    return eligible


def _parse_ts(v):
    if not v:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")) \
            if isinstance(v, str) else v
    except ValueError:
        return None


def enrich(writer: MemoryWriter, user_id: str | None = None, *,
           extractor=None, limit: int | None = None,
           min_confidence: float | None = None,
           dry_run: bool = False) -> EnrichmentReport:
    """Second-pass LLM extraction over zero-signal chunks. Explicit call."""
    import time

    t0 = time.time()
    rep = EnrichmentReport()
    eligible = find_eligible_chunks(writer, user_id, limit)
    rep.chunks_total = len(writer.store.all_chunks(user_id))
    rep.chunks_eligible = len(eligible)
    if dry_run or not eligible:
        rep.seconds = round(time.time() - t0, 3)
        return rep

    extractor = extractor or _default_extractor()
    texts = [c["text"] for c in eligible]
    subjects = [writer._name_of(c.get("user_id") or "default") for c in eligible]
    batch = extractor(texts, subjects)
    rep.llm_calls += 1
    metrics.bump_llm_call()

    cap = ENRICH_CONFIDENCE_CAP
    floor = min_confidence if min_confidence is not None \
        else writer.cfg.min_confidence

    for chunk, facts in zip(eligible, batch):
        model = None
        uid = chunk.get("user_id") or "default"
        # name learning from enriched facts: if the LLM spotted "my name
        # is X" in text the patterns missed, later facts in this batch can
        # be re-subjected to X so they align with query entities.
        learned_name = None
        for f in facts:
            if str(f.get("relation", "")).lower() == "name" and f.get("value"):
                learned_name = str(f["value"]).strip()
                if writer._name_of(uid) is None and learned_name:
                    writer._set_name(uid, learned_name)
                break
        subj_hint = learned_name or writer._name_of(uid) or uid
        cands: list[Candidate] = []
        for f in facts:
            try:
                conf = float(f.get("confidence", 0.7))
            except (TypeError, ValueError):
                conf = 0.7
            conf = min(conf, cap)
            if conf < floor:
                continue
            model = f.get("_model") or model
            subj = str(f.get("subject") or "").strip()
            # re-subject generic speaker labels to the learned persona name
            # so enriched facts are findable by entity queries
            if (not subj or subj.lower() in ("user", "the user", "speaker",
                                             "i", "me")):
                subj = subj_hint
            cands.append(Candidate(
                subject=subj[:80],
                relation=_safe_relation(f["relation"]),
                value=str(f["value"])[:160],
                confidence=conf,
                pattern="llm_enrichment"))
        if not cands:
            continue
        n = writer.ingest_candidates(
            cands, user_id=uid,
            agent_id=chunk.get("agent_id"), chunk_id=chunk["id"],
            ts=_parse_ts(chunk.get("ts")), source="llm-enrichment",
            extractor_model=model)
        rep.facts_extracted += len(cands)
        rep.facts_committed += n
    rep.extractor_model = getattr(extractor, "model_name", None)
    rep.seconds = round(time.time() - t0, 3)
    return rep


def _safe_relation(rel) -> str:
    r = normalize(str(rel)).replace(" ", "_").replace("-", "_")
    return "".join(ch for ch in r if ch.isalnum() or ch == "_")[:40] or "related_to"


def enrich_async(writer: MemoryWriter, user_id: str | None = None, **kw):
    """Fire-and-forget background enrichment. Returns (thread, result holder)."""
    holder: dict = {}

    def _run():
        try:
            holder["report"] = enrich(writer, user_id, **kw)
        except Exception as e:  # pragma: no cover - background guard
            holder["error"] = str(e)

    t = threading.Thread(target=_run, daemon=True, name="cortexm-enrich")
    t.start()
    return t, holder

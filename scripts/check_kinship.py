#!/usr/bin/env python3
"""Quick check: what kinship facts did the extractor store for beam_8 / 9 / 10?
"""
import os, sys, time
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from cortexm.api.memory import Memory
from cortexm.config import Config
from cortexm.bench.beam_loader import (
    load_beam_rows, beam_rows_to_personas)
from cortexm.text.embedder import HashingEmbedder
from cortexm.text.idiolect import PerUserIdiolectNormalizer
from cortexm.text.dissim import DisSimSplitter

rows = load_beam_rows(n=10, cache_dir="/tmp/beam_cache")
personas = beam_rows_to_personas(rows, max_turns_per_persona=50)

cfg = Config.from_env()
cfg.db_path = "/tmp/kinship_check.db"
cfg.enable_rerank = True
if os.path.exists(cfg.db_path):
    os.unlink(cfg.db_path)
mem = Memory(cfg)
idiolect = PerUserIdiolectNormalizer(
    HashingEmbedder(mem.palace.dims, mem.palace.cfg.seed))
dissim = DisSimSplitter(max_depth=2)

for p in personas:
    text = p["text"]
    idiolect.observe(p["user_id"], text)
    text = idiolect.normalize(p["user_id"], text)
    for clause in dissim.simplify_text(text):
        mem.add([{"role": "user", "content": clause.text}],
                user_id=p["user_id"])

# show kinship facts stored for beam_7..10
for uid in ["beam_7", "beam_8", "beam_9", "beam_10"]:
    print(f"\n=== {uid} ===")
    facts = mem.store.query_facts(user_id=uid, active=True)
    kinship_rels = {"parent", "child", "partner", "spouse", "sibling",
                    "friend", "colleague"}
    kinship_facts = [f for f in facts if f.relation in kinship_rels]
    print(f"  total facts: {len(facts)}, kinship facts: {len(kinship_facts)}")
    for f in kinship_facts[:10]:
        print(f"    {f.subject} | {f.relation} | {f.value}")

mem.close()
if os.path.exists(cfg.db_path):
    os.unlink(cfg.db_path)

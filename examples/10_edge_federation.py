"""10 — Edge profile + privacy-preserving federation (schema only)."""
from context_m.config import Config
from context_m import Memory

# edge tier: binary codec, 96 bytes/vector — 10M memories on a Raspberry Pi 5
m = Memory(Config(codec="binary"))
m.add("My name is Ed. I work at a wind farm monitoring sensors.",
      user_id="ed")
print("storage:", m.storage_stats()["codec"],
      m.storage_stats()["bytes_per_vector"], "bytes/vector")

report = m.export_schema_report(user_id="ed")
print("federation payload keys:", sorted(report)[:4])
print("privacy:", report["privacy"])
merged = Memory.merge_schema_reports([report, report, report])
print("global contributors:", merged["contributors"],
      "| relations:", list(merged["relations"])[:3])
m.close()

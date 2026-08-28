"""4 — Cryptographic provenance: query → VSA → dereference → hash → source."""
from cortexm import Memory

m = Memory()
m.add("My name is Alan Turing. I work on the Enigma project at Bletchley.",
      user_id="alan")
audit = m.audit("Where does Alan work?", user_id="alan")
print("verification:", audit["verification"])
for link in audit["chain"][:3]:
    print(f"  {link['triple']}  hash={link['source_hash'][:12]}… "
          f"verified={link['source_verified']}")
    print(f"    source: {link['source_text'][:60]!r}")
m.close()

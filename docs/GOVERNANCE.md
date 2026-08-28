# Governance & Licensing Commitments

This document records the project's structural defenses against the
"MongoDB/Redis license-change death spiral": the scenario where a
vendor-funded project builds adoption, then pulls the rug by switching to
a source-available license that retroactively monetizes the community's
dependencies.

## 1. The commitment

**Context-M core is Apache-2.0 and stays Apache-2.0.** No CLA will ever
be introduced that grants the right to relicense the core under
non-open terms, and no future steward — commercial or otherwise — is
granted that right by anything in this repository.

## 2. Roadmap to neutral stewardship

Following the plan's Phase 4 governance track:

| Milestone | Trigger | Commitment |
|---|---|---|
| Governance board (informal) | 500+ stars or first external maintainer | PUBLIC decision log in this repo; RFC process for breaking changes (`docs/rfcs/`) |
| Foundation donation | Month 18, or 5K stars, or first paying pilot — whichever comes first | Donate `cortexm/` core + benchmarks to a neutral foundation (Apache Foundation or Linux Foundation sandbox); trademark transfers with the code |
| Enterprise tier boundary | At donation | Only these stay commercial: hosted federation plane, audit UI, SSO/SCIM integrations, SLA support. The fabric, the benchmark harness, the MCP server, the leaderboard remain open forever |

## 3. What the enterprise tier may never take back

Explicitly and permanently open, because they are the substrate others
build on:

- The `context_m` package (all layers: trace, VSA, bridge, api)
- The benchmark harness, OOD pipeline, and leaderboard generator
- The MCP server and CLI
- The migration tooling (`cortexm migrate`)
- The file formats: Trace schema, palace binary layout, commit/branch
  hash-chain format

Any attempt to move one of these behind a commercial license is a
governance violation under this document, and the community's remedy is
the last Apache-2.0 commit — which, by design, is always a complete,
working system (this is also why the benchmark artifacts and reproduction
instructions live in-repo).

## 4. Contributor protections

- **No CLA.** Contributions land under Apache-2.0's inbound=outbound
  default; the DCO (`Signed-off-by`) is the attribution mechanism.
- **Patent grant.** Apache-2.0 §3 provides it; we will not accept
  contributions under weaker terms.
- **Trademark.** "Context-M" will be transferred to the foundation at
  donation time, never held hostage by a commercial entity.

## 5. SOC 2 evidence templates

Enterprise procurement needs evidence artifacts, not promises. The repo
ships runnable evidence generators so auditors can reproduce every
control claim:

| Trust Service Criterion | Evidence in repo | Generator |
|---|---|---|
| CC7.2 (monitoring) | Prometheus metrics endpoint, health probes | `cortexm serve-rest` → `/metrics`, `/healthz`, `/readyz` |
| CC7.1 (detection) | Tamper-evident audit chain (hash-linked), tamper test | `tests/test_enterprise.py::test_audit_tamper_detection` |
| CC6.1 (logical access) | RBAC roles + API key digests + TTLs | `tests/test_enterprise.py` (RBAC suite) |
| CC6.7 (data in transit) | TLS termination docs, bearer auth | `docs/DEPLOYMENT.md` |
| CC8.1 (change management) | Memory Git hash-chained commits, branch/merge audit | `examples/07_memory_git.py` |
| PI1/PI2 (availability) | WAL crash recovery proof, snapshot/restore + PITR | `tests/test_wal_recovery.py`, `tests/test_enterprise.py` (snapshot suite) |
| PI3 (integrity) | BLAKE3/BLAKE2b provenance verification, ZK-lite proofs | `examples/04_provenance_audit.py`, `examples/08_zk_proof.py` |
| P6/P7 (confidentiality) | AES-256-GCM at rest, PII firewall, GDPR erasure + crypto-shred | `tests/test_enterprise.py` (PII/crypto/GDPR suites) |

A sample evidence pack export: `cortexm audit --db memory.db --export
jsonl` produces the audit-chain artifact; `cortexm snapshot` produces
manifest-verified backup evidence. These map 1:1 onto the criteria table
above and are regenerated on every release run.

## 6. What we deliberately did NOT build

Honesty about scope: managed cloud infrastructure, billing, hosted
dashboards, a Discord server, and the CRDT federation plane are not in
this repository. The first four require budget and standing
infrastructure this project does not have; the CRDT sync layer is
specified (`cortexm/federation.py` ships the k-anonymous schema
aggregation it builds on) but the conflict-resolution plane itself is
roadmapped, not shipped. See `docs/ROADMAP.md` for the honest gaps
ledger.

"""Federation transports.

A transport moves signed JSON envelopes between nodes. Two are provided:

* ``InMemoryMesh`` — deterministic test/单-process topology (star, line,
  partitioned groups);
* ``FileTransport`` — offline federation: envelopes are written as JSONL
  files into per-node spool directories. Any shared filesystem, git
  remote, object store or USB stick completes the physical channel; the
  CRDT guarantees convergence no matter the delivery order or duplication.
"""

from __future__ import annotations

import json
from pathlib import Path

from cortexm.federation.node import FederationNode


class InMemoryMesh:
    """Direct-connect topology; supports partitions for heal tests."""

    def __init__(self) -> None:
        self.links: set[tuple[str, str]] = set()
        self.deliveries = 0

    def link(self, a: str, b: str) -> None:
        self.links.add((a, b))
        self.links.add((b, a))

    def cut(self, a: str, b: str) -> None:
        self.links.discard((a, b))
        self.links.discard((b, a))

    def connected(self, a: str, b: str) -> bool:
        return (a, b) in self.links

    def gossip(self, nodes: dict[str, FederationNode],
               rounds: int = 1) -> int:
        """Every directly-linked pair runs one two-way sync per round."""
        total = 0
        for _ in range(rounds):
            for a, b in sorted(self.links):
                if a >= b:
                    continue
                na, nb = nodes[a], nodes[b]
                na.sync_with(nb)
                self.deliveries += 1
                total += 1
        return total

    def full_sync(self, nodes: dict[str, FederationNode]) -> int:
        """All-pairs sync — converges any topology in one shot."""
        ids = sorted(nodes)
        n = 0
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                nodes[a].sync_with(nodes[b])
                self.deliveries += 1
                n += 1
        return n


class FileTransport:
    """Offline envelope exchange via spool directories.

    Protocol per node directory:
      outbox/<seq>.jsonl   envelopes this node wants others to apply
      inbox/<peer>.jsonl   envelopes received from peers (append-only)
    A mule process (cron, rsync, git push, human with a USB drive) moves
    outbox files into the peer's inbox. ``drain`` applies whatever landed
    and replies with reciprocal deltas.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def node_dir(self, node_id: str) -> Path:
        d = self.root / node_id
        (d / "outbox").mkdir(parents=True, exist_ok=True)
        (d / "inbox").mkdir(parents=True, exist_ok=True)
        return d

    def emit_digest(self, node: FederationNode) -> Path:
        env = node.digest_envelope()
        d = self.node_dir(node.node_id)
        p = d / "outbox" / f"digest-{node.clock.now().replace('.', '_')}.json"
        p.write_text(json.dumps(env))
        return p

    def emit_delta(self, node: FederationNode,
                   their_digest_env: dict) -> Path:
        env = node.delta_envelope_for(their_digest_env)
        d = self.node_dir(node.node_id)
        p = d / "outbox" / f"delta-{node.clock.now().replace('.', '_')}.json"
        p.write_text(json.dumps(env))
        return p

    def emit_delta_and_digest(self, node: FederationNode,
                              their_digest_env: dict) -> tuple[Path, Path]:
        """Answer a peer's digest with BOTH: the delta they are missing and
        our own digest so they can reciprocate. One mule round-trip then
        achieves two-way convergence."""
        delta_path = self.emit_delta(node, their_digest_env)
        digest_path = self.emit_digest(node)
        return delta_path, digest_path

    def deliver(self, from_node: FederationNode, to_node_id: str,
                envelope_path: Path) -> Path:
        """The 'mule': copy an outbox file into a peer's inbox."""
        d = self.node_dir(to_node_id)
        dst = d / "inbox" / f"{from_node.node_id}-{envelope_path.name}"
        dst.write_text(envelope_path.read_text())
        return dst

    def drain(self, node: FederationNode) -> dict:
        """Apply every inbox envelope; answer digests with reciprocal
        deltas (also placed in outbox for the mule to pick up)."""
        d = self.node_dir(node.node_id)
        applied = digests = 0
        for f in sorted((d / "inbox").glob("*.json")):
            env = json.loads(f.read_text())
            if env.get("type") == "delta":
                node.apply_delta_envelope(env)
                applied += 1
                f.unlink()                    # envelopes are one-shot
            elif env.get("type") == "digest":
                self.emit_delta_and_digest(node, env)
                digests += 1
                f.unlink()
        return {"deltas_applied": applied, "digests_answered": digests}

    def mule(self, nodes: dict[str, FederationNode]) -> int:
        """Move every outbox envelope to its destination's inbox (the
        physical channel — in real deployments this is rsync/git/sneakernet).
        Deltas carry a `to` address and are routed; digests broadcast."""
        moved = 0
        for node_id, node in nodes.items():
            d = self.node_dir(node_id)
            for f in sorted((d / "outbox").glob("*.json")):
                env = json.loads(f.read_text())
                dest = env.get("to") or None
                targets = [dest] if dest else [p for p in nodes
                                                if p != node_id]
                for peer_id in targets:
                    if peer_id != node_id and peer_id in nodes:
                        self.deliver(node, peer_id, f)
                        moved += 1
                f.unlink()
        return moved

    def exchange(self, nodes: dict[str, FederationNode],
                 rounds: int = 3) -> None:
        """Offline gossip: post digests, mule, drain — repeat until the
        outboxes stay empty. Converges regardless of delivery order."""
        for node in nodes.values():
            self.emit_digest(node)
        for _ in range(rounds):
            moved = self.mule(nodes)
            if moved == 0:
                break
            for node in nodes.values():
                self.drain(node)

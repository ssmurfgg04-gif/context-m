"""FederationNode — identity, delta signing/verification, anti-entropy sync.

Threat model (documented honestly):
* members share a symmetric federation key (HMAC-SHA256); every delta is
  signed and the sender must be a known member id;
* signatures authenticate origin + integrity, NOT freshness — replaying an
  old delta is harmless (merge is idempotent) but replay of a *valid*
  signed delta is possible; monotonic HLC stamps bound how far a replayed
  delta can move state;
* this is transport-agnostic: deltas are JSON envelopes handed to whatever
  transport is configured (in-memory mesh for tests, files for offline
  federation, HTTP/TCP in production).
"""

from __future__ import annotations

import hashlib
import hmac
import json

from context_m.federation.crdt import FederationState
from context_m.federation.hlc import HLC


def _canonical_env(env: dict) -> bytes:
    return json.dumps(env, sort_keys=True, separators=(",", ":")).encode()


def sign_envelope(env: dict, key: str) -> str:
    return hmac.new(key.encode(), _canonical_env(env),
                    hashlib.sha256).hexdigest()


class FederationError(Exception):
    pass


class FederationNode:
    """One replica of the federated memory state."""

    def __init__(self, node_id: str, members: list[str] | None = None,
                 federation_key: str = "default-fed-key",
                 buckets: int = 64,
                 state: FederationState | None = None) -> None:
        self.node_id = node_id
        self.members = set(members or [node_id])
        self.members.add(node_id)
        self.federation_key = federation_key
        self.clock = HLC(node_id)
        self.state = state or FederationState(node_id, buckets)
        self.bytes_sent = 0
        self.bytes_received = 0
        self.syncs = 0

    # ------------------------------------------------------------ writes
    def put_fact(self, key: str, payload: dict) -> str:
        return self.state.put(key, payload, self.clock)

    def retract(self, key: str, purge: bool = False) -> str:
        return self.state.put(key, {}, self.clock,
                              tombstone=True, purge=purge)

    # ------------------------------------------------------------ digests
    def digest_envelope(self) -> dict:
        env = {"type": "digest", "from": self.node_id,
               "clock": self.clock.now(),
               "digest": self.state.digest()}
        env["sig"] = sign_envelope({k: v for k, v in env.items()},
                                   self.federation_key)
        return env

    # ------------------------------------------------------------ deltas
    def delta_envelope_for(self, their_digest_env: dict) -> dict:
        self._verify(their_digest_env, expected_type="digest")
        # absorb sender's clock — keeps our stamps causally after theirs
        self.clock.receive(their_digest_env["clock"])
        their_digest = their_digest_env["digest"]
        delta = self.state.delta_for(their_digest)
        env = {"type": "delta", "from": self.node_id,
               "to": their_digest_env["from"],
               "clock": self.clock.now(),
               "delta": delta}
        env["sig"] = sign_envelope({k: v for k, v in env.items()},
                                   self.federation_key)
        return env

    def apply_delta_envelope(self, env: dict) -> int:
        self._verify(env, expected_type="delta")
        if env.get("to") not in (None, self.node_id):
            raise FederationError(f"delta addressed to {env.get('to')}")
        self.clock.receive(env["clock"])
        n = self.state.apply_delta(env["delta"])
        return n

    def _verify(self, env: dict, expected_type: str) -> None:
        if env.get("type") != expected_type:
            raise FederationError(f"expected {expected_type}, got "
                                  f"{env.get('type')}")
        sender = env.get("from")
        if sender not in self.members:
            raise FederationError(f"unknown sender: {sender}")
        sig = env.pop("sig", None)
        if sig is None:
            raise FederationError("unsigned envelope rejected")
        expect = sign_envelope(env, self.federation_key)
        if not hmac.compare_digest(sig, expect):
            raise FederationError(f"bad signature from {sender}")
        env["sig"] = sig  # restore

    # ------------------------------------------------------------ sync
    def sync_with(self, other: "FederationNode") -> dict:
        """Two-way anti-entropy: digest exchange then both-direction deltas.

        Order (A=self, B=other):
          A --digest--> B ; B --delta(for A)--> A ; A applies
          B --digest--> A ; A --delta(for B)--> B ; B applies
        After both half-rounds the states converge (union merge + OR-set
        resolution is commutative). Returns a small accounting dict.
        """
        a_bytes = b_bytes = 0

        d1 = self.digest_envelope()
        delta1 = other.delta_envelope_for(d1)
        a_bytes += len(json.dumps(d1))
        b_bytes += len(json.dumps(delta1))
        self.bytes_received += len(json.dumps(delta1))
        other.bytes_sent += len(json.dumps(delta1))
        applied_self = self.apply_delta_envelope(delta1)

        d2 = other.digest_envelope()
        delta2 = self.delta_envelope_for(d2)
        b_bytes += len(json.dumps(d2))
        a_bytes += len(json.dumps(delta2))
        other.bytes_received += len(json.dumps(delta2))
        self.bytes_sent += len(json.dumps(delta2))
        applied_other = other.apply_delta_envelope(delta2)

        self.syncs += 1
        other.syncs += 1
        return {"a_sent_bytes": a_bytes, "b_sent_bytes": b_bytes,
                "a_applied": applied_self, "b_applied": applied_other}

    def converged_with(self, other: "FederationNode") -> bool:
        return (self.state.canonical_bytes() ==
                other.state.canonical_bytes())

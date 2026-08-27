"""Federation — multi-node memory replication and schema aggregation.

Two distinct capabilities (do not conflate them):

1. CRDT REPLICATION (this package's core): conflict-free replicated
   bi-temporal memory state. Nodes exchange HMAC-signed digest/delta
   envelopes over any transport; union merge + HLC-stamped OR-set
   resolution guarantees deterministic convergence with no coordinator,
   no locks, and no lost retraction semantics.

2. SCHEMA AGGREGATION (``schema_report``): the privacy-preserving
   Semantic Flywheel — opt-in nodes contribute relation histograms only.
"""

from context_m.federation.crdt import (
    DEFAULT_BUCKETS,
    FederationState,
    FactVersion,
    fact_key,
)
from context_m.federation.fabric import (
    apply_to_store,
    export_to_crdt,
    node_from_store,
)
from context_m.federation.hlc import HLC, parse_stamp
from context_m.federation.node import FederationError, FederationNode
from context_m.federation.schema_report import (
    export_schema_report,
    merge_schema_reports,
)
from context_m.federation.transport import FileTransport, InMemoryMesh

__all__ = [
    "DEFAULT_BUCKETS", "FederationState", "FactVersion", "fact_key",
    "HLC", "parse_stamp", "FederationNode", "FederationError",
    "InMemoryMesh", "FileTransport", "export_to_crdt", "apply_to_store",
    "node_from_store", "export_schema_report", "merge_schema_reports",
]

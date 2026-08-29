/**
 * Storage interface module — for DSH plugins that only need the
 * storage subset (no session replay/fork).
 *
 * Re-exports just the storage methods from the main plugin entry.
 * Useful when a DSH tool plugin wants memory but doesn't need
 * session-replay semantics.
 */
export { default as default } from "./index.js";

export const storage = {
  add: "contextm_add",
  search: "contextm_search",
  structural_query: "contextm_structural_query",
  consolidate: "contextm_consolidate",
  export_provenance: "contextm_export_provenance",
  audit: "contextm_audit",
};

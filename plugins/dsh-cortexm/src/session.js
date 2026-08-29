/**
 * Session interface module — for DSH plugins that want session-replay
 * + fork + trajectory view (Reddit deep-dive ≥10 mentions for
 * "replay" / "trajectory view" / "session log" / "fork", 2026-08-29).
 *
 * Context-M's audit log already has every event (BLAKE3-chained,
 * append-only, bi-temporal). Replay is just re-emitting events in
 * order. Fork is copying up to a tx-id and continuing with a new
 * run_id. Trajectory is a visualizable projection.
 */
export const session = {
  replay: "contextm_replay",
  fork: "contextm_fork",
  trajectory: "contextm_trajectory",
  inspect: "contextm_inspect",
};

export { default as default } from "./index.js";

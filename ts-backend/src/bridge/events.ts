/** Discriminated-union event types emitted to the Rust orchestrator via NDJSON. */

export type BridgeEvent =
  | { t: "thinking"; item_id: string; content: string }
  | { t: "goal_iteration"; iteration: number; objective: string }
  | { t: "tool_call"; id: string; name: string; args: unknown }
  | { t: "tool_result"; id: string; name: string; result: unknown }
  | { t: "text"; content: string }
  | { t: "summary"; content: string }
  | { t: "usage"; input_tokens: number; output_tokens: number }
  | { t: "goal_status"; status: string; objective: string }
  | { t: "done"; session_id: string }
  | { t: "error"; message: string };

/** Serialize a BridgeEvent to a newline-terminated JSON string. */
export function toNdjson(e: BridgeEvent): string {
  return JSON.stringify(e) + "\n";
}

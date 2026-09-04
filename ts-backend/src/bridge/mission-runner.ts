/**
 * Bun entry-point for the sandboxed.sh bridge.
 *
 * Protocol:
 *   stdin  → one JSON header line: { session_id, directory, model, agent, message }
 *   stdout → NDJSON stream of BridgeEvent objects (one per line)
 *
 * Usage:
 *   echo '{"session_id":"s1",...}' | bun run src/bridge/mission-runner.ts
 */

import { toNdjson, type BridgeEvent } from "./events.js";
import { runMission, type MissionHeader } from "./run-mission.js";

function emit(e: BridgeEvent): void {
  process.stdout.write(toNdjson(e));
}

async function readFirstLine(): Promise<string> {
  // Read stdin line-by-line and return the first non-empty line.
  for await (const line of console) {
    const trimmed = (line as string).trim();
    if (trimmed.length > 0) return trimmed;
  }
  throw new Error("stdin closed before a header line was received");
}

async function main(): Promise<void> {
  const raw = await readFirstLine();

  let header: MissionHeader;
  try {
    header = JSON.parse(raw) as MissionHeader;
  } catch {
    emit({ t: "error", message: `Invalid JSON header: ${raw}` });
    process.exit(1);
  }

  await runMission(header, emit);
}

main().catch((err: unknown) => {
  const message = err instanceof Error ? err.message : String(err);
  emit({ t: "error", message });
  process.exit(1);
});

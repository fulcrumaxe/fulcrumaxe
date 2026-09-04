import type { BridgeEvent } from "./events.js";

export interface MissionHeader {
  session_id: string;
  directory: string;
  model: string | null;
  agent: string | null;
  message: string;
}

export type Emit = (e: BridgeEvent) => void;

/**
 * Run a mission given a parsed header, emitting BridgeEvents via `emit`.
 *
 * Two modes:
 * - Default (scripted): a deterministic, zero-cost event sequence for wiring/CI.
 * - Real (`AF_REAL=1`): spawns a real Claude Code agent (`claude -p` streaming
 *   JSON) and re-emits its events as BridgeEvents — the "real agent through the
 *   bridge" path. Draws on the Agent SDK credit, so it's opt-in.
 *
 * PLUG POINT: the real path runs a single agent today. The full Team-Lead loop
 * (spawn executor → reviewers → merge, parsing each AGENT_OUTPUT envelope) slots
 * in here, re-emitting their events through the same `emit`.
 */
export async function runMission(header: MissionHeader, emit: Emit): Promise<void> {
  if (process.env.AF_REAL === "1") {
    return runMissionReal(header, emit);
  }
  return runMissionScripted(header, emit);
}

async function runMissionScripted(header: MissionHeader, emit: Emit): Promise<void> {
  emit({ t: "goal_iteration", iteration: 1, objective: header.message });
  emit({ t: "thinking", item_id: "tl-1", content: "Team Lead routing the mission." });
  emit({
    t: "tool_call",
    id: "spawn-exec-1",
    name: "spawn:executor",
    args: { discussion: null, task: header.message },
  });
  emit({
    t: "tool_result",
    id: "spawn-exec-1",
    name: "spawn:executor",
    result: { agent: "executor", verdict: "done", pr: null },
  });
  emit({ t: "text", content: "Executor implemented the change." });
  emit({ t: "summary", content: "Mission complete." });
  emit({ t: "usage", input_tokens: 1000, output_tokens: 200 });
  emit({ t: "goal_status", status: "complete", objective: header.message });
  emit({ t: "done", session_id: header.session_id });
}

/** Real mode: drive a real `claude -p` agent and re-emit its stream as BridgeEvents. */
async function runMissionReal(header: MissionHeader, emit: Emit): Promise<void> {
  emit({ t: "goal_iteration", iteration: 1, objective: header.message });
  emit({ t: "thinking", item_id: "tl-1", content: "Team Lead spawning a real agent for this mission." });

  const bin = process.env.AF_CLAUDE_BIN || "claude";
  const model = process.env.AF_MODEL || "claude-haiku-4-5"; // cheap by default
  const maxTurns = process.env.AF_MAX_TURNS || "1";
  const args = [
    "-p",
    header.message,
    "--output-format",
    "stream-json",
    "--verbose",
    "--max-turns",
    maxTurns,
    "--model",
    model,
  ];

  const proc = Bun.spawn([bin, ...args], {
    cwd: header.directory || ".",
    stdout: "pipe",
    stderr: "pipe",
  });

  let finalText = "";
  let inTok = 0;
  let outTok = 0;

  const handleLine = (line: string): void => {
    const trimmed = line.trim();
    if (!trimmed) return;
    let ev: Record<string, unknown>;
    try {
      ev = JSON.parse(trimmed) as Record<string, unknown>;
    } catch {
      return;
    }
    const type = ev.type as string | undefined;
    const message = ev.message as Record<string, unknown> | undefined;

    if (type === "assistant" && Array.isArray(message?.content)) {
      for (const block of message!.content as Array<Record<string, unknown>>) {
        if (block.type === "text" && typeof block.text === "string" && block.text) {
          finalText = block.text;
          emit({ t: "text", content: block.text });
        } else if (block.type === "tool_use") {
          emit({
            t: "tool_call",
            id: String(block.id ?? "tool"),
            name: String(block.name ?? "tool"),
            args: (block.input as unknown) ?? {},
          });
        }
      }
      const u = message!.usage as Record<string, number> | undefined;
      if (u) {
        inTok += u.input_tokens ?? 0;
        outTok += u.output_tokens ?? 0;
      }
    } else if (type === "user" && Array.isArray(message?.content)) {
      for (const block of message!.content as Array<Record<string, unknown>>) {
        if (block.type === "tool_result") {
          emit({
            t: "tool_result",
            id: String(block.tool_use_id ?? "tool"),
            name: "tool",
            result: (block.content as unknown) ?? null,
          });
        }
      }
    } else if (type === "result") {
      if (typeof ev.result === "string" && ev.result) finalText = ev.result as string;
      const u = ev.usage as Record<string, number> | undefined;
      emit({
        t: "usage",
        input_tokens: u?.input_tokens ?? inTok,
        output_tokens: u?.output_tokens ?? outTok,
      });
    }
  };

  const decoder = new TextDecoder();
  let buf = "";
  const reader = proc.stdout.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n")) >= 0) {
      handleLine(buf.slice(0, idx));
      buf = buf.slice(idx + 1);
    }
  }
  if (buf.trim()) handleLine(buf);

  const code = await proc.exited;
  if (code !== 0) {
    const err = await new Response(proc.stderr).text();
    emit({ t: "error", message: `claude exited ${code}: ${err.slice(0, 300)}` });
  }
  emit({ t: "summary", content: finalText || "Mission complete." });
  emit({ t: "goal_status", status: code === 0 ? "complete" : "failed", objective: header.message });
  emit({ t: "done", session_id: header.session_id });
}

/**
 * spawn/runtime/opencode-runtime.ts — Adapter that runs a role prompt via opencode/Qwen.
 *
 * Spawns `opencode run -m <model> --dangerously-skip-permissions <prompt>`
 * as an ARGV array (no shell interpolation), captures stdout, strips ANSI
 * escape sequences, and parses the trailing AGENT_OUTPUT JSON envelope if
 * present.
 *
 * Gated by AF_RUNTIME=opencode (or by the caller passing runtime:"opencode").
 * Default model: opencode-go/qwen3.7-max.
 *
 * Usage:
 *   import { runOpencodeRole } from "./runtime/opencode-runtime.js";
 *   const result = await runOpencodeRole({
 *     prompt: "...",
 *     role: "executor",
 *     model: "opencode-go/qwen3.7-max", // optional
 *     cwd: "/path/to/scratch-workspace", // optional
 *   });
 */

import { spawn } from "node:child_process";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const DEFAULT_OPENCODE_MODEL = "opencode-go/qwen3.7-max";

// ANSI escape sequence regex — strips colour codes, cursor moves, etc.
// The \x1b (ESC) control character is intentional here: it's the actual byte
// terminals emit for escape sequences, not an accidental control char in a
// user-facing pattern.
// eslint-disable-next-line no-control-regex
const ANSI_RE = /\x1b\[[0-9;]*[A-Za-z]/g;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface OpencodeRoleParams {
  /** The assembled spawn prompt to send to the agent. */
  prompt: string;
  /** Role name — used for logging and AGENT_OUTPUT tagging. */
  role: string;
  /** opencode model string in `provider/model` format. Defaults to DEFAULT_OPENCODE_MODEL. */
  model?: string;
  /** Working directory for the opencode process. Defaults to process.cwd(). */
  cwd?: string;
  /**
   * Additional env vars to pass to the subprocess.
   * Merged on top of the inherited process.env.
   */
  env?: Record<string, string>;
}

export interface OpencodeRoleResult {
  /** Full stdout from opencode (ANSI-stripped). */
  output: string;
  /**
   * Parsed AGENT_OUTPUT envelope JSON if the agent emitted one in its final
   * message; null if no envelope was found or it could not be parsed.
   */
  agentOutput: Record<string, unknown> | null;
  /** Exit code from the opencode process. */
  exitCode: number;
}

// ---------------------------------------------------------------------------
// AGENT_OUTPUT envelope parser
// ---------------------------------------------------------------------------

/**
 * Extract the AGENT_OUTPUT JSON block from agent stdout.
 *
 * Agents embed their structured output as:
 *   <!-- AGENT_OUTPUT -->
 *   ```json
 *   { ... }
 *   ```
 *   <!-- /AGENT_OUTPUT -->
 *
 * We find the last occurrence in case there are multiple partial outputs.
 */
function parseAgentOutput(text: string): Record<string, unknown> | null {
  const openTag = "<!-- AGENT_OUTPUT -->";
  const closeTag = "<!-- /AGENT_OUTPUT -->";

  let searchFrom = 0;
  let lastStart = -1;
  let lastEnd = -1;

  while (true) {
    const s = text.indexOf(openTag, searchFrom);
    if (s === -1) break;
    const e = text.indexOf(closeTag, s + openTag.length);
    if (e === -1) break;
    lastStart = s;
    lastEnd = e + closeTag.length;
    searchFrom = lastEnd;
  }

  if (lastStart === -1) return null;

  const block = text.slice(lastStart + openTag.length, lastEnd - closeTag.length);

  // Strip optional ```json ... ``` fences
  const fenced = block.match(/```(?:json)?\s*([\s\S]*?)```/);
  const jsonText = fenced ? fenced[1]! : block;

  try {
    const parsed = JSON.parse(jsonText.trim());
    if (typeof parsed === "object" && parsed !== null) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // Malformed JSON — return null
  }
  return null;
}

// ---------------------------------------------------------------------------
// Main adapter function
// ---------------------------------------------------------------------------

/**
 * Run a role prompt via opencode.
 *
 * Spawns:
 *   opencode run -m <model> --dangerously-skip-permissions <prompt>
 *
 * ARGV array — no shell; prompt is passed as a single string argument.
 * stdout is collected and returned in `output` (ANSI-stripped).
 * stderr is forwarded to the caller's process.stderr for observability.
 */
export async function runOpencodeRole(
  params: OpencodeRoleParams
): Promise<OpencodeRoleResult> {
  const {
    prompt,
    role,
    model = DEFAULT_OPENCODE_MODEL,
    cwd = process.cwd(),
    env,
  } = params;

  const argv = [
    "run",
    "-m", model,
    "--dangerously-skip-permissions",
    prompt,
  ];

  process.stderr.write(
    `[opencode-runtime] spawning opencode for role=${role} model=${model} cwd=${cwd}\n`
  );

  const chunks: Buffer[] = [];

  const mergedEnv: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v !== undefined) mergedEnv[k] = v;
  }
  if (env) {
    for (const [k, v] of Object.entries(env)) {
      mergedEnv[k] = v;
    }
  }

  return new Promise<OpencodeRoleResult>((resolve) => {
    const child = spawn("opencode", argv, {
      cwd,
      env: mergedEnv,
      stdio: ["ignore", "pipe", "pipe"],
    });

    child.stdout.on("data", (chunk: Buffer) => {
      chunks.push(chunk);
    });

    child.stderr.on("data", (chunk: Buffer) => {
      // Forward stderr for observability
      process.stderr.write(chunk);
    });

    child.on("close", (code: number | null) => {
      const rawOutput = Buffer.concat(chunks).toString("utf-8");
      const cleanOutput = rawOutput.replace(ANSI_RE, "");

      const exitCode = code ?? 1;

      if (exitCode !== 0) {
        process.stderr.write(
          `[opencode-runtime] opencode exited ${exitCode} for role=${role}\n`
        );
      } else {
        process.stderr.write(
          `[opencode-runtime] opencode completed for role=${role}\n`
        );
      }

      const agentOutput = parseAgentOutput(cleanOutput);

      resolve({ output: cleanOutput, agentOutput, exitCode });
    });

    child.on("error", (err: Error) => {
      process.stderr.write(
        `[opencode-runtime] opencode spawn error for role=${role}: ${err.message}\n`
      );
      resolve({ output: "", agentOutput: null, exitCode: 1 });
    });
  });
}

// ---------------------------------------------------------------------------
// Gate check — is the opencode runtime enabled?
// ---------------------------------------------------------------------------

/**
 * Returns true when AF_RUNTIME=opencode is set in the environment.
 * Used by spawn-agent.ts to decide whether to invoke the adapter.
 */
export function isOpencodeRuntimeEnabled(): boolean {
  return process.env["AF_RUNTIME"] === "opencode";
}

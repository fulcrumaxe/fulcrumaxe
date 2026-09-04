/**
 * spawn/discussion-status.ts — Shared helper for parsing STATUS from Discussion bodies.
 *
 * Faithful 1:1 port of backend/discussion_status.py (186 LOC).
 *
 * STATUS lines look like:
 *   <!-- STATUS:SPEC_READY SINCE:2026-05-09T00:00:00Z -->
 *   <!-- STATUS:IMPLEMENTING SINCE:2026-05-09T01:00:00Z -->
 *   <!-- STATUS:REVIEWING PR:#321 SINCE:2026-05-09T02:00:00Z -->
 *   <!-- STATUS:DONE PR:#321 SINCE:2026-05-09T03:00:00Z -->
 *
 * Mirrors:
 *   extract_status(body)      → extractStatus(body): string
 *   extract_linked_pr(body)   → extractLinkedPr(body): number | null
 *   extract_since(body)       → extractSince(body): string | null
 *   set_status(body, new_status, now_iso) → setStatus(body, newStatus, nowIso?): string
 *   get_sections(body)        → getSections(body): SectionMap
 *   missing_sections(body)    → missingSections(body): string[]
 *
 * CLI (mirroring Python exactly):
 *   bun run src/spawn/discussion-status.ts get-sections <N>
 *   bun run src/spawn/discussion-status.ts missing-sections <N>
 */

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

// ---------------------------------------------------------------------------
// Patterns — mirrors Python regex constants exactly
// ---------------------------------------------------------------------------

const _STATUS_PATTERN = /<!--\s*STATUS:(\w+)/;
const _PR_PATTERN = /<!--\s*STATUS:[^>]*PR:#(\d+)/;
const _SINCE_PATTERN = /<!--\s*STATUS:[^>]*SINCE:([^\s>]+)/;

// Matches any of the three required ## headers at line start — mirrors Python _SECTION_HEADER_RE
const _SECTION_HEADER_RE =
  /^##\s+(Intent|Spec \(Acceptance\)|Implementation Notes)/gm;

// Strip the leading STATUS comment from legacy bodies — mirrors _STATUS_COMMENT_RE
const _STATUS_COMMENT_RE = /<!--.*?-->/gs;

// Full STATUS comment block for replacement — mirrors _FULL_MARKER_RE inside set_status()
const _FULL_MARKER_RE = /<!--\s*STATUS:[^>]*-->/;

// ---------------------------------------------------------------------------
// Public constants — mirrors Python VALID_STATUSES and REQUIRED_SECTIONS
// ---------------------------------------------------------------------------

export const VALID_STATUSES = new Set([
  "DISCUSSING",
  "SPEC_READY",
  "IMPLEMENTING",
  "REVIEWING",
  "DONE",
  "CLOSED",
]);

export const REQUIRED_SECTIONS: string[] = [
  "Intent",
  "Spec (Acceptance)",
  "Implementation Notes",
];

// Map from header display name to dict key — mirrors _SECTION_KEYS
const _SECTION_KEYS: Record<string, string> = {
  "Intent": "intent",
  "Spec (Acceptance)": "spec",
  "Implementation Notes": "implementation_notes",
};

// ---------------------------------------------------------------------------
// Public API — mirrors Python functions exactly
// ---------------------------------------------------------------------------

/**
 * Return the STATUS value from a Discussion body, or 'UNKNOWN'.
 * Mirrors: extract_status(body) → str
 */
export function extractStatus(body: string): string {
  const m = _STATUS_PATTERN.exec(body ?? "");
  return m ? m[1]! : "UNKNOWN";
}

/**
 * Return the linked PR number from the STATUS line, or null.
 * Mirrors: extract_linked_pr(body) → Optional[int]
 */
export function extractLinkedPr(body: string): number | null {
  const m = _PR_PATTERN.exec(body ?? "");
  return m ? parseInt(m[1]!, 10) : null;
}

/**
 * Return the SINCE timestamp from the STATUS line, or null.
 * Mirrors: extract_since(body) → Optional[str]
 */
export function extractSince(body: string): string | null {
  const m = _SINCE_PATTERN.exec(body ?? "");
  return m ? m[1]! : null;
}

/**
 * Return body with the STATUS marker set to newStatus.
 *
 * - If a <!-- STATUS:... --> marker already exists it is replaced in-place.
 * - If no marker exists the new marker is prepended to the body (two newlines after it).
 *
 * nowIso defaults to the current UTC instant formatted as ISO 8601 (YYYY-MM-DDTHH:MM:SSZ).
 * Pass a fixed string in tests.
 *
 * Mirrors: set_status(body, new_status, now_iso=None) → str
 */
export function setStatus(
  body: string,
  newStatus: string,
  nowIso?: string
): string {
  if (nowIso === undefined) {
    const d = new Date();
    const pad = (n: number): string => String(n).padStart(2, "0");
    nowIso =
      `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
      `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}Z`;
  }

  const newMarker = `<!-- STATUS:${newStatus} SINCE:${nowIso} -->`;
  const safeBody = body ?? "";

  if (_FULL_MARKER_RE.test(safeBody)) {
    // Replace the existing marker (count=1 — mirrors Python sub(..., count=1))
    return safeBody.replace(_FULL_MARKER_RE, newMarker);
  } else {
    // No marker present — prepend it
    return newMarker + "\n\n" + safeBody;
  }
}

export interface SectionMap {
  intent: string;
  spec: string;
  implementation_notes: string;
}

/**
 * Parse the three-section spec template from a Discussion body.
 *
 * Returns a SectionMap with keys `intent`, `spec`, `implementation_notes`.
 *
 * If the body uses the new three-section format (all headers present), each
 * value contains the text between that header and the next ## header (or EOF).
 *
 * Back-compat: if none of the three section headers are found (legacy body),
 * returns {intent:"", spec:<full body without status comment>, implementation_notes:""}.
 *
 * Partial format: headers that exist are parsed normally; missing ones return "".
 *
 * Mirrors: get_sections(body) → dict[str, str]
 */
export function getSections(body: string): SectionMap {
  const safeBody = body ?? "";

  // Collect all matches — mirrors Python list(_SECTION_HEADER_RE.finditer(body))
  const headerRe = new RegExp(_SECTION_HEADER_RE.source, "gm");
  const matches: Array<{ index: number; end: number; headerName: string }> = [];
  let m: RegExpExecArray | null;
  while ((m = headerRe.exec(safeBody)) !== null) {
    matches.push({
      index: m.index,
      end: m.index + m[0].length,
      headerName: m[1]!,
    });
  }

  // Legacy body — no section headers at all
  if (matches.length === 0) {
    const stripped = safeBody.replace(_STATUS_COMMENT_RE, "").trim();
    return { intent: "", spec: stripped, implementation_notes: "" };
  }

  const result: SectionMap = {
    intent: "",
    spec: "",
    implementation_notes: "",
  };

  for (let i = 0; i < matches.length; i++) {
    const match = matches[i]!;
    const key = _SECTION_KEYS[match.headerName];
    if (key === undefined) continue;
    const contentStart = match.end;
    const contentEnd =
      i + 1 < matches.length ? matches[i + 1]!.index : safeBody.length;
    (result as unknown as Record<string, string>)[key] = safeBody
      .slice(contentStart, contentEnd)
      .trim();
  }

  return result;
}

/**
 * Return the names of required section headers absent from body.
 *
 * Uses the display names from REQUIRED_SECTIONS:
 *   ["Intent", "Spec (Acceptance)", "Implementation Notes"]
 *
 * Returns an empty list when all three headers are present.
 *
 * Mirrors: missing_sections(body) → list[str]
 */
export function missingSections(body: string): string[] {
  const safeBody = body ?? "";
  const headerRe = new RegExp(_SECTION_HEADER_RE.source, "gm");
  const found = new Set<string>();
  let m: RegExpExecArray | null;
  while ((m = headerRe.exec(safeBody)) !== null) {
    found.add(m[1]!);
  }
  return REQUIRED_SECTIONS.filter((s) => !found.has(s));
}

// ---------------------------------------------------------------------------
// _fetch_body — mirrors Python _fetch_body(discussion_num)
// Fetches a Discussion body via discussion_cache (TS CLI).
// ---------------------------------------------------------------------------

function fetchBody(discussionNum: number): string {
  // Resolve path to discussion-cache.ts relative to this file
  // This file: ts-backend/src/spawn/discussion-status.ts
  // Cache script: ts-backend/src/spawn/discussion-cache.ts
  const cacheScript =
    process.env["DISCUSSION_CACHE_SCRIPT"] ??
    join(new URL(import.meta.url).pathname, "..", "discussion-cache.ts");

  const cacheScriptExists = existsSync(cacheScript);
  if (!cacheScriptExists) {
    // Fallback: try Python cache script (mirrors original Python impl)
    const pyScript = join(
      new URL(import.meta.url).pathname,
      "..",
      "..",
      "..",
      "..",
      "backend",
      "discussion_cache.py"
    );
    try {
      // Use execFileSync (no shell) to mirror Python's subprocess.run([sys.executable, cache_script, "get-body", str(n)], ...)
      // This prevents shell metacharacter injection via discussionNum or script path.
      const stdout = execFileSync("python3", [pyScript, "get-body", String(discussionNum)], {
        timeout: 30_000,
        encoding: "utf-8",
        stdio: ["pipe", "pipe", "pipe"],
      });
      return stdout;
    } catch {
      return "";
    }
  }

  try {
    // Use execFileSync (no shell) to mirror Python's subprocess.run([sys.executable, cache_script, "get-body", str(n)], ...)
    const stdout = execFileSync("bun", [cacheScript, "get-body", String(discussionNum)], {
      timeout: 30_000,
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "pipe"],
    });
    return stdout;
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// CLI entrypoint — mirrors Python __main__ exactly
// ---------------------------------------------------------------------------

if (import.meta.main) {
  if (process.argv.length < 3) {
    process.stderr.write(
      `Usage: ${process.argv[1]} <get-sections|missing-sections> <discussion_number>\n`
    );
    process.exit(1);
  }

  const subcommand = process.argv[2]!;
  const discussionNum = parseInt(process.argv[3] ?? "", 10);

  if (isNaN(discussionNum)) {
    process.stderr.write(
      `Usage: ${process.argv[1]} <get-sections|missing-sections> <discussion_number>\n`
    );
    process.exit(1);
  }

  const body = fetchBody(discussionNum);

  if (subcommand === "get-sections") {
    console.log(JSON.stringify(getSections(body)));
  } else if (subcommand === "missing-sections") {
    const missing = missingSections(body);
    if (missing.length > 0) {
      const names = missing.join(", ");
      process.stderr.write(
        `WARN: discussion #${discussionNum} body missing section: ${names}\n`
      );
    }
    // stdout: JSON list — mirrors Python exactly
    console.log(JSON.stringify(missing));
  } else {
    process.stderr.write(`Unknown subcommand: ${subcommand}\n`);
    process.exit(1);
  }
}

/**
 * tests/loop/loop-phased-step5.parity.test.ts
 *
 * Parity tests for loop/loop-phased-step5.ts.
 *
 * Mirrors scripts/loop-phased-step5.sh (1141 LOC bash) 1:1.
 *
 * # What IS parity-tested
 *   - mergeGateAllowed(): given a PR's labels → allowed/blocked must match bash
 *   - checkNackLabels(): given a label set → finds/misses NACK labels
 *   - sanitizeDiff(): control-plane token redaction + 8000-char cap
 *   - parsePrStateEntry(): entry JSON → structured fields
 *   - NACK_LABELS constant: exact list matches bash _NACK_LABELS
 *
 * # What is NOT parity-tested (external side effects)
 *   spawnAgent         — bash calls spawn-agent.sh (or mock via SPAWN_AGENT=echo)
 *   gh pr merge        — bash calls gh pr merge --squash (or mock via GH_MERGE=echo)
 *   gh api labels      — REST label mutations
 *   gh pr view labels  — label queries (mocked in hasLabel via HAS_LABEL_*)
 *   pr_state.py        — sqlite operations
 *   consensus_panel.py — GitHub GraphQL queries
 *   rotate-team-log.sh — GitHub issue comment
 *   post-merge-hook.sh — post-merge bookkeeping chain
 *   security-trigger.sh — diff pattern matching (mocked in test mode)
 *   two-gate-check.sh   — PR body markers (mocked in test mode)
 *   panel-helpers.sh    — discussion status mutations
 *
 * Run: cd ts-backend && bun test tests/loop/loop-phased-step5.parity.test.ts
 */

import { describe, it, expect } from "bun:test";
import {
  mergeGateAllowed,
  sanitizeDiff,
  parsePrStateEntry,
  mapSecurityRequiredExitCode,
  NACK_LABELS,
  type MergeGateInput,
} from "../../src/loop/loop-phased-step5.js";

// ---------------------------------------------------------------------------
// NACK_LABELS constant
// ---------------------------------------------------------------------------

describe("NACK_LABELS", () => {
  it("contains all 8 expected labels (matches bash _NACK_LABELS)", () => {
    const expected = [
      "security-needs-fix",
      "security-issue",
      "security-review-needs-fix",
      "code-review-needs-fix",
      "needs-re-review",
      "acceptance-failed",
      "do-not-merge",
      "wip",
    ];
    expect(NACK_LABELS).toHaveLength(expected.length);
    const nackArr: string[] = [...NACK_LABELS];
    for (const label of expected) {
      expect(nackArr).toContain(label);
    }
  });
});

// ---------------------------------------------------------------------------
// mergeGateAllowed() — the core merge-gate parity function
// ---------------------------------------------------------------------------

describe("mergeGateAllowed", () => {
  const baseInput = (): MergeGateInput => ({
    labels: [],
    needsSecurityReview: false,
    securityTriggerDetected: false,
    dashboardTouched: false,
    debaterGateOn: false,
  });

  describe("NACK label blocking", () => {
    for (const nack of NACK_LABELS) {
      it(`blocks merge when NACK label '${nack}' is present — even with all pass labels`, () => {
        const input: MergeGateInput = {
          ...baseInput(),
          labels: ["code-review-passed", "security-review-passed", nack],
          needsSecurityReview: true,
        };
        const result = mergeGateAllowed(input);
        expect(result.allowed).toBe(false);
        expect(result.reason).toContain(nack);
      });
    }

    it("allows merge when no NACK labels are present and all gates pass", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed"],
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
    });
  });

  describe("code-review-passed gate", () => {
    it("blocks merge when code-review-passed is missing", () => {
      const input: MergeGateInput = { ...baseInput(), labels: [] };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(false);
      expect(result.reason).toContain("code-review-passed");
    });

    it("allows merge when code-review-passed is present (no security needed)", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed"],
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
    });
  });

  describe("security-review-passed gate", () => {
    it("blocks when needs_security_review=true and label is absent", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed"],
        needsSecurityReview: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(false);
      expect(result.reason).toContain("security-review-passed");
    });

    it("allows when needs_security_review=true and security-review-passed is present", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed", "security-review-passed"],
        needsSecurityReview: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
    });

    it("blocks when security trigger detected but label is absent", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed"],
        needsSecurityReview: false,
        securityTriggerDetected: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(false);
      expect(result.reason).toContain("security-review-passed");
    });

    it("allows when security trigger detected and security-review-passed is present", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed", "security-review-passed"],
        needsSecurityReview: false,
        securityTriggerDetected: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
    });

    it("does NOT require security-review-passed when neither flag is set", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed"],
        needsSecurityReview: false,
        securityTriggerDetected: false,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
    });

    // HG-7 (D#1588 Batch B) — provenance:external forces security-review-passed
    it("blocks when externalProvenanceForcesSecurity=true and label is absent, even with no other trigger", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed"],
        needsSecurityReview: false,
        securityTriggerDetected: false,
        externalProvenanceForcesSecurity: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(false);
      expect(result.reason).toContain("security-review-passed");
    });

    it("allows when externalProvenanceForcesSecurity=true and security-review-passed is present", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed", "security-review-passed"],
        needsSecurityReview: false,
        securityTriggerDetected: false,
        externalProvenanceForcesSecurity: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
    });
  });

  // ---------------------------------------------------------------------------
  // mapSecurityRequiredExitCode — HG-7 fail-closed exit-code mapping
  // (D#1588 Batch B security-needs-fix round: external_intake_gate.py's
  // `security-required` CLI now distinguishes "confirmed not required" (exit 1)
  // from "fetch failed / unknown" (exit 3). Every call site — including this
  // TS port — MUST treat the fetch-failure signal as "security review IS
  // required", never as "not required". This mirrors the bash mapping in
  // scripts/loop-phased-step5.sh's _external_provenance_forces_security().)
  // ---------------------------------------------------------------------------
  describe("mapSecurityRequiredExitCode (HG-7 fail-closed mapping)", () => {
    it("status 0 (label confirmed present) -> required (true)", () => {
      expect(mapSecurityRequiredExitCode(0)).toBe(true);
    });

    it("status 1 (confirmed NOT required) -> not required (false)", () => {
      expect(mapSecurityRequiredExitCode(1)).toBe(false);
    });

    it("status 3 (fetch failed / unknown) -> fail closed, required (true)", () => {
      expect(mapSecurityRequiredExitCode(3)).toBe(true);
    });

    it("status null (spawn failure, e.g. python3 missing) -> fail closed, required (true)", () => {
      expect(mapSecurityRequiredExitCode(null)).toBe(true);
    });

    it("any other unexpected status (e.g. 2, usage error) -> fail closed, required (true)", () => {
      expect(mapSecurityRequiredExitCode(2)).toBe(true);
      expect(mapSecurityRequiredExitCode(127)).toBe(true);
    });
  });

  describe("browser-test-passed gate", () => {
    it("blocks when dashboard touched but browser-test-passed is absent", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed"],
        dashboardTouched: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(false);
      expect(result.reason).toContain("browser-test-passed");
    });

    it("allows when dashboard touched and browser-test-passed is present", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed", "browser-test-passed"],
        dashboardTouched: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
    });

    it("does NOT require browser-test-passed when dashboard is not touched", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed"],
        dashboardTouched: false,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
    });
  });

  describe("debater-confirmed gate", () => {
    it("blocks when debater gate is on but debater-confirmed is absent", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed"],
        debaterGateOn: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(false);
      expect(result.reason).toContain("debater-confirmed");
    });

    it("allows when debater gate is on and debater-confirmed is present", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed", "debater-confirmed"],
        debaterGateOn: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
    });

    it("does NOT require debater-confirmed when gate is off", () => {
      const input: MergeGateInput = {
        ...baseInput(),
        labels: ["code-review-passed"],
        debaterGateOn: false,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
    });
  });

  describe("all gates combined", () => {
    it("allows merge with all required labels present (full gate set)", () => {
      const input: MergeGateInput = {
        labels: [
          "code-review-passed",
          "security-review-passed",
          "browser-test-passed",
          "debater-confirmed",
        ],
        needsSecurityReview: true,
        securityTriggerDetected: true,
        dashboardTouched: true,
        debaterGateOn: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(true);
      expect(result.reason).toContain("all gate labels present");
    });

    it("blocks on first missing label (NACK takes priority over missing pass labels)", () => {
      const input: MergeGateInput = {
        labels: [
          "code-review-passed",
          "security-review-passed",
          "browser-test-passed",
          "debater-confirmed",
          "wip", // NACK label
        ],
        needsSecurityReview: true,
        securityTriggerDetected: false,
        dashboardTouched: true,
        debaterGateOn: true,
      };
      const result = mergeGateAllowed(input);
      expect(result.allowed).toBe(false);
      expect(result.reason).toContain("wip");
    });
  });
});

// ---------------------------------------------------------------------------
// checkNackLabels() — test-mode label checking
// ---------------------------------------------------------------------------

describe("checkNackLabels", () => {
  // checkNackLabels reads from env vars in test mode (when SPAWN_AGENT=echo).
  // We test the mergeGateAllowed version instead (pure function).

  it("returns null when no NACK labels are present (empty label set)", () => {
    // Use mergeGateAllowed as the parity-testable proxy
    const result = mergeGateAllowed({
      labels: ["code-review-passed"],
      needsSecurityReview: false,
      securityTriggerDetected: false,
      dashboardTouched: false,
      debaterGateOn: false,
    });
    expect(result.allowed).toBe(true);
  });

  it("detects security-needs-fix as a NACK", () => {
    const result = mergeGateAllowed({
      labels: ["code-review-passed", "security-needs-fix"],
      needsSecurityReview: false,
      securityTriggerDetected: false,
      dashboardTouched: false,
      debaterGateOn: false,
    });
    expect(result.allowed).toBe(false);
    expect(result.reason).toContain("security-needs-fix");
  });

  it("detects do-not-merge as a NACK", () => {
    const result = mergeGateAllowed({
      labels: ["code-review-passed", "do-not-merge"],
      needsSecurityReview: false,
      securityTriggerDetected: false,
      dashboardTouched: false,
      debaterGateOn: false,
    });
    expect(result.allowed).toBe(false);
    expect(result.reason).toContain("do-not-merge");
  });
});

// ---------------------------------------------------------------------------
// sanitizeDiff()
// ---------------------------------------------------------------------------

describe("sanitizeDiff", () => {
  it("redacts AGENT_OUTPUT token", () => {
    const result = sanitizeDiff("before AGENT_OUTPUT after");
    expect(result).not.toContain("AGENT_OUTPUT");
    expect(result).toContain("[REDACTED-TOKEN]");
  });

  it("redacts SPAWN_REQUEST token", () => {
    const result = sanitizeDiff("before SPAWN_REQUEST after");
    expect(result).not.toContain("SPAWN_REQUEST");
    expect(result).toContain("[REDACTED-TOKEN]");
  });

  it("redacts TERMINATE_REQUEST token", () => {
    const result = sanitizeDiff("before TERMINATE_REQUEST after");
    expect(result).not.toContain("TERMINATE_REQUEST");
    expect(result).toContain("[REDACTED-TOKEN]");
  });

  it("redacts STATUS:SPEC_READY marker", () => {
    const result = sanitizeDiff("body STATUS:SPEC_READY more");
    expect(result).not.toContain("STATUS:SPEC_READY");
    expect(result).toContain("[REDACTED-STATUS]");
  });

  it("redacts STATUS:DONE marker", () => {
    const result = sanitizeDiff("STATUS:DONE");
    expect(result).toContain("[REDACTED-STATUS]");
  });

  it("caps diff at 8000 chars and appends truncation notice", () => {
    const longDiff = "x".repeat(9000);
    const result = sanitizeDiff(longDiff);
    expect(result.length).toBeLessThanOrEqual(8000 + 50); // some slack for the notice
    expect(result).toContain("[diff truncated at 8000 chars]");
  });

  it("does not truncate diffs under 8000 chars", () => {
    const shortDiff = "a".repeat(100);
    const result = sanitizeDiff(shortDiff);
    expect(result).not.toContain("truncated");
    expect(result.length).toBe(100);
  });

  it("redacts <system> tags (CWE-20)", () => {
    const result = sanitizeDiff("before <system>content</system> after");
    expect(result).not.toContain("<system>");
  });

  it("redacts tokenizer-control tokens", () => {
    const result = sanitizeDiff("before <|endoftext|> after");
    expect(result).not.toContain("<|endoftext|>");
    expect(result).toContain("[REDACTED]");
  });

  it("leaves normal diff content intact", () => {
    const diff = `+++ b/src/foo.ts
-const x = 1;
+const x = 2;`;
    const result = sanitizeDiff(diff);
    expect(result).toContain("src/foo.ts");
    expect(result).toContain("const x = 2;");
  });

  it("redacts multiple occurrences of AGENT_OUTPUT", () => {
    const result = sanitizeDiff("AGENT_OUTPUT first AGENT_OUTPUT second");
    expect(result.split("[REDACTED-TOKEN]").length).toBe(3); // 2 replacements → 3 parts
  });
});

// ---------------------------------------------------------------------------
// parsePrStateEntry()
// ---------------------------------------------------------------------------

describe("parsePrStateEntry", () => {
  it("returns null for empty array", () => {
    expect(parsePrStateEntry("[]")).toBeNull();
  });

  it("returns null for invalid JSON", () => {
    expect(parsePrStateEntry("not-json")).toBeNull();
  });

  it("extracts phase from first entry", () => {
    const entry = parsePrStateEntry(
      JSON.stringify([{ phase: "merging", pr: 42, fix_cycle_count: 1, needs_security_review: true, debate_cycle_count: 0 }])
    );
    expect(entry).not.toBeNull();
    expect(entry!.phase).toBe("merging");
    expect(entry!.pr).toBe(42);
    expect(entry!.fix_cycle_count).toBe(1);
    expect(entry!.needs_security_review).toBe(true);
    expect(entry!.debate_cycle_count).toBe(0);
  });

  it("defaults missing fields to safe values", () => {
    const entry = parsePrStateEntry(JSON.stringify([{ phase: "code_review" }]));
    expect(entry).not.toBeNull();
    expect(entry!.pr).toBe(0);
    expect(entry!.fix_cycle_count).toBe(0);
    expect(entry!.needs_security_review).toBe(false);
    expect(entry!.debate_cycle_count).toBe(0);
  });

  it("ignores entries beyond the first (matches bash: entries[0])", () => {
    const entry = parsePrStateEntry(
      JSON.stringify([
        { phase: "merging", pr: 100 },
        { phase: "code_review", pr: 200 },
      ])
    );
    expect(entry!.phase).toBe("merging");
    expect(entry!.pr).toBe(100);
  });

  it("handles phase=unknown for missing phase key", () => {
    const entry = parsePrStateEntry(JSON.stringify([{}]));
    expect(entry!.phase).toBe("unknown");
  });

  it("coerces debate_cycle_count to number", () => {
    const entry = parsePrStateEntry(
      JSON.stringify([{ phase: "debate", debate_cycle_count: "3" }])
    );
    expect(entry!.debate_cycle_count).toBe(3);
  });
});

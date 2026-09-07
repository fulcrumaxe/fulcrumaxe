/**
 * dial-messages.ts — the operator-facing text the dial paths refuse with (D#1945).
 *
 * Why this file exists
 * --------------------
 * The refusal message below was a byte-identical literal in three places:
 * backend/dial_registry.py, src/spawn/dial-registry.ts and
 * src/rpc/mutating-p6b.ts. PR #1943 changed the wording and had to update all
 * three by hand; the reviewer confirmed they matched by reading them side by
 * side. Nothing asserted it.
 *
 * That matters more than a cosmetic mismatch because it is a security-surface
 * message: it tells an operator why a dial change was refused and what to do
 * instead. If the lanes drift, the same refusal hands out different remediation
 * through different backends, and the divergence is invisible until somebody
 * opens both files.
 *
 * This file removes the TypeScript-side duplication — one definition, two
 * importers. The remaining Python↔TypeScript pair cannot be collapsed without
 * generating one language from the other, so it is compared instead, by
 * scripts/ci/dial-refusal-message-parity-guard.py.
 *
 * Why here, and not somewhere else
 * --------------------------------
 * The two importers live in different subtrees (src/spawn/ and src/rpc/), so
 * one of them was always going to reach across. Putting the constant in
 * src/spawn/dial-registry.ts — the canonical dial implementation, and the
 * obvious first instinct — would have made src/rpc/mutating-p6b.ts import a
 * module that pulls in node:fs, node:crypto and the shared state-dir resolver
 * just to read one string. mutating-p6b.ts deliberately carries its own
 * standalone dial logic and imports none of that today.
 *
 * So the constant gets its own leaf module beside the canonical implementation:
 * it has no imports of its own, which is what lets both subtrees depend on it
 * without either one acquiring the other's runtime surface.
 *
 * This module is the TypeScript half of a pair. Renaming the export, moving
 * this file, or turning the initializer into anything other than a plain
 * concatenation of string literals will fail the guard rather than silently
 * stop being checked — see item 5 of the D#1945 spec.
 */

/**
 * The invariant half of the "source is not in the directive allowlist" refusal.
 *
 * Only the invariant half lives here. The `source ${...} is not in the
 * directive allowlist. ` prefix stays at each throw site because it
 * interpolates the rejected source, and Python spells that differently
 * (repr() vs JSON.stringify) — it is deliberately not part of what the
 * cross-language guard compares.
 *
 * Mirror of _SOURCE_NOT_ALLOWLISTED_REMEDY in backend/dial_registry.py.
 * The two are compared byte-for-byte on every CI run; do not reword one
 * without rewording the other.
 */
export const SOURCE_NOT_ALLOWLISTED_REMEDY =
  "A caller cannot authorize itself — ask an operator to run " +
  "`bash scripts/provision-dial-allowlist.sh`, or to add an entry to " +
  "<STATE_DIR>/dial-directive-allowlist.json by hand. Ceilings stay " +
  "enforced either way.";

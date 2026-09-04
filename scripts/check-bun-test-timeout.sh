#!/usr/bin/env bash
# scripts/check-bun-test-timeout.sh
#
# D#2276: bun's per-test default timeout is 5000ms, a separate knob from
# any subprocess-level timeout (e.g. spawnSync's own `timeout:` option).
# Nothing enforced the per-test default before this — the configured value
# lived only in ts-backend/package.json's "test" script, and two other
# tracked invocation sites (.github/workflows/ci.yml, scripts/run-pr-tests.sh)
# called `bun test tests/` directly, bypassing it silently.
#
# Rather than annotating every spawn-heavy it() block with its own timeout
# (rejected — a brace-matched scan found 1101 it() blocks across 50 files
# against CI's own "Ran 1159 tests", so ~58 tests are invisible to any
# static enumeration; a check premised on enumerating tests would report
# green over that blindness), this enforces one invariant with nothing to
# remember: every tracked `bun test` invocation of the ts-backend suite
# routes through the one script (`bun run test`) that sets the timeout.
#
# Deliberately NOT an allowlist-of-literals check like
# check-tests-fixed-tmp-paths.sh — there is nothing to allowlist. A bare
# `bun test tests/` invocation of the whole suite is never correct; the
# only correct invocation is `bun run test` (or `bun test tests/
# --timeout <N>` where N matches the configured value, but the design
# intent is a single source of truth, so this is flagged too).
#
# Scope: tracked `*.sh`, `*.yml`, `*.yaml` files (git ls-files) — the
# executable/CI surface. Markdown docs (ts-backend/README.md,
# wiki/TypeScript-Backend.md) are deliberately out of scope: they are not
# executed by anything, so a stale doc snippet is not the defect this
# guards against. Per-file debugging instructions in test-file header
# comments (`* Run: bun test tests/<file>.test.ts --timeout N`) are also
# out of scope by construction: they target a specific file or subdirectory
# path, never the bare `tests/` directory, so the boundary check below
# never matches them.
#
# Two files are excluded by path, not by allowlist entry, the same way
# check-tests-fixed-tmp-paths.sh excludes its own hermetic test harness:
#   - this script's own path (its comments and docstring necessarily
#     discuss and give examples of the exact pattern being detected)
#   - its test harness (tests/test_check_bun_test_timeout.sh), whose
#     fixtures write synthetic "bun test tests/" content as fixture data,
#     never executed by this suite itself.
#
# Unlike check-tests-fixed-tmp-paths.sh (soft-skips with a [WARN] when its
# script is missing), scripts/lib/preflight-common.sh hard-fails when this
# script is missing while ts-backend/package.json is present — see the
# check_bun_test_timeout() gate function for why.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "FAIL: not a git repository — this guard requires \`git ls-files\` to determine the tracked file set, and cannot run outside one" >&2
    exit 1
fi

PACKAGE_JSON="ts-backend/package.json"

if [ ! -f "$PACKAGE_JSON" ]; then
    echo "OK: $PACKAGE_JSON not present — nothing to govern (open-source export case is handled by the caller, not here)"
    exit 0
fi

python3 - "$PACKAGE_JSON" <<'PYEOF'
import json
import re
import subprocess
import sys

package_json_path = sys.argv[1]

# ---------------------------------------------------------------------------
# Read the one configured timeout value from ts-backend/package.json's
# "test" script. This is the single source of truth the invariant protects.
# ---------------------------------------------------------------------------

with open(package_json_path, encoding="utf-8") as f:
    pkg = json.load(f)

test_script = pkg.get("scripts", {}).get("test", "")
m = re.search(r"--timeout[= ]+(\d+)", test_script)
if not m:
    print(
        f"FAIL: {package_json_path}'s \"test\" script ({test_script!r}) has no "
        f"--timeout — nothing defines the configured default",
        file=sys.stderr,
    )
    sys.exit(1)

configured_timeout = m.group(1)

# ---------------------------------------------------------------------------
# Scan tracked *.sh, *.yml, *.yaml files for a bare whole-suite invocation
# (`bun test tests/`, not `bun run test`) that bypasses the configured
# script. Boundary-checked so a subpath invocation (`tests/spawn/`,
# `tests/foo.test.ts`) — a legitimate single-file/subdir debugging
# instruction — is never flagged.
# ---------------------------------------------------------------------------

_SELF_EXCLUDE = {
    "scripts/check-bun-test-timeout.sh",
    "tests/test_check_bun_test_timeout.sh",
}

out = subprocess.run(
    ["git", "ls-files", "*.sh", "*.yml", "*.yaml"],
    capture_output=True,
    text=True,
    check=False,
)
files = sorted(
    l for l in out.stdout.splitlines() if l and l not in _SELF_EXCLUDE
)

# `bun test tests/` immediately followed by whitespace, a quote, a shell
# metacharacter, or end-of-line — never by further path characters (which
# would make it a scoped subpath invocation, out of scope by design).
BARE_INVOCATION_RE = re.compile(r"\bbun test tests/(?=[\s\"'|;>]|$)")

violations = []
for path in files:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        continue
    for lineno, line in enumerate(lines, start=1):
        if BARE_INVOCATION_RE.search(line):
            violations.append((path, lineno, line.strip()))

if violations:
    for path, lineno, text in violations:
        print(
            f"FAIL: {path}:{lineno} invokes bare 'bun test tests/' directly "
            f"— must route through 'bun run test' so {package_json_path}'s "
            f"configured --timeout {configured_timeout} governs it: {text!r}",
            file=sys.stderr,
        )
    sys.exit(1)

print(
    f"OK: no tracked *.sh/*.yml/*.yaml file bypasses the configured "
    f"--timeout {configured_timeout} ({len(files)} files scanned, 0 bare "
    f"'bun test tests/' invocations)"
)
PYEOF
exit $?

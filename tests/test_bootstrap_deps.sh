#!/usr/bin/env bash
# tests/test_bootstrap_deps.sh - scripts/bootstrap-deps.sh treats ts-backend/
# as optional: without bun it is skipped (one line, exit 0); with bun it is
# installed as before; --dry-run shows the skip; another component failing
# still fails the run. npm and bun are logging stubs; no network.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PASS=0; FAIL=0
ok()  { echo "  [OK] $1";   PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

T="$(mktemp -d "${TMPDIR:-/tmp}/bootstrap-deps.XXXXXX")"
trap 'rm -rf "$T"' EXIT

SKIP_LINE='[bootstrap-deps] ts-backend/: skipped (TypeScript backend; bun not installed)'

mkdir -p "$T/nobun" "$T/withbun"
for tool in bash env dirname ls cat mkdir rm date basename; do
  p="$(command -v "$tool")" && { ln -s "$p" "$T/nobun/$tool"; ln -s "$p" "$T/withbun/$tool"; }
done
mk_stub() { # dir name rc
  cat > "$1/$2" <<STUB
#!/usr/bin/env bash
echo "$2 \$* (cwd=\$(basename "\$PWD"))" >> "$T/calls.log"
exit ${3:-0}
STUB
  chmod +x "$1/$2"
}
mk_stub "$T/nobun" npm 0
mk_stub "$T/withbun" npm 0
mk_stub "$T/withbun" bun 0

new_repo() {
  R="$T/repo"; rm -rf "$R"; mkdir -p "$R/scripts" "$R/dashboard" "$R/ts-backend"
  cp "$REPO_ROOT/scripts/bootstrap-deps.sh" "$R/scripts/"
  echo '{}' > "$R/dashboard/package.json"
  echo '{}' > "$R/ts-backend/package.json"
  : > "$R/ts-backend/bun.lock"
  : > "$T/calls.log"
}
run() { # pathdir args...
  local d="$1"; shift
  OUT="$(PATH="$d" bash "$R/scripts/bootstrap-deps.sh" "$@" 2>&1)"; RC=$?
}

echo "bun absent"
new_repo; run "$T/nobun"
[[ $RC -eq 0 ]] && ok "exit 0" || bad "exit $RC: $OUT"
[[ "$(grep -cxF "$SKIP_LINE" <<<"$OUT")" -eq 1 ]] && ok "exactly one skip line" || bad "skip line: $OUT"
grep -q 'ERROR' <<<"$OUT" && bad "unexpected ERROR" || ok "no error"
grep -q '^npm .*(cwd=dashboard)' "$T/calls.log" && ok "dashboard still installed" || bad "dashboard not installed"
grep -q 'ts-backend' "$T/calls.log" && bad "ts-backend was installed" || ok "ts-backend not installed"

echo "bun present"
new_repo; run "$T/withbun"
[[ $RC -eq 0 ]] && ok "exit 0" || bad "exit $RC: $OUT"
grep -q '^bun install (cwd=ts-backend)' "$T/calls.log" && ok "bun install called" || bad "bun install not called"
grep -qF "$SKIP_LINE" <<<"$OUT" && bad "skip line printed" || ok "no skip line"

echo "dry run, bun absent"
new_repo; run "$T/nobun" --dry-run
[[ $RC -eq 0 ]] && ok "exit 0" || bad "exit $RC"
grep -qxF "$SKIP_LINE" <<<"$OUT" && ok "skip line shown" || bad "skip line missing: $OUT"
[[ ! -s "$T/calls.log" ]] && ok "nothing installed" || bad "dry run ran a command"

echo "another component failing still fails"
new_repo; mk_stub "$T/nobun" npm 1; run "$T/nobun"
[[ $RC -ne 0 ]] && ok "non-zero exit" || bad "exit 0 despite npm failure"
grep -qF 'ERROR' <<<"$OUT" && ok "error reported" || bad "no error line"
grep -qxF "$SKIP_LINE" <<<"$OUT" && ok "skip still printed" || bad "skip line missing"
mk_stub "$T/nobun" npm 0

echo "bun present but failing still fails"
new_repo; mk_stub "$T/withbun" bun 1; run "$T/withbun"
[[ $RC -ne 0 ]] && ok "non-zero exit" || bad "exit 0 despite bun failure"

echo ""
echo "passed: $PASS failed: $FAIL"
[[ $FAIL -eq 0 ]]

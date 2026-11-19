#!/usr/bin/env bash
# tests/test_harness_answers_file.sh — `harness.sh --record-topic --answers-file`.
#
# Checks the file route records the same answers as --answers, refuses unsafe
# files, never reads stdin, and (mutation) that each refusal can actually fail.
# No network, no gh. State lives in a fresh temp dir.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/harness-af.XXXXXX")"
trap 'rm -rf "$T"' EXIT
FAILS=0
N=0

H() { # H HARNESS STATE ARGS...   (stdin closed)
  local h="$1" st="$2"; shift 2
  AUTONOMOUS_TEAM_STATE_DIR="$st" GH_TOKEN=invalid GITHUB_TOKEN=invalid GH_HOST=invalid.localhost \
    bash "$h" "$@" </dev/null
}
answers_of() { python3 -c "import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True))" "$1"; }

# run_checks HARNESS — prints the name of each failing check; returns 1 if any.
run_checks() {
  local h="$1" bad=0 st f
  N=$((N + 1))
  st="$T/st-$N"; f="$T/ans-$N.json"
  mkdir -p "$st"
  printf '{"github_login":"someone"}' > "$f"

  # 1. the file route records exactly what --answers does
  H "$h" "$st" --record-topic identity --answers-file "$f" --session a >/dev/null 2>&1 || { echo "file-route-fails"; bad=1; }
  H "$h" "$st" --record-topic identity --answers "$(cat "$f")" --session b >/dev/null 2>&1
  if [[ ! -s "$st/coldstart/a/answers.json" ]] || \
     [[ "$(answers_of "$st/coldstart/a/answers.json" 2>/dev/null | sed 's/"[0-9.]*"//')" != "$(answers_of "$st/coldstart/b/answers.json" 2>/dev/null | sed 's/"[0-9.]*"//')" ]]; then
    echo "file-route-differs"; bad=1
  fi
  # 2. a symlink is refused
  ln -sf "$f" "$T/link-$N.json"
  H "$h" "$st" --record-topic identity --answers-file "$T/link-$N.json" --session c >/dev/null 2>&1 && { echo "symlink-accepted"; bad=1; }
  # 3. a non-object and invalid JSON are refused
  echo '[1]' > "$T/arr-$N.json"
  H "$h" "$st" --record-topic identity --answers-file "$T/arr-$N.json" --session d >/dev/null 2>&1 && { echo "array-accepted"; bad=1; }
  echo 'nope' > "$T/bad-$N.json"
  H "$h" "$st" --record-topic identity --answers-file "$T/bad-$N.json" --session d >/dev/null 2>&1 && { echo "invalid-json-accepted"; bad=1; }
  # 4. an oversize file is refused
  python3 -c "import json;print(json.dumps({'a':'x'*70000}))" > "$T/big-$N.json"
  H "$h" "$st" --record-topic identity --answers-file "$T/big-$N.json" --session e >/dev/null 2>&1 && { echo "oversize-accepted"; bad=1; }
  # 5. a directory and a missing path are refused
  H "$h" "$st" --record-topic identity --answers-file "$T" --session f >/dev/null 2>&1 && { echo "directory-accepted"; bad=1; }
  H "$h" "$st" --record-topic identity --answers-file "$T/none.json" --session f >/dev/null 2>&1 && { echo "missing-accepted"; bad=1; }
  # 6. both flags together are refused
  H "$h" "$st" --record-topic identity --answers '{}' --answers-file "$f" --session g >/dev/null 2>&1 && { echo "both-accepted"; bad=1; }
  # 7. a refused file records nothing
  if [[ -e "$st/coldstart/d/answers.json" ]] && grep -q 'identity' "$st/coldstart/d/answers.json" 2>/dev/null; then
    echo "refused-wrote"; bad=1
  fi
  return $bad
}

check() { # check NAME HARNESS EXPECT(pass|fail)
  local out rc
  out="$(run_checks "$2")"; rc=$?
  if [[ "$3" == pass && $rc -ne 0 ]]; then echo "FAIL $1: $out"; FAILS=$((FAILS + 1))
  elif [[ "$3" == fail && $rc -eq 0 ]]; then echo "FAIL $1: mutant not caught"; FAILS=$((FAILS + 1))
  else echo "ok   $1"; fi
}

SRC="$REPO_ROOT/scripts/coldstart-interview"
check "real harness passes every check" "$SRC/harness.sh" pass

mutant() { # mutant NAME SED-EXPR
  local d="$T/m-$1"
  mkdir -p "$d"
  cp -r "$SRC/." "$d/"
  sed -i "$2" "$d/harness.sh"
  if cmp -s "$d/harness.sh" "$SRC/harness.sh"; then
    echo "FAIL mutant $1 did not change the file"; FAILS=$((FAILS + 1)); return
  fi
  check "mutant $1 is caught" "$d/harness.sh" fail
}
mutant nofollow 's/os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK/os.O_RDONLY | os.O_NONBLOCK/'
mutant size 's/st.st_size > 65536/st.st_size > 10**9/;s/os.read(fd, 65537)/os.read(fd, 10**9)/'
mutant object 's/if not isinstance(obj, dict):/if False:/'
mutant both 's/if \[\[ -n "\$ANSWERS_JSON" \]\]; then/if false; then/'

if [[ $FAILS -eq 0 ]]; then echo "PASS test_harness_answers_file"; else echo "$FAILS failure(s)"; exit 1; fi

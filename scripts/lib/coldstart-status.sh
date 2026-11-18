#!/usr/bin/env bash
# scripts/lib/coldstart-status.sh — the machine-readable progress stream of
# `coldstart.sh --non-interactive --status-fd N`.
#
# Every line is exactly `step=<step> state=<state>` or
# `step=<step> state=<state> code=<code>`. The closed vocabularies live in
# scripts/coldstart-status-schema.json and nowhere else: an emit that names
# anything outside them is refused (return 2) rather than written, so a
# consumer pinning the schema hash never sees a value it was not told about.
# `code=missing_topic:<id>` carries the topic id after the colon.
#
#   coldstart_status_hash          print the sha256 of the schema file
#   coldstart_status_init FD       open the stream (FD must be an open descriptor)
#   coldstart_status_emit STEP STATE [CODE]

_CS_STATUS_SCHEMA="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/coldstart-status-schema.json"
_CS_STATUS_FD=""

coldstart_status_hash() {
  python3 -I - "$_CS_STATUS_SCHEMA" <<'PYEOF'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
PYEOF
}

coldstart_status_init() {
  local fd="${1:-}"
  if [[ ! "$fd" =~ ^[0-9]+$ ]] || (( fd < 3 )); then
    echo "ERROR: --status-fd must be a descriptor number of 3 or more (got: ${fd:-<empty>})" >&2
    return 1
  fi
  if ! { : >&"$fd"; } 2>/dev/null; then
    echo "ERROR: --status-fd $fd is not an open, writable descriptor" >&2
    return 1
  fi
  _CS_STATUS_FD="$fd"
}

coldstart_status_emit() {
  local step="$1" state="$2" code="${3:-}" line
  line="$(python3 -I - "$_CS_STATUS_SCHEMA" "$step" "$state" "$code" <<'PYEOF'
import json, re, sys
schema = json.load(open(sys.argv[1]))
step, state, code = sys.argv[2:5]
if step not in schema["steps"] or state not in schema["states"]:
    sys.exit(2)
out = f"step={step} state={state}"
if code:
    base, _, suffix = code.partition(":")
    if base not in schema["codes"]:
        sys.exit(2)
    pat = schema.get("code_suffix", {}).get(base)
    if pat is None and suffix:
        sys.exit(2)
    if pat is not None and not re.fullmatch(pat, suffix):
        sys.exit(2)
    out += f" code={code}"
print(out)
PYEOF
  )" || { echo "ERROR: coldstart status line refused (step=$step state=$state code=$code)" >&2; return 2; }
  [[ -n "$_CS_STATUS_FD" ]] || return 0
  printf '%s\n' "$line" >&"$_CS_STATUS_FD"
}

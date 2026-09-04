---
name: Never dump auth headers — verbose/explain/trace flags leak secrets
description: curl -v, vastai --explain/--curl, gh api --verbose, and set -x all print Authorization headers in plaintext. Two live leaks of the user's API key in session 85514482.
type: feedback
originSessionId: 85514482-6eda-41bb-baf3-45fb37863d1a
tier: transferable
---
In session 85514482 the user's API key leaked into chat TWICE within 15 minutes:
1. `curl -v -X GET "https://api.example.com/v1/endpoints/" -H "Authorization: Bearer $(cat ~/api-key.txt)"` — printed the full Bearer token in the request headers section
2. `vastai create endpoint ... --explain` — printed the full Bearer token in the "Prepared Request" headers dump

Both required immediate user-side rotation of the key.

**Why:** debug/verbose/trace flags exist to show the full request including auth. They're meant for safe environments. In a session where stdout is logged to chat, they're a foot-gun.

**How to apply:**

NEVER use any of these flags on a request that includes auth headers OR on a script that touches auth:

| Forbidden | Safe alternative |
|---|---|
| `curl -v` | `curl -i` (response headers only, no request dump) |
| `curl --trace`, `--trace-ascii`, `--trace-time` | none — there's no safe trace |
| `vastai --explain` | run with `--raw` for JSON output, parse the response |
| `vastai --curl` | same — never use this flag |
| `gh api --verbose` | plain `gh api` |
| `set -x` in a bash script that uses Authorization headers or secret env vars | leave shell trace off; use targeted `echo` statements that DON'T include the secrets |

**If verbose debugging is unavoidable**, pipe through a scrubber:
```bash
curl -v ... 2>&1 | sed -E 's/(authorization|Authorization|Bearer)[^"]*/\1 [REDACTED]/g'
```

But prefer to NOT use verbose flags at all. Almost every debugging task can be done with response inspection (`curl -i`, `curl -w '%{http_code}'`, response body parsing) which doesn't expose the request.

**What to do when a leak happens:**
1. STOP immediately. Do not wait to "wrap up" the current task.
2. Tell the user explicitly: "I leaked your <which> key in <which> output above."
3. Recommend rotation steps.
4. Wait for confirmation before continuing.

Both leaks in session 85514482 were caught and the keys were rotated. This memory exists so future sessions don't repeat them.

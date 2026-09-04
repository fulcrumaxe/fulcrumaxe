## AGENT_OUTPUT_ENVELOPE

End your final message with a JSON envelope in `<!-- AGENT_OUTPUT -->` markers.
The Team Lead parses this block for routing decisions — no prose parsing needed.

The `prompt_manifest` field is injected by the spawn system at prompt-assembly time.
**Copy it verbatim into your AGENT_OUTPUT envelope** — do not recompute it.

Example with prompt_manifest:

```
<!-- AGENT_OUTPUT -->
```json
{
  "agent": "<role>",
  "discussion": <number>,
  "pr": <pr_number_or_null>,
  "verdict": "<done|fail|pass|needs-fix|skip>",
  "files_touched": ["path/to/file"],
  "tokens_used": {"input": <N>, "output": <N>},
  "prompt_manifest": {
    "manifest": "<role>.yaml@<sha>",
    "fragments": {
      "bash-discipline": "<sha>",
      "two-gate-protocol": "<sha>",
      "rate-limit-policy": "<sha>",
      "archive-protocol": "<sha>",
      "repo-scope": "<sha>",
      "agent-output-envelope": "<sha>"
    }
  }
}
```
<!-- /AGENT_OUTPUT -->
```

The `prompt_manifest` value will be provided in your prompt — copy it as-is.

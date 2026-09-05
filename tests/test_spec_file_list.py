"""Tests for backend/spec_file_list.py — Spec file-list extraction.

Covers:
  - acceptance_files inline YAML array in frontmatter
  - acceptance_files block YAML list in frontmatter
  - Code-block file headers (```ts path/to/file.ts)
  - Multiple languages recognised
  - Empty body / no markers → empty list
  - Priority: frontmatter wins over code-block headers
  - Smoke test matching the Discussion #965 acceptance criteria
  - CLI forces a fresh cache read (D#1778 Blocking Issue 1) — the scope-drift
    signal must not be computed against a stale cached Spec body
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.spec_file_list import extract_file_list, _parse_frontmatter, _parse_code_blocks, _cli


# ---------------------------------------------------------------------------
# Strategy 1 — acceptance_files in YAML frontmatter (inline array)
# ---------------------------------------------------------------------------

class TestFrontmatterInlineArray:
    def test_basic_inline_array(self):
        body = """---
estimated_hours: 2
acceptance_files: ["a.py", "b.py"]
---

Some spec text.
"""
        assert extract_file_list(body) == ["a.py", "b.py"]

    def test_single_item_inline(self):
        body = """---
acceptance_files: ["scripts/post-agent-hook.sh"]
---
"""
        assert extract_file_list(body) == ["scripts/post-agent-hook.sh"]

    def test_inline_without_quotes(self):
        body = """---
acceptance_files: [a.py, b.py, c.py]
---
"""
        result = extract_file_list(body)
        assert "a.py" in result
        assert "b.py" in result
        assert "c.py" in result

    def test_inline_single_quotes(self):
        body = """---
acceptance_files: ['src/foo.ts', 'src/bar.ts']
---
"""
        result = extract_file_list(body)
        assert "src/foo.ts" in result
        assert "src/bar.ts" in result


# ---------------------------------------------------------------------------
# Strategy 1 — acceptance_files in YAML frontmatter (block list)
# ---------------------------------------------------------------------------

class TestFrontmatterBlockList:
    def test_block_list(self):
        body = """---
estimated_hours: 3
acceptance_files:
  - src/foo.py
  - src/bar.py
---
"""
        result = extract_file_list(body)
        assert result == ["src/foo.py", "src/bar.py"]

    def test_block_list_with_quotes(self):
        body = """---
acceptance_files:
  - "scripts/post-agent-hook.sh"
  - "backend/spec_file_list.py"
---
"""
        result = extract_file_list(body)
        assert "scripts/post-agent-hook.sh" in result
        assert "backend/spec_file_list.py" in result


# ---------------------------------------------------------------------------
# Strategy 2 — code-block file headers
# ---------------------------------------------------------------------------

class TestCodeBlockHeaders:
    def test_python_header(self):
        body = """Some text.

```py src/foo.py
def hello(): pass
```
"""
        assert extract_file_list(body) == ["src/foo.py"]

    def test_typescript_header(self):
        body = """```ts src/App.tsx
const x = 1;
```
"""
        assert extract_file_list(body) == ["src/App.tsx"]

    def test_bash_header(self):
        body = """```bash scripts/my-hook.sh
#!/bin/bash
```
"""
        assert extract_file_list(body) == ["scripts/my-hook.sh"]

    def test_multiple_code_blocks(self):
        body = """
```ts frontend/app.ts
// code
```

Some text.

```py backend/server.py
# code
```

```sh scripts/deploy.sh
#!/bin/sh
```
"""
        result = extract_file_list(body)
        assert "frontend/app.ts" in result
        assert "backend/server.py" in result
        assert "scripts/deploy.sh" in result

    def test_rust_header(self):
        body = """```rs src/lib.rs
fn main() {}
```
"""
        assert extract_file_list(body) == ["src/lib.rs"]

    def test_code_block_without_path_ignored(self):
        body = """```python
def hello(): pass
```
"""
        # No path after the language tag → should not extract
        result = extract_file_list(body)
        assert result == []


# ---------------------------------------------------------------------------
# Priority: frontmatter wins over code-block headers
# ---------------------------------------------------------------------------

class TestPriority:
    def test_frontmatter_takes_priority(self):
        body = """---
acceptance_files: ["a.py", "b.py"]
---

```ts src/app.ts
// some code
```
"""
        # Should return frontmatter list, NOT code-block paths
        result = extract_file_list(body)
        assert result == ["a.py", "b.py"]
        assert "src/app.ts" not in result


# ---------------------------------------------------------------------------
# Edge cases — empty / no markers
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_body(self):
        assert extract_file_list("") == []

    def test_no_markers(self):
        body = "Just some plain text with no file declarations."
        assert extract_file_list(body) == []

    def test_frontmatter_without_acceptance_files(self):
        body = """---
estimated_hours: 2
complexity_points: 3
---

No acceptance_files key here.
"""
        assert extract_file_list(body) == []

    def test_empty_inline_array(self):
        body = """---
acceptance_files: []
---
"""
        assert extract_file_list(body) == []


# ---------------------------------------------------------------------------
# Smoke test — matches D#965 acceptance criteria
# ---------------------------------------------------------------------------

class TestSmokeD965:
    """Simulates: Spec has acceptance_files: [a.py, b.py].
    PR diff includes a.py, b.py, c.py → c.py is out-of-scope.
    """

    def test_acceptance_criteria_smoke(self):
        spec_body = """---
estimated_hours: 1
acceptance_files: ["a.py", "b.py"]
---

Some spec text.
"""
        declared = set(extract_file_list(spec_body))
        pr_files = {"a.py", "b.py", "c.py"}
        drift = pr_files - declared
        assert drift == {"c.py"}, f"Expected drift={{c.py}}, got {drift}"

    def test_no_drift_when_all_declared(self):
        spec_body = """---
acceptance_files: ["a.py", "b.py", "c.py"]
---
"""
        declared = set(extract_file_list(spec_body))
        pr_files = {"a.py", "b.py", "c.py"}
        drift = pr_files - declared
        assert drift == set()

    def test_code_block_smoke(self):
        """Fallback to code-block headers when no acceptance_files frontmatter."""
        spec_body = """## Spec

Change these files:

```py a.py
# code
```

```py b.py
# code
```
"""
        declared = set(extract_file_list(spec_body))
        pr_files = {"a.py", "b.py", "c.py"}
        drift = pr_files - declared
        assert drift == {"c.py"}


# ---------------------------------------------------------------------------
# CLI must force a fresh read (D#1778 Blocking Issue 1)
# ---------------------------------------------------------------------------

class TestCLIFreshRead:
    """scope-drift-check.sh calls this CLI once per finished executor PR to get
    the declared file list. If the underlying read serves a stale cache row,
    the scope-drift comment is computed against the previous revision of the
    Spec — wrong, and silently so. The CLI must request fresh=True.

    Uses two distinct, realistic bodies (not the same string reused) so this
    cannot pass by coincidence — a stale/fresh mix-up would surface as the
    wrong file appearing in stdout, not just a call-count off-by-one.
    """

    def test_cli_requests_fresh_read_over_stale(self, monkeypatch, capsys):
        calls = []

        def _fake_get_body(number, fresh=False):
            calls.append((number, fresh))
            if fresh:
                # What a live GraphQL fetch would return after a PM just
                # edited the Spec.
                return '---\nacceptance_files: ["backend/live_edit.py"]\n---\n'
            # What a 300s-TTL cache row from before the edit would still hold.
            return '---\nacceptance_files: ["backend/stale_cached.py"]\n---\n'

        monkeypatch.setattr(
            "backend.discussion_cache.get_body", _fake_get_body
        )

        _cli("4242")

        out = capsys.readouterr().out
        assert calls == [(4242, True)], (
            f"expected a single fresh=True call, got {calls}"
        )
        assert "backend/live_edit.py" in out
        assert "backend/stale_cached.py" not in out

    def test_cli_empty_body_skips_check(self, monkeypatch, capsys):
        """get_body returning "" (fetch failed, nothing cached) → CLI exits 0
        with no output, same as before this change."""
        monkeypatch.setattr(
            "backend.discussion_cache.get_body", lambda number, fresh=False: ""
        )

        with pytest.raises(SystemExit) as exc_info:
            _cli("9999")

        assert exc_info.value.code == 0
        assert capsys.readouterr().out == ""

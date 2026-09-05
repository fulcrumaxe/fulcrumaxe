"""
tests/test_sweep_premature_closes.py — unit tests for
scripts/sweep-premature-closes.py (D#2021).

Covers Spec (Acceptance) items 10-11:
  10. Sweep finds the five known cases — reproduced here against a log
      FIXTURE (not live host state; the real dashboard-logs directory is
      untracked runtime state that does not exist inside an isolated
      executor worktree, so this test builds the equivalent fixture
      directly instead of depending on it).
  11. Sweep mutates nothing, and --repair/--reopen are not accepted.

No real GitHub calls: the body-based (secondary) detector is exercised via
dependency injection (`fetch_closed_fn`), never the live network path.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "sweep-premature-closes.py"

_spec = importlib.util.spec_from_file_location("sweep_premature_closes", SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

find_log_files = _mod.find_log_files
scan_logs_for_multi_close = _mod.scan_logs_for_multi_close
has_duplicate_done_marker = _mod.has_duplicate_done_marker
has_open_done_marker = _mod.has_open_done_marker
run_sweep = _mod.run_sweep
resolve_log_root = _mod.resolve_log_root

# The five Discussions D#2021 identified as closed prematurely at least once,
# measured from real manual-merge logs on the reporting host.
KNOWN_PREMATURE_CLOSES = [1997, 1967, 1788, 1765, 1632]


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


class TestScanLogsForMultiClose:
    def test_finds_all_five_known_cases(self, tmp_path):
        # Reproduce the real fixture the Spec measured: each of the five
        # known Discussions logs "Closing Discussion #N" twice across the
        # manual-merge logs (one log file per PR merge, as the real hook
        # writes them).
        log_dir = tmp_path / ".autonomous-team" / "dashboard-logs"
        log_dir.mkdir(parents=True)

        pr = 1000
        for disc in KNOWN_PREMATURE_CLOSES:
            for _ in range(2):
                _write_log(
                    log_dir / f"manual-merge-{pr}.log",
                    [f"[post-merge-hook] Closing Discussion #{disc}"],
                )
                pr += 1

        # A correctly-closed Discussion (closed exactly once) must NOT be flagged.
        _write_log(log_dir / f"manual-merge-{pr}.log", ["[post-merge-hook] Closing Discussion #9999"])

        log_files = find_log_files(repo_root=tmp_path)
        counts = scan_logs_for_multi_close(log_files)

        for disc in KNOWN_PREMATURE_CLOSES:
            assert counts.get(disc, 0) >= 2, f"D#{disc} should show >=2 closes, got {counts.get(disc, 0)}"

        assert counts.get(9999, 0) == 1, "a correctly-closed Discussion must show exactly 1, never flagged"

    def test_no_log_files_returns_empty(self, tmp_path):
        log_files = find_log_files(repo_root=tmp_path)
        assert log_files == []
        assert scan_logs_for_multi_close(log_files) == {}


class TestHasDuplicateDoneMarker:
    def test_single_marker_is_clean(self):
        body = "<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->\n\nSome text."
        assert has_duplicate_done_marker(body) is False

    def test_two_real_markers_is_flagged(self):
        body = (
            "<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->\n\n"
            "prose\n\n"
            "<!-- STATUS:DONE SINCE:2026-01-02T00:00:00Z -->\n"
        )
        assert has_duplicate_done_marker(body) is True

    def test_marker_inside_fence_is_flagged(self):
        body = (
            "<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->\n\n"
            "```\n<!-- STATUS:DONE -->\n```\n"
        )
        assert has_duplicate_done_marker(body) is True

    def test_negative_case_prose_mention_is_not_flagged(self):
        # The exact false-positive trap this function must NOT fall into:
        # a Discussion that talks ABOUT the DONE marker convention in plain
        # prose (no HTML-comment wrapper) must not be flagged. This repo has
        # several real Discussions like this (D#1798, D#2021 itself).
        body = (
            "<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->\n\n"
            "This Discussion discusses how the writer rewrites every "
            "occurrence of STATUS:DONE anywhere in the body, including a "
            "stray STATUS:DONE mention here and STATUS:DONE again there."
        )
        assert has_duplicate_done_marker(body) is False

    def test_negative_case_documentation_example_in_fence_with_only_one_real_marker(self):
        # A body that shows the bare (non-comment) prefix in a fenced
        # example — not a real marker — must not be flagged.
        body = (
            "<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->\n\n"
            "```\nSTATUS:PENDING\nSTATUS:BLOCKED\n```\n"
        )
        assert has_duplicate_done_marker(body) is False

    def test_empty_body_is_not_flagged(self):
        assert has_duplicate_done_marker("") is False

    def test_respects_done_value_parameter(self):
        body = "<!-- STATUS:CLOSED -->\n<!-- STATUS:CLOSED -->\n"
        assert has_duplicate_done_marker(body, done_value="CLOSED") is True
        assert has_duplicate_done_marker(body, done_value="DONE") is False


class TestHasOpenDoneMarker:
    """Detector 3 (D#2020): an OPEN Discussion with a stale first-line DONE
    marker — the shape neither of the other two detectors can see. D#1908
    is the real-world fixture: it was open, carried exactly one clean
    ``<!-- STATUS:DONE SINCE:2026-08-18T19:29:09Z -->`` marker on line 1
    (quoted from the PM's dated measurement comment on D#2020, since a PM
    hand-corrected the live marker to DISCUSSING the same day this Spec was
    written — the present-day body no longer shows it), and the spawn gate
    still refused to dispatch PR 2/3 of it: "Spawn blocked: Discussion #1908
    status is DONE — work is already complete."
    """

    def test_single_clean_marker_on_open_discussion_is_flagged(self):
        # D#1908's real shape: open, one well-formed marker, line 1.
        body = (
            "<!-- STATUS:DONE SINCE:2026-08-18T19:29:09Z -->\n\n"
            "> **Spec is FROZEN.** PR 1 of 3 is specified in this comment.\n"
        )
        assert has_open_done_marker(body) is True

    def test_non_done_marker_is_not_flagged(self):
        body = "<!-- STATUS:SPEC_READY SINCE:2026-08-22T00:33:51Z -->\n\nsome prose\n"
        assert has_open_done_marker(body) is False

    def test_marker_not_on_first_line_is_not_flagged(self):
        body = "Some preamble line.\n<!-- STATUS:DONE SINCE:2026-08-18T19:29:09Z -->\n"
        assert has_open_done_marker(body) is False

    def test_trailing_prose_after_marker_on_same_line_is_not_flagged(self):
        # Not a real marker shape — guards against over-matching a line that
        # merely starts with the marker syntax.
        body = "<!-- STATUS:DONE --> and then some extra text\n"
        assert has_open_done_marker(body) is False

    def test_empty_body_is_not_flagged(self):
        assert has_open_done_marker("") is False

    def test_respects_done_value_parameter(self):
        body = "<!-- STATUS:CLOSED -->\n"
        assert has_open_done_marker(body, done_value="CLOSED") is True
        assert has_open_done_marker(body, done_value="DONE") is False


class TestRunSweep:
    def test_combines_log_and_body_detectors(self, tmp_path):
        log_dir = tmp_path / ".autonomous-team" / "dashboard-logs"
        log_dir.mkdir(parents=True)
        _write_log(log_dir / "manual-merge-1.log", ["[post-merge-hook] Closing Discussion #100"])
        _write_log(log_dir / "manual-merge-2.log", ["[post-merge-hook] Closing Discussion #100"])
        _write_log(log_dir / "manual-merge-3.log", ["[post-merge-hook] Closing Discussion #200"])
        _write_log(log_dir / "manual-merge-4.log", ["[post-merge-hook] Closing Discussion #200"])

        def fake_fetch():
            return {
                200: {"body": "<!-- STATUS:DONE -->\n<!-- STATUS:DONE -->\n", "closed": True},  # corrupted, closed
                300: {"body": "<!-- STATUS:DONE -->\n\nordinary body", "closed": True},  # clean, closed
            }

        report = run_sweep(repo_root=tmp_path, fetch_closed_fn=fake_fetch)

        assert report["files_read"] == 4
        assert report["sources_scanned"], "sources_scanned must never be empty"

        flagged_numbers = {d["discussion"] for d in report["discussions"]}
        assert 100 in flagged_numbers  # log-based only
        assert 200 in flagged_numbers  # both detectors
        assert 300 not in flagged_numbers  # clean, must not appear

        by_number = {d["discussion"]: d for d in report["discussions"]}
        assert "log_multi_close" in by_number[100]["detectors"]
        assert "duplicate_done_marker" not in by_number[100]["detectors"]
        assert "log_multi_close" in by_number[200]["detectors"]
        assert "duplicate_done_marker" in by_number[200]["detectors"]

    def test_open_discussion_with_stale_done_marker_is_flagged(self, tmp_path):
        # Spec (Acceptance) item 9 — the discriminating test for D#2020.
        # No manual-merge logs at all: D#1908 was never closed, so the
        # log-based detector has nothing to count. #1908's real shape,
        # reconstructed from the PM's dated measurement comment on D#2020
        # (today's live body was hand-corrected to DISCUSSING, so this is
        # history, not the present value): open, one clean first-line DONE
        # marker, `planned_prs` referenced only in a comment, PR 3 unmerged.
        def fake_fetch():
            return {
                1908: {
                    "body": (
                        "<!-- STATUS:DONE SINCE:2026-08-18T19:29:09Z -->\n\n"
                        "> **Spec is FROZEN.** PR 1 of 3 is specified in this comment.\n"
                    ),
                    "closed": False,
                },
                # Negative case, item 10 part 1: OPEN, but SPEC_READY not DONE.
                2020: {
                    "body": "<!-- STATUS:SPEC_READY SINCE:2026-08-22T00:33:51Z -->\n\nprose\n",
                    "closed": False,
                },
                # Negative case, item 10 part 2: CLOSED with a single clean
                # DONE marker — the correct, non-corrupted closed shape.
                # Not duplicated, so duplicate_done_marker must not fire
                # either, and open_with_done_marker must not fire because
                # it is closed.
                1997: {
                    "body": "<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->\n\nordinary body\n",
                    "closed": True,
                },
            }

        report = run_sweep(repo_root=tmp_path, fetch_closed_fn=fake_fetch)

        by_number = {d["discussion"]: d for d in report["discussions"]}
        assert 1908 in by_number, "an OPEN Discussion with a stale DONE marker must be flagged"
        assert by_number[1908]["detectors"] == ["open_with_done_marker"]

        assert 2020 not in by_number, "OPEN + non-DONE marker must not be flagged"
        assert 1997 not in by_number, "CLOSED + single clean DONE marker must not be flagged"

    def test_sources_scanned_never_empty_even_with_no_files(self, tmp_path):
        report = run_sweep(repo_root=tmp_path, fetch_closed_fn=lambda: {})
        assert report["files_read"] == 0
        assert report["sources_scanned"] != []
        assert report["discussions"] == []

    def test_zero_files_read_carries_a_warning_naming_the_root(self, tmp_path, capsys):
        # The exact "absence of evidence read as evidence of absence" bug
        # this whole Spec is about, one layer up (Team Lead review, D#2021
        # fix round): a run that reads zero log files must not look like a
        # clean report. It must say, loudly, where it looked and found
        # nothing — not just print files_read=0 buried in a summary line.
        report = run_sweep(repo_root=tmp_path, fetch_closed_fn=lambda: {})
        assert report["files_read"] == 0
        assert "warning" in report
        assert str(tmp_path) in report["warning"]
        assert report["log_root"] == str(tmp_path)

        # Also loud on stderr, not just buried in the returned dict.
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert str(tmp_path) in captured.err

    def test_nonzero_files_read_carries_no_warning(self, tmp_path):
        log_dir = tmp_path / ".autonomous-team" / "dashboard-logs"
        log_dir.mkdir(parents=True)
        _write_log(log_dir / "manual-merge-1.log", ["[post-merge-hook] Closing Discussion #100"])

        report = run_sweep(repo_root=tmp_path, fetch_closed_fn=lambda: {})
        assert report["files_read"] == 1
        assert "warning" not in report

    def test_human_format_surfaces_the_warning(self, tmp_path):
        report = run_sweep(repo_root=tmp_path, fetch_closed_fn=lambda: {})
        text = _mod._format_human(report)
        assert "WARNING" in text
        assert "UNSCANNED" in text
        # Must not be the old, unconditionally-reassuring line.
        assert "No premature closes detected by either detector." not in text


class TestResolveLogRoot:
    """The fix for the Team Lead review round: a worktree's own
    .autonomous-team/dashboard-logs/ is always empty because
    scripts/merge-and-hook.sh (the only writer) never runs inside one — so
    resolving log_root from this file's own __file__ location silently
    scanned the wrong checkout. resolve_log_root() goes through
    backend.repo_root.main_repo_root() instead, which is git-common-dir
    aware and correct from inside a worktree.
    """

    def test_resolves_to_a_directory_backend_repo_root_agrees_with(self):
        # Exercises the real (non-injected) resolution path against this
        # actual checkout — the only way to prove the import + sys.path
        # wiring genuinely works, not just that the fallback branch does.
        import sys as _sys

        repo_dir = SCRIPT_PATH.resolve().parent.parent
        if str(repo_dir) not in _sys.path:
            _sys.path.insert(0, str(repo_dir))
        from backend.repo_root import main_repo_root

        assert resolve_log_root(fallback=Path("/should-not-be-used")) == main_repo_root()

    def test_falls_back_when_backend_repo_root_is_unimportable(self, monkeypatch, tmp_path):
        # Simulate this file having been copied out on its own, with no
        # sibling backend/ package — the exact scenario the Team Lead
        # review reproduced by extracting the script alone.
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "backend.repo_root" or name.startswith("backend.repo_root"):
                raise ImportError("simulated: no sibling backend/ package")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert resolve_log_root(fallback=tmp_path) == tmp_path


class TestCliContract:
    """Sweep mutates nothing and never accepts --repair/--reopen."""

    def test_repair_flag_rejected(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--repair"],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0

    def test_reopen_flag_rejected(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--reopen"],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0

    def test_json_flag_produces_valid_json(self, monkeypatch, capsys):
        # In-process, not subprocess: main() -> run_sweep() with no override
        # would hit the live network. Monkeypatch run_sweep itself so this
        # stays hermetic while still exercising main()'s own --json wiring.
        fake_report = {"sources_scanned": ["fake.log"], "files_read": 0, "discussions": []}
        monkeypatch.setattr(_mod, "run_sweep", lambda *a, **kw: fake_report)

        rc = _mod.main(["--json"])
        assert rc == 0

        import json as _json
        captured = capsys.readouterr()
        parsed = _json.loads(captured.out)
        assert parsed == fake_report

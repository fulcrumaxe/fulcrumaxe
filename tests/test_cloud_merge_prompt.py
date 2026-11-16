"""EB-10e: the Team Lead iteration prompt in run-loop-iteration.sh must send merges through the
wrapper that asks the cloud first, never tell an agent to run a raw squash-merge itself."""
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEXT = open(os.path.join(ROOT, "run-loop-iteration.sh")).read()
RAW = "gh pr " + "merge"


def _prompt_lines():
    start = TEXT.index("<<PROMPT")
    end = TEXT.index("\nPROMPT\n", start)
    return TEXT[start:end].splitlines()


def test_prompt_merge_step_uses_the_wrapper():
    step = [ln for ln in _prompt_lines() if ln.lstrip().startswith("7. Auto-merge")]
    assert len(step) == 1
    assert "bash scripts/merge-and-hook.sh --pr {number}" in step[0]


def test_prompt_never_instructs_a_raw_merge():
    for ln in _prompt_lines():
        i = ln.find(RAW)
        if i >= 0:
            assert "never" in ln[:i].lower(), ln


def test_wrapper_itself_asks_the_cloud():
    wrapper = open(os.path.join(ROOT, "scripts", "merge-and-hook.sh")).read()
    assert "cloud-merge-gate.sh" in wrapper

"""backend/telemetry_report.py — pure-read payload builder for the opt-in
telemetry report (D#2565).

PR-a (this file, this PR) ships only the network-safety seam:
:func:`maybe_send` is the single Python-side function that would ever
initiate a report send, and at gate-off (the default) it returns
immediately without importing ``socket``, ``requests``, or ``urllib``, or
touching the network in any way whatsoever -- this is what the Python
level-0 guard test targets (a pytest that patches ``socket.socket`` and
``socket.getaddrinfo`` to raise, then calls this function with the gate
off; the call must return cleanly with neither patch invoked).

The counts read + payload assembly -- a ``counts`` dict with exactly
``{spawns, prs_merged, gate_pass, sent_back}``, no ``failures`` key, and the
``--dry-run --json`` CLI that prints it -- lands in PR-b, along with the
actual send. The send itself is ``curl`` via ``scripts/lib/telemetry.sh``,
not Python: ``requests`` isn't installed on the reference host, and a bare
``urllib.request.urlopen`` with no timeout was measured hanging past 120s
during Spec review. Left as :class:`NotImplementedError` here rather than
guessed at, so PR-b's payload shape isn't pre-empted by this PR.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from repo root (mirrors control_plane.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import control_plane  # noqa: E402


def maybe_send(counts: dict | None = None) -> None:
    """The Python-side telemetry entry point.

    At gate-off this returns immediately -- no socket, no DNS, no
    subprocess. PR-a never calls this with the gate on; PR-b wires the
    actual send (curl, via scripts/lib/telemetry.sh) behind this seam.
    """
    if not control_plane.check_gate("telemetry_report"):
        return
    raise NotImplementedError("send seam wired in PR-b (D#2565)")


def main(argv: list[str] | None = None) -> int:
    print(
        "backend/telemetry_report.py: the payload builder (--dry-run --json) "
        "lands in PR-b (D#2565) -- this PR ships only the network-safety seam.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

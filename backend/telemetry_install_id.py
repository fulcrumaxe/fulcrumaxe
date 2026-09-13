"""backend/telemetry_install_id.py — per-install id + opt-in flow for the
opt-in telemetry report (D#2565).

The id is 32 random hex characters (``secrets.token_hex(16)``), generated
only on the false->true ``gates.telemetry_report`` transition -- never
lazily on read, and never derived from anything (hostname, repo slug, MAC,
``git config user.email``). It lives at
``backend.state_paths.STATE_DIR / ID_FILENAME``, outside any git repo and
with no in-repo pointer to it, so a clone or fork copies neither the id nor
a path to it (verified during Spec review: STATE_DIR sits outside every git
repo on this host, and the existing STATE_DIR pointers under
``.autonomous-team/`` are untracked local symlinks -- ``git ls-files -s``
returns nothing for any of them; this file adds no new pointer).

Reading is unconditionally pure -- :func:`read_id` never creates the file,
regardless of gate state (AC-6). Only :func:`ensure_id` mints one, and it is
called from exactly one place below: ``--opt-in``, the false->true
transition. A generate-on-read design would mint an id at level 0, and once
per scratch state dir in CI -- this module never does that.

``--opt-in`` is the whole consent flow: flip the gate, mint the id if
missing, and print the disclosure -- the field list, the disclosure URL, and
the erase command, but never the id itself, so the id never reaches
scrollback or a clipboard on a machine whose code plane is public.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

# Allow running as a script from repo root (mirrors control_plane.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Resolved through backend.state_paths at call time in every function below
# -- never bind STATE_DIR to a module-level name, which would freeze it
# against a later AUTONOMOUS_TEAM_STATE_DIR override (see state_paths.py's
# module docstring, D#1810).
from backend import control_plane, state_paths  # noqa: E402

ID_FILENAME = "telemetry-install-id"

_ID_RE = re.compile(r"[0-9a-f]{32}")

DISCLOSURE_URL = "https://fulcrumaxe.dev/telemetry.html"
ERASE_COMMAND = "bash scripts/telemetry-erase.sh"

# The exact field set the payload ships (AC-9, PR-b). Defined here, once, so
# the disclosure printed at opt-in (and again at first send, PR-b's AC-17)
# can never drift from the actual payload -- PR-b's payload builder should
# import DISCLOSED_FIELDS rather than re-deriving the list.
DISCLOSED_FIELDS: tuple[str, ...] = (
    "install",
    "version",
    "os",
    "window_hours",
    "counts.spawns",
    "counts.prs_merged",
    "counts.gate_pass",
    "counts.sent_back",
)


def _id_path() -> Path:
    """Resolved fresh on every call -- see module docstring."""
    return state_paths.STATE_DIR / ID_FILENAME


def read_id() -> str | None:
    """Pure read. Returns the id if the file exists AND matches the expected
    shape, else ``None``.

    Never creates the file. Safe to call any number of times at any gate
    state (AC-6) -- this function does not itself look at the gate at all.

    A shape mismatch (truncated write, a clobbered or hand-edited file) is
    treated as absent rather than passed through: this value flows straight
    into the erase wrapper's DELETE body and, in PR-b, the report payload,
    so a malformed value should skip the day exactly like a missing file
    does (AC-7's guard), not get sent anywhere.
    """
    try:
        value = _id_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not _ID_RE.fullmatch(value):
        return None
    return value


def ensure_id() -> str:
    """Return the install id, minting one only if this is the first opt-in.

    This is the false->true transition hook -- call it ONLY when actually
    opting in (see :func:`opt_in` below). Calling it anywhere else would
    mint an id the gate hasn't earned yet.
    """
    existing = read_id()
    if existing is not None:
        return existing

    new_id = secrets.token_hex(16)
    path = _id_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write with 0600 from the moment the file exists rather than
    # chmod-after -- belt-and-suspenders against a permissive umask on the
    # open() call itself.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(new_id + "\n")
    os.chmod(path, 0o600)
    return new_id


def format_disclosure() -> str:
    """The text printed at opt-in (and again at first send, PR-b) -- the
    field list, the disclosure URL, and the erase command. Never the id
    itself (AC-16).
    """
    lines = ["Telemetry report enabled. At most once a day, this sends:"]
    lines.extend(f"  - {field}" for field in DISCLOSED_FIELDS)
    lines.append(f"Details: {DISCLOSURE_URL}")
    lines.append(f"Erase everything ever sent: {ERASE_COMMAND}")
    return "\n".join(lines)


def opt_in() -> str:
    """Flip ``gates.telemetry_report`` on, mint the id if missing, and
    return the disclosure text (also printed to stdout by the CLI below).

    Never returns or prints the id itself.
    """
    cp = control_plane.ControlPlane()
    cp.load()
    cp.set("gates.telemetry_report", True)
    ensure_id()
    return format_disclosure()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="telemetry_install_id",
        description="Install id + opt-in flow for the telemetry report (D#2565).",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--read", action="store_true",
        help="Print the install id if one exists, else print nothing (exit 0 either way).",
    )
    g.add_argument(
        "--opt-in", action="store_true", dest="opt_in_flag",
        help="Enable gates.telemetry_report, mint the id if missing, print the disclosure.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.read:
        existing = read_id()
        if existing:
            print(existing)
        return 0
    if args.opt_in_flag:
        print(opt_in())
        return 0
    return 1  # unreachable -- mutually exclusive group is required


if __name__ == "__main__":
    sys.exit(main())

"""snapshot_path.py — the one definition of the loop-snapshot file location.

Before this module the path was spelled out as a hard-coded literal under /tmp in
seven places, while the only script that ran the producer wrote to a PID-suffixed
sibling of it that nobody read.  Every consumer now derives the path from here.

Resolution order (highest priority first):

1. ``SNAPSHOT_PATH``              — explicit full path to the file.  Pre-existing
   override, used heavily by ``tests/test_merge_gate.sh`` and
   ``tests/test_loop_phased_step5.sh``; it still wins.
2. ``AUTONOMOUS_TEAM_STATE_DIR``  — via ``backend.state_paths.STATE_DIR``.
3. ``~/.fulcrumaxe-state/loop-snapshot.json`` — the default.

Not ``/tmp``: ``backend/state_paths.py`` is the declared home for mutable runtime
state, and a ``/tmp`` file is a guaranteed cold miss after every reboot.  The
flip side — a state-dir file outliving a reboot and sitting there looking
present for days — is handled by readers staleness-checking against
``MAX_AGE_SECONDS``, not by picking a directory that gets wiped.

Resolution timing (D#1979)
---------------------------
Both ``resolve()`` and the module attribute ``SNAPSHOT_PATH`` are resolved at
*call time*, not import time — mirroring ``backend/state_paths.py``'s own
rule ("Always go through the module, even inside a function"). ``SNAPSHOT_PATH``
is not a real module-level constant; it is served by a :pep:`562` module
``__getattr__`` that calls :func:`resolve` on every access, so changing
``AUTONOMOUS_TEAM_STATE_DIR`` after import changes what both accessors return.
``resolve()`` reads ``state_paths.STATE_DIR`` through the ``state_paths``
module object rather than importing the name directly, for the same reason:
binding a :pep:`562`-resolved value to a module-global freezes it for the life
of the process (see ``state_paths.py``'s "Resolution timing" section, and
D#1810, which this mirrors).

Known residual freeze NOT fixed by D#1979 (out of scope, on purpose)
---------------------------------------------------------------------
``backend/loop_snapshot.py`` still does ``DEFAULT_SNAPSHOT_PATH =
str(SNAPSHOT_PATH)`` at its own import time, and ``load()`` takes that string
as a function-definition-time default argument — the same freeze-a-call-time-
resolver pattern this file's ``__getattr__`` exists to eliminate, one level
further downstream. It is not fixed here because changing a public function's
default-argument signature has a wider blast radius than a red-``main``
hotfix should carry; see D#2034.

Usage from Python::

    from backend.snapshot_path import SNAPSHOT_PATH, MAX_AGE_SECONDS

Usage from bash / TypeScript — this module is also its own accessor, so callers
never have to duplicate the default string::

    SNAPSHOT_PATH="${SNAPSHOT_PATH:-$(python3 backend/snapshot_path.py)}"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `python3 backend/snapshot_path.py` from the repo root, where `backend`
# is not yet importable as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import state_paths

#: Basename of the snapshot file inside the state directory.
SNAPSHOT_FILENAME = "loop-snapshot.json"

#: Age beyond which a snapshot must not be treated as describing current state.
#: Matches ``run_analyst.classify_stale_snapshot_consumption``'s threshold and
#: ``loop_snapshot.DEFAULT_MAX_AGE_SECONDS``.  The refresh timer runs at half
#: this interval so a single missed tick is not enough to go stale.
MAX_AGE_SECONDS = 600


def resolve() -> Path:
    """Return the snapshot path, honouring both env overrides at call time.

    Prefer the module-level :data:`SNAPSHOT_PATH` accessor; use this directly
    when the environment may have changed after import (tests, long-lived
    processes) — though both now resolve fresh on every access.
    """
    override = os.environ.get("SNAPSHOT_PATH")
    if override:
        return Path(override)
    return state_paths.STATE_DIR / SNAPSHOT_FILENAME


def __getattr__(name: str):
    """PEP 562 module-level attribute resolution for ``SNAPSHOT_PATH``.

    Fires only for names absent from module globals, which is what makes
    ``SNAPSHOT_PATH`` resolve fresh on every access instead of freezing at
    import time — the second of the two freezes fixed by D#1979. This also
    means ``monkeypatch.setattr(sp, "SNAPSHOT_PATH", ...)`` still works:
    ``setattr`` installs a real entry in the module's ``__dict__``, which
    shadows ``__getattr__`` for ordinary lookup, exactly like
    ``backend/state_paths.py`` already relies on for its own PEP 562 names.
    """
    if name == "SNAPSHOT_PATH":
        return resolve()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    print(resolve())

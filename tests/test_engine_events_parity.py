"""Schema parity with the cloud's protocol file, proven by fixtures rather than by hash.

The cloud copy inlines its definitions and this copy uses $ref/$defs, so the two files are
not byte-identical. What has to agree is the set of events each accepts: every fixture the
cloud calls valid must pass here, and every one it calls invalid must fail here.
"""
import glob
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FX = os.path.join(ROOT, "tests", "fixtures", "engine-events-parity")
sys.path.insert(0, os.path.join(ROOT, "scripts", "lib"))
import engine_emit  # noqa: E402


def _source():
    with open(os.path.join(FX, "SOURCE")) as f:
        return f.read().strip()


def _files(kind):
    return sorted(glob.glob(os.path.join(FX, kind, "*.json")))


def _load(path):
    with open(path) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def validator():
    jsonschema = pytest.importorskip("jsonschema")
    schema = engine_emit.load_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def test_fixture_set_is_complete():
    assert len(_files("valid")) == 26 and len(_files("invalid")) == 81, _source()


@pytest.mark.parametrize("path", _files("valid"), ids=os.path.basename)
def test_valid_fixture_passes(path, validator):
    ev = _load(path)
    errs = [e.message for e in validator.iter_errors(ev)]
    assert not errs, f"{errs}\nfixtures from:\n{_source()}"
    engine_emit.validate(ev)  # raises EmitError if the emitter's own check refuses it


@pytest.mark.parametrize("path", _files("invalid"), ids=os.path.basename)
def test_invalid_fixture_fails(path, validator):
    ev = _load(path)
    assert not validator.is_valid(ev), f"accepted by the JSON Schema validator\nfixtures from:\n{_source()}"
    with pytest.raises(engine_emit.EmitError):
        engine_emit.validate(ev)

"""Tests for backend/cost_pricing.py — shared token-cost pricing module.

Covers:
  - cost_usd() basic math for each token type
  - Combined token types
  - Default model selection
  - Unknown model falls back to _default
  - Zero tokens → zero cost
  - rates_for_model() lookup
  - RATE_CARD structure invariants
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure repo root is on path so the module resolves as backend.cost_pricing
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.cost_pricing import (
    RATE_CARD,
    DEFAULT_MODEL,
    cost_usd,
    rates_for_model,
)


# ---------------------------------------------------------------------------
# cost_usd — per token type
# ---------------------------------------------------------------------------

class TestCostUsdPerType:
    def test_input_1m_tokens(self):
        # 1M input × $3.00/1M = $3.00
        result = cost_usd(input_tok=1_000_000, output_tok=0)
        assert abs(result - 3.00) < 1e-6

    def test_output_1m_tokens(self):
        # 1M output × $15.00/1M = $15.00
        result = cost_usd(input_tok=0, output_tok=1_000_000)
        assert abs(result - 15.00) < 1e-6

    def test_cache_read_1m_tokens(self):
        # 1M cache_read × $0.30/1M = $0.30
        result = cost_usd(input_tok=0, output_tok=0, cache_read=1_000_000)
        assert abs(result - 0.30) < 1e-6

    def test_cache_write_1m_tokens(self):
        # 1M cache_write × $3.75/1M = $3.75
        result = cost_usd(input_tok=0, output_tok=0, cache_write=1_000_000)
        assert abs(result - 3.75) < 1e-6

    def test_cache_read_cheaper_than_input(self):
        # cache_read ($0.30/1M) is cheaper than input ($3.00/1M)
        cost_cr = cost_usd(0, 0, cache_read=1_000_000)
        cost_in = cost_usd(1_000_000, 0)
        assert cost_cr < cost_in

    def test_cache_write_pricier_than_input(self):
        # cache_write ($3.75/1M) is more expensive than input ($3.00/1M)
        cost_cw = cost_usd(0, 0, cache_write=1_000_000)
        cost_in = cost_usd(1_000_000, 0)
        assert cost_cw > cost_in


# ---------------------------------------------------------------------------
# cost_usd — combined token counts
# ---------------------------------------------------------------------------

class TestCostUsdCombined:
    def test_known_combination(self):
        """Manual arithmetic check for a realistic agent run."""
        input_tok = 10_000
        output_tok = 1_000
        cache_read = 5_000
        cache_write = 2_000

        expected = (
            10_000 * 3.00 / 1_000_000
            + 1_000 * 15.00 / 1_000_000
            + 5_000 * 0.30 / 1_000_000
            + 2_000 * 3.75 / 1_000_000
        )
        result = cost_usd(input_tok, output_tok, cache_read=cache_read, cache_write=cache_write)
        assert abs(result - expected) < 1e-9

    def test_zero_tokens_zero_cost(self):
        assert cost_usd(0, 0, 0, 0) == 0.0

    def test_result_rounded_to_8dp(self):
        # With an odd token count the raw float has many decimal places;
        # result must be rounded to 8 decimal places.
        result = cost_usd(7, 3)
        # verify precision is at most 8 decimal places
        assert result == round(result, 8)

    def test_small_run_nonzero(self):
        # Even a tiny run (10 input, 5 output) should produce a positive cost
        result = cost_usd(10, 5)
        assert result > 0.0


# ---------------------------------------------------------------------------
# cost_usd — model selection
# ---------------------------------------------------------------------------

class TestCostUsdModel:
    def test_default_model_used_when_none(self):
        """model=None should give same result as model=DEFAULT_MODEL."""
        cost_none = cost_usd(10_000, 500, model=None)
        cost_explicit = cost_usd(10_000, 500, model=DEFAULT_MODEL)
        assert cost_none == cost_explicit

    def test_default_model_used_when_omitted(self):
        """Omitting model arg gives same result as explicit DEFAULT_MODEL."""
        cost_omitted = cost_usd(10_000, 500)
        cost_explicit = cost_usd(10_000, 500, model=DEFAULT_MODEL)
        assert cost_omitted == cost_explicit

    def test_unknown_model_falls_back_to_default(self):
        """Unrecognised model should fall back to _default rates, not raise."""
        cost_unknown = cost_usd(10_000, 500, model="gpt-99-turbo")
        cost_default = cost_usd(10_000, 500, model="_default")
        assert cost_unknown == cost_default

    def test_empty_string_model_falls_back(self):
        """Empty string model falls back to default."""
        cost_empty = cost_usd(10_000, 500, model="")
        cost_default = cost_usd(10_000, 500, model=DEFAULT_MODEL)
        assert cost_empty == cost_default

    def test_explicit_default_key_works(self):
        """model='_default' is a valid key and should not raise."""
        result = cost_usd(1_000, 100, model="_default")
        assert result > 0.0


# ---------------------------------------------------------------------------
# rates_for_model
# ---------------------------------------------------------------------------

class TestRatesForModel:
    def test_returns_dict_with_required_keys(self):
        rates = rates_for_model()
        assert set(rates.keys()) == {"input", "output", "cache_write", "cache_read"}

    def test_default_model_rates_match_sonnet_46(self):
        rates_default = rates_for_model()
        rates_explicit = rates_for_model(DEFAULT_MODEL)
        assert rates_default == rates_explicit

    def test_unknown_model_returns_default_rates(self):
        rates_unknown = rates_for_model("nonexistent-model")
        rates_fallback = rates_for_model("_default")
        assert rates_unknown == rates_fallback

    def test_returns_copy_not_reference(self):
        """Mutating the returned dict must not affect RATE_CARD."""
        rates = rates_for_model()
        rates["input"] = 999.0
        assert RATE_CARD["_default"]["input"] != 999.0


# ---------------------------------------------------------------------------
# RATE_CARD structure
# ---------------------------------------------------------------------------

class TestRateCardStructure:
    def test_default_key_present(self):
        assert "_default" in RATE_CARD

    def test_sonnet_46_key_present(self):
        assert "claude-sonnet-4-6" in RATE_CARD

    def test_all_entries_have_required_fields(self):
        required = {"input", "output", "cache_write", "cache_read"}
        for model, rates in RATE_CARD.items():
            missing = required - set(rates.keys())
            assert not missing, f"Model '{model}' missing fields: {missing}"

    def test_all_rates_positive(self):
        for model, rates in RATE_CARD.items():
            for field, value in rates.items():
                assert value > 0, f"Model '{model}' field '{field}' must be > 0"

    def test_default_and_sonnet_rates_identical(self):
        """_default matches claude-sonnet-4-6 — project uses Sonnet for all spawns."""
        assert RATE_CARD["_default"] == RATE_CARD["claude-sonnet-4-6"]


# ---------------------------------------------------------------------------
# D#2294 — backend/cost_tracker.py's separate _DEFAULT_PRICING table.
#
# The classes below cover backend.cost_tracker (not backend.cost_pricing
# above — the two modules are independent rate cards; see the D#2294 PR
# body for why this file, despite its name, is the natural home for both).
# ---------------------------------------------------------------------------

from backend.cost_tracker import (  # noqa: E402
    _DEFAULT_CONFIG_PATH,
    _DEFAULT_PRICING,
    _WARNED_MISSING_KEYS,
    _WARNED_UNKNOWN_MODELS,
    _compute_cost,
    _load_pricing,
)


class TestPricingTablesStayEqual:
    """_load_pricing() returns config.json's `pricing` block *wholesale*
    whenever it parses and is non-empty — _DEFAULT_PRICING is reached only
    when the config read fails. A PR that edits only one table is therefore
    a silent no-op on any host with a populated config.json (D#2294). This
    guard is what stops that recurring.
    """

    def test_default_pricing_matches_config_json_pricing_block(self):
        import json

        repo_root = Path(__file__).resolve().parent.parent.parent
        config_path = repo_root / _DEFAULT_CONFIG_PATH
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        config_pricing = cfg["pricing"]

        mismatched = sorted(
            model
            for model in set(_DEFAULT_PRICING) | set(config_pricing)
            if _DEFAULT_PRICING.get(model) != config_pricing.get(model)
        )
        assert not mismatched, (
            "backend/cost_tracker.py:_DEFAULT_PRICING and "
            ".autonomous-team/config.json's `pricing` block have diverged "
            f"for model(s) {mismatched} — _load_pricing() returns the config "
            "block wholesale at runtime, so editing only one table silently "
            "changes no figure (D#2294)."
        )


class TestHandPricedRow:
    """D#2149 acceptance: a hand-computed price for a real agent_run row
    must match what _compute_cost emits. Row is D#2249 / PR #2258's
    code-reviewer-2249-1788413413 (whole agent_run table, Linux dev host).
    Skips cleanly if that exact row isn't present on this host/checkout.
    """

    _AGENT_ID = "code-reviewer-2249-1788413413"
    # input_tok=394, output_tok=38636, cache_read=20040467, cache_write=218998
    # — confirmed against the live stats.duckdb during D#2294 triage.
    _INPUT_TOK = 394
    _OUTPUT_TOK = 38636
    _CACHE_READ = 20_040_467
    _CACHE_WRITE = 218_998

    def test_hand_priced_row_matches_compute_cost(self):
        try:
            import duckdb
        except ImportError:
            pytest.skip("duckdb not installed")

        db_path = Path.home() / ".autonomous-forever-state" / "stats.duckdb"
        if not db_path.exists():
            pytest.skip(f"stats.duckdb not found at {db_path} on this host/checkout")

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            row = conn.execute(
                "SELECT model, input_tok, output_tok, cache_read, cache_write "
                "FROM agent_run WHERE agent_id = ?",
                [self._AGENT_ID],
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            pytest.skip(f"agent_id {self._AGENT_ID!r} not found in {db_path}")

        model, input_tok, output_tok, cache_read, cache_write = row
        assert (input_tok, output_tok, cache_read, cache_write) == (
            self._INPUT_TOK,
            self._OUTPUT_TOK,
            self._CACHE_READ,
            self._CACHE_WRITE,
        ), "recorded row no longer matches the values sourced in the Spec — re-verify by hand"

        rates = _DEFAULT_PRICING[model]
        # Term-by-term arithmetic, matching the PR body.
        expected = (
            input_tok / 1000.0 * rates["input_per_1k"]
            + output_tok / 1000.0 * rates["output_per_1k"]
            + cache_read / 1000.0 * rates["cache_read_per_1k"]
            + cache_write / 1000.0 * rates["cache_write_5m_per_1k"]
        )
        actual = _compute_cost(
            input_tok,
            output_tok,
            model,
            _DEFAULT_PRICING,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        assert round(actual, 6) == round(expected, 6)
        assert round(actual, 6) == 7.414105


class TestMissingRateKeyWarning:
    """A pricing entry that omits a key the recorded data populates must
    warn — the same way an unknown model does (D#2294 item 5).
    """

    def test_warns_once_per_model_key_pair(self, recwarn):
        _WARNED_MISSING_KEYS.discard(("m", "cache_read_per_1k"))
        pricing = {"m": {"input_per_1k": 0.003}}

        _compute_cost(0, 0, "m", pricing, cache_read_tokens=1000)
        messages = [str(w.message) for w in recwarn.list]
        assert any("m" in msg and "cache_read_per_1k" in msg for msg in messages), messages

        recwarn.clear()
        _compute_cost(0, 0, "m", pricing, cache_read_tokens=1000)
        assert len(recwarn.list) == 0, "second call with the same (model, key) must not re-warn"

        _WARNED_MISSING_KEYS.discard(("m", "cache_read_per_1k"))

    def test_no_warning_when_the_token_class_is_zero(self, recwarn):
        _WARNED_MISSING_KEYS.discard(("m2", "cache_read_per_1k"))
        pricing = {"m2": {"input_per_1k": 0.003}}

        _compute_cost(0, 0, "m2", pricing, cache_read_tokens=0)
        assert len(recwarn.list) == 0


class TestUnknownModelWarningSymmetry:
    """The existing unknown-model warning must fire only when the row
    actually contributes tokens (D#2294 item 6) — 583 token-carrying rows
    should not become 583+1079 warnings once the null/zero-token rows are
    included.
    """

    def test_no_warning_for_zero_token_unknown_model(self, recwarn):
        _WARNED_UNKNOWN_MODELS.discard("sonnet")
        pricing = {"default": {"input_per_1k": 0.003, "output_per_1k": 0.015}}

        _compute_cost(0, 0, "sonnet", pricing)
        assert len(recwarn.list) == 0

    def test_warning_for_nonzero_token_unknown_model(self, recwarn):
        _WARNED_UNKNOWN_MODELS.discard("sonnet")
        pricing = {"default": {"input_per_1k": 0.003, "output_per_1k": 0.015}}

        _compute_cost(100, 0, "sonnet", pricing)
        messages = [str(w.message) for w in recwarn.list]
        assert any("sonnet" in msg for msg in messages), messages

        _WARNED_UNKNOWN_MODELS.discard("sonnet")


class TestModelPricingCompleteness:
    """The durable regression test (D#2294 item 7): every model + token
    class combination actually present in agent_run must have a rate key
    in the effective pricing table, or that token class is silently priced
    at $0.00. Fails on the pre-fix tables, passes post-fix — see the PR
    body for both runs.

    Reads stats.duckdb directly rather than via backend.state_paths: this
    test needs the real recorded data (which models actually carry which
    token classes), not a sandboxed scratch fixture — see CLAUDE.md's
    AUTONOMOUS_TEAM_STATE_DIR rule. Skips cleanly (with a printed reason)
    when the db is absent, e.g. a fresh CI checkout with no recorded runs.
    """

    @staticmethod
    def _stats_db_path() -> Path | None:
        env = os.environ.get("STATS_DB_PATH")
        path = Path(env) if env else Path.home() / ".autonomous-forever-state" / "stats.duckdb"
        return path if path.exists() else None

    def test_every_populated_token_class_has_a_rate_key(self):
        db_path = self._stats_db_path()
        if db_path is None:
            pytest.skip(
                "stats.duckdb not found (checked $STATS_DB_PATH and "
                "~/.autonomous-forever-state/stats.duckdb) — no recorded "
                "agent_run data available on this host/checkout"
            )
        try:
            import duckdb
        except ImportError:
            pytest.skip("duckdb not installed")

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT model,
                       SUM(input_tok)   AS input_tok,
                       SUM(output_tok)  AS output_tok,
                       SUM(cache_read)  AS cache_read,
                       SUM(cache_write) AS cache_write
                FROM agent_run
                WHERE model IS NOT NULL
                GROUP BY model
                """
            ).fetchall()
        finally:
            conn.close()

        pricing = _load_pricing()
        key_for_class = {
            "input_tok": "input_per_1k",
            "output_tok": "output_per_1k",
            "cache_read": "cache_read_per_1k",
            "cache_write": "cache_write_5m_per_1k",
        }

        missing = []
        for model, input_tok, output_tok, cache_read, cache_write in rows:
            populated = {
                "input_tok": input_tok,
                "output_tok": output_tok,
                "cache_read": cache_read,
                "cache_write": cache_write,
            }
            rates = pricing.get(model, {})
            for token_class, total in populated.items():
                if total and total > 0 and key_for_class[token_class] not in rates:
                    missing.append((model, key_for_class[token_class], int(total)))

        assert not missing, (
            "model(s) with a populated token class but no rate key for it "
            f"(scope: whole agent_run table, host: {db_path}): {missing}"
        )

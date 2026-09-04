"""backend/cost_pricing.py — Shared token-cost pricing model.

Single source of truth for Anthropic API token prices.  Every module that
needs to convert token counts to USD should import ``cost_usd`` from here
rather than maintaining its own inline rate card.

Rate source
-----------
Anthropic public pricing page, snapshot 2026-05-20:
  https://www.anthropic.com/pricing  (claude-sonnet-4-6)

  Input tokens:        $3.00 / 1M tokens
  Output tokens:      $15.00 / 1M tokens
  Cache write tokens:  $3.75 / 1M tokens
  Cache read tokens:   $0.30 / 1M tokens

Default model is ``claude-sonnet-4-6`` — the canonical model for SDK-routed
agent spawns in this project.  Unknown models fall back to the ``_default``
entry, which uses the same Sonnet 4.6 rates.

Usage::

    from backend.cost_pricing import cost_usd

    usd = cost_usd(input_tok=10_000, output_tok=500)
    usd = cost_usd(10_000, 500, cache_read=2_000, cache_write=1_000)
    usd = cost_usd(10_000, 500, model="claude-sonnet-4-6")
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Rate card — USD per 1M tokens
# ---------------------------------------------------------------------------

#: Per-model rate card.  All values are USD per *one million* tokens.
#:
#: Add a new entry here when the project starts using a new model.
#: The ``_default`` key is used when ``model`` is ``None``, empty, or unknown.
RATE_CARD: dict[str, dict[str, float]] = {
    # claude-sonnet-4-6 — canonical model for SDK-routed spawns (2026-05-20)
    "claude-sonnet-4-6": {
        "input":       3.00,   # $3.00 / 1M input tokens
        "output":      15.00,  # $15.00 / 1M output tokens
        "cache_write": 3.75,   # $3.75 / 1M cache-write tokens
        "cache_read":  0.30,   # $0.30 / 1M cache-read tokens
    },
    # Fallback / default when model column is NULL or unrecognised.
    # Matches Sonnet 4.6 rates — the safest assumption for this project.
    "_default": {
        "input":       3.00,
        "output":      15.00,
        "cache_write": 3.75,
        "cache_read":  0.30,
    },
}

#: Model used when no ``model`` argument is supplied.
DEFAULT_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def cost_usd(
    input_tok: int,
    output_tok: int,
    cache_read: int = 0,
    cache_write: int = 0,
    model: str | None = None,
) -> float:
    """Compute the USD cost for a set of token counts.

    Parameters
    ----------
    input_tok:
        Regular (non-cached) input tokens.
    output_tok:
        Output tokens.
    cache_read:
        Cache-read tokens.  Priced at a discount ($0.30/1M for Sonnet 4.6).
    cache_write:
        Cache-write tokens.  Priced slightly above input ($3.75/1M).
    model:
        Model identifier, e.g. ``"claude-sonnet-4-6"``.  ``None`` and
        unrecognised strings both fall back to the ``_default`` entry.

    Returns
    -------
    float
        Estimated cost in USD, rounded to 8 decimal places.

    Examples
    --------
    >>> cost_usd(1_000_000, 0)
    3.0
    >>> cost_usd(0, 1_000_000)
    15.0
    >>> cost_usd(0, 0, cache_read=1_000_000)
    0.3
    >>> cost_usd(0, 0, cache_write=1_000_000)
    3.75
    """
    key = model or DEFAULT_MODEL
    rates = RATE_CARD.get(key) or RATE_CARD["_default"]

    _1m = 1_000_000.0
    cost = (
        input_tok    * rates["input"]       / _1m
        + output_tok * rates["output"]      / _1m
        + cache_read * rates["cache_read"]  / _1m
        + cache_write * rates["cache_write"] / _1m
    )
    return round(cost, 8)


def rates_for_model(model: str | None = None) -> dict[str, float]:
    """Return the per-1M-token rate dict for *model*.

    The returned dict has keys ``input``, ``output``, ``cache_write``,
    ``cache_read``.  Values are USD per 1M tokens.

    Unknown or ``None`` model falls back to ``_default``.
    """
    key = model or DEFAULT_MODEL
    return dict(RATE_CARD.get(key) or RATE_CARD["_default"])

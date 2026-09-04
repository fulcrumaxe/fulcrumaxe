"""backend/corpus_drift/claims — one module per hand-curated claim.

Each module exports an evaluate() function with the signature:
    evaluate(runs, transcripts_dir, window_days, sample_cap, **kwargs) -> ClaimResult

Adding a new claim: create a new .py file here; the CLI hub imports them by name.
"""

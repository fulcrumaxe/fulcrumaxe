# SPIKE-1: Bun vs Node DuckDB go/no-go for stats routes

**Date**: 2026-05-23  
**DB**: `~/.autonomous-forever-state/stats.duckdb` (7 MB, 4 tables, 3544 agent_run rows / 3789 metric_event rows)  
**Package**: `@duckdb/node-api@1.5.3-r.1` ("Neo" DuckDB binding)  
**Runtimes**: Bun 1.3.14, Node v24.15.0  

---

## Verdict: GO (Bun-native)

`@duckdb/node-api` installs, loads, and runs correctly under Bun 1.3.14. Results are identical to Node v24.15.0 and match the Python `duckdb` baseline values. 3/3 repeated Bun runs, zero crashes, zero stability errors across 50 sequential + 20 concurrent queries.

---

## Evidence

### Installation

```
bun add @duckdb/node-api
installed @duckdb/node-api@1.5.3-r.1  (exit 0)
```

The N-API native addon loaded successfully on first import under Bun 1.3.14. No errors, no missing symbols.

### Query harness (harness.ts)

Five checks mirroring the real stats routes:

| # | Check | Bun | Node |
|---|-------|-----|------|
| Q1 | metric_event window-fn summary (16 rows, timestamps) | PASS | PASS |
| Q2 | agent_run APPROX_QUANTILE float aggregates + BigInt COUNT(*) | PASS | PASS |
| Q3 | agent_run SUM/MAX on int64 token columns | PASS | PASS |
| Q4 | Parametrised CAST(? AS TIMESTAMP) filter (prepared stmt) | PASS | PASS |
| S  | Stability: 50 sequential + 20 concurrent open/query/close | PASS | PASS |

**3 consecutive Bun runs: all 5/5.** No crashes, no leaks.

### Numeric fidelity (Bun vs Python baseline)

| Metric | Python | Bun |
|--------|--------|-----|
| COUNT(agent_run WHERE end_ts NOT NULL) | 3543 | 3543 ok |
| MAX(duration_s) | 249042.43 | 249042.43 ok |
| APPROX_QUANTILE(duration_s, 0.50) | 0.0639 | 0.0639 ok |
| APPROX_QUANTILE(duration_s, 0.99) | 2920.31 | 2920.31 ok |
| SUM(input_tok) | 527056 | 527056 ok |
| MAX(input_tok) | 80000 | 80000 ok |

### Bun#17216 concern (microtask-scale concurrency)

20 simultaneous Promise.all queries against the read-only DB: **0 errors, 0 crashes** across 3 repeated harness runs.

---

## Important API Note (affects Phase 3 implementation)

`@duckdb/node-api` v1.5.x ("Neo") has a different type system from the Python duckdb binding:

- **Timestamps** are returned as `DuckDBTimestampValue` objects with `.micros: BigInt` (not JS Date).
  Convert: `new Date(Number(tsVal.micros / 1000n))`
- **COUNT(*) and integer aggregates** return `bigint`, not `number`.
- Connection cleanup uses `closeSync()` / `disconnectSync()` / `destroySync()`.

Phase 3 should include a thin `duckdb-helpers.ts` conversion module.

---

## Conclusion

Bun-native path is viable. No Node fallback service is needed. The stats routes can read stats.duckdb directly via @duckdb/node-api under Bun 1.3.14.

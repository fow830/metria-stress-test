# Metria Stress Test (MST)

**Status:** Commit #0 — published schema only. No runner yet.

Status-first evidence protocol for cold external code audits. Machine output is `pass | warn | fail | null` per lens, plus raw metrics and evidence. There is **no Ideal Score**, **no A–F grade/band**, and **no numeric risk advisory**.

Human reports map machine `null` to **UNDECLARED** (subject’s gap) or **NOT MEASURED** (our tooling gap).

## What this repo is

Separate GitHub project for the Metria Stress Test contract and (later) CLI.

| Shipped now | Not yet |
|---|---|
| `schema/stress-run.schema.json` **v0.1** | `metria run` CLI |
| Valid + invalid fixtures | Adapters (Semgrep, Syft, pytest, …) |
| Python schema tests | Corpus of public repos |

## Schema v0.1 (status-first)

- `StressRun.overall` ∈ `{pass, warn, fail}` — hard-gate aggregation only
- Each `DimensionResult.status` ∈ `{pass, warn, fail, null}`
- `score` is always `null` with `score_reason: "uncalibrated"` until a published calibration corpus exists
- `additionalProperties: false` on run and lens objects — banned fields (`ideal_score`, `idealnost`, `overall_score`, `risk_score`, `grade`, `band`) break CI
- Null lenses require `null_reason` + `null_owner` (`subject` | `metria`)
- Lenses in schema: `structural`, `supply_chain`, `test_adequacy`, `controls`, `architectural`, `normative` (experimental)

## Validate fixtures

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest schema/tests -q
```

## Roadmap (from accepted Claude review)

0. **This commit** — README + LICENSE + schema v0.1 + fixture CI  
1. Policy profile `cold_external`  
2. Discovery → `SubjectManifest`  
3. Sandbox runner  
4–7. Structural / supply-chain / evidence pack / adequacy adapters  
8–9. Public corpus + published results (including FAIL / NOT MEASURED)  

Product code before this schema lands is out of scope.

## License

Apache-2.0 — see [LICENSE](LICENSE).

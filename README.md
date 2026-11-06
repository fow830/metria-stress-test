# Metria Stress Test

Status-first evidence protocol for external software stress runs.

This repository is the product home for **Metria**. Commit #0 ships only the
machine contract: `schema/stress-run.schema.json` v0.1, fixtures, and schema
tests. No CLI, adapters, or scores yet.

## Decisions (authoritative)

Accepted first-total review (2026-11-06):

- **Build the product.** v1 is status-first (`pass` / `warn` / `fail` / `null`).
- Field `score` stays in the schema but is **always `null`** with
  `score_reason: "uncalibrated"` until a published calibrated corpus exists.
- **Banned:** Ideal Score, `grade`, `band` (A–F), numeric `risk_advisory`, and
  weighted protectability blends.
- **Protectability split:** `supply_chain` + `controls` (no weighted sum).
- **Normative** is experimental, reserved in schema, out of MVP measurement.
- Human report uses **`UNDECLARED` / `NOT MEASURED`**, never bare `NULL`.
- Five lenses remain in the schema vision; v1 measures `structural`,
  `supply_chain`, and `test_adequacy`; `architectural` / `normative` /
  `controls` are often undeclared.

## Layout

```text
schema/stress-run.schema.json          # JSON Schema draft 2020-12
schema/fixtures/valid-minimal.json     # minimal valid StressRun
schema/fixtures/invalid-*.json         # negative containment fixtures
tests/test_stress_run_schema.py        # pytest + jsonschema
```

## Run schema tests

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest -q
```

Valid fixtures must validate. Fixtures that introduce `ideal_score`, `grade`,
`band`, blended protectability `weights`, or `status: null` without
`null_reason` / `null_owner` / `null_display` must be rejected.

## Commit map

| Commit | Artifact |
|--------|----------|
| **#0** (this) | Schema v0.1 + negative fixtures + tests |
| #1+ | Policy, discovery, sandbox, adapters, corpus (see review) |

No product code before the schema CI is green.

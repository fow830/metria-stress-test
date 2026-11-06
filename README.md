# Metria Stress Test

**Status:** Commit #0 schema merged · MVP CLI + adapters + fixed-SHA corpus (this PR)

Status-first evidence protocol for cold external software audits. Machine output is
`pass | warn | fail | null` per lens, plus raw metrics and evidence. There is
**no Ideal Score**, **no A–F grade/band**, and **no numeric risk advisory**.

Human reports map machine `null` → **UNDECLARED** (subject gap) or **NOT MEASURED** (our tooling gap).

## Quick start

```bash
python3 -m pip install -e ".[dev]"
metria run /path/to/checkout --out metria-out --print-report
# or pinned remote:
metria run https://github.com/pallets/click@006ae84d651846c944331fff78e340701fe58562 --out metria-out
```

Outputs: `stress-run.json` (schema v0.1), `report.txt`, `evidence.zip`.

## What v0.1 measures

| Lens | v0.1 behavior |
|------|----------------|
| `structural` | AST danger patterns + HEAD-tree secret heuristics + optional `ruff` |
| `supply_chain` | Lockfile/requirements inventory + OSV queries; `sbom_practice` as sub-result |
| `test_adequacy` | pytest discovery/collect/run (budgeted); no strong oracle → `warn` |
| `controls` | `UNDECLARED` |
| `architectural` | `UNDECLARED` (or `NOT MEASURED` if markers found but not executed) |
| `normative` | `UNDECLARED` · experimental |

`score` is always `null` with `score_reason: "uncalibrated"`.

## Schema containment

```bash
python3 -m pytest -q
```

Negative fixtures reject `ideal_score`, `grade`, `band`, blended protectability weights,
and `status: null` without attribution.

## Fixed-SHA corpus

Subjects are pinned in `corpus/subjects.yaml`. Publish all results, including FAIL / NOT MEASURED:

```bash
python3 scripts/run_corpus.py
# results → corpus/results/<id>@<sha12>/
```

| id | repo | tag / pin |
|----|------|-----------|
| click | pallets/click | 8.1.7 |
| requests | psf/requests | v2.32.3 |
| six | benjaminp/six | 1.16.0 |
| httpx | encode/httpx | 0.27.2 |
| flask | pallets/flask | 3.0.3 |

## Layout

```text
schema/stress-run.schema.json
policy/cold_external.yaml
metria/                    # CLI + adapters
corpus/subjects.yaml
corpus/results/            # published runs
docs/                      # playbook + Claude review reports
tests/
```

## Claude review (GitHub)

External Claude reviews this PR via GitHub only (no zip packs).
Playbook: [docs/CURSOR-CLAUDE-GITHUB-REVIEW-PLAYBOOK.md](docs/CURSOR-CLAUDE-GITHUB-REVIEW-PLAYBOOK.md).
Reports land in `docs/reviews/claude-rN.md` on the same PR branch.

## License

Apache-2.0 — see [LICENSE](LICENSE).

#!/usr/bin/env python3
"""Clone fixed-SHA corpus subjects and run metria; publish results into corpus/results/."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metria.runner import load_policy, run_subject, runner_environment  # noqa: E402
from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402


def validate_run(run: dict) -> None:
    schema = json.loads((ROOT / "schema" / "stress-run.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(run)


def checkout(url: str, sha: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "init", "-q"], cwd=str(dest))
    subprocess.check_call(["git", "remote", "add", "origin", url], cwd=str(dest))
    subprocess.check_call(["git", "fetch", "-q", "--depth", "1", "origin", sha], cwd=str(dest))
    subprocess.check_call(["git", "checkout", "-q", "FETCH_HEAD"], cwd=str(dest))


def _rel_or_abs(out_dir: Path) -> str:
    try:
        return str(out_dir.relative_to(ROOT))
    except ValueError:
        return str(out_dir)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subjects", type=Path, default=ROOT / "corpus" / "subjects.yaml")
    ap.add_argument("--out", type=Path, default=ROOT / "corpus" / "results")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--no-osv", action="store_true")
    ap.add_argument("--no-tests", action="store_true")
    args = ap.parse_args()

    doc = yaml.safe_load(args.subjects.read_text(encoding="utf-8"))
    policy = load_policy(ROOT / "policy" / "cold_external.yaml")
    summary = []
    env_info = runner_environment()

    for subject in doc["subjects"]:
        sid = subject["id"]
        if args.only and sid not in args.only:
            continue
        sha = subject["commit_sha"]
        url = subject["repo_url"]
        out_dir = args.out / f"{sid}@{sha[:12]}"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)

        print(f"==> {sid}@{sha[:12]}", flush=True)
        tmp = Path(tempfile.mkdtemp(prefix=f"metria-corpus-{sid}-"))
        try:
            checkout(url, sha, tmp)
            run = run_subject(
                tmp,
                repo_url=url,
                policy=policy,
                out_dir=out_dir,
                commit_sha=sha,
                query_osv=not args.no_osv,
                run_tests=not args.no_tests,
            )
            validate_run(run)
            summary.append(
                {
                    "id": sid,
                    "repo_url": url,
                    "commit_sha": sha,
                    "overall": run["overall"],
                    "lenses": {
                        k: {
                            "status": v["status"],
                            "null_display": v.get("null_display"),
                        }
                        for k, v in run["lenses"].items()
                    },
                    "result_dir": _rel_or_abs(out_dir),
                    "runner": {
                        "python": env_info["python"],
                        "platform": env_info["platform"],
                        "runner_image_digest": run.get("runner_image_digest"),
                    },
                }
            )
            print(f"    overall={run['overall']}", flush=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    summary_doc = {
        "runner_profile": env_info,
        "subjects": summary,
    }
    summary_path = args.out / "SUMMARY.json"
    summary_path.write_text(json.dumps(summary_doc, indent=2) + "\n", encoding="utf-8")
    profile_path = args.out / "RUNNER_PROFILE.json"
    profile_path.write_text(
        json.dumps(
            {
                **env_info,
                "note": (
                    "Corpus is reproducible on this runner profile (OS/Python/tools). "
                    "Install: python3 -m pip install -e '.[dev]' && python3 -m pip install ruff"
                ),
                "command": "python3 scripts/run_corpus.py",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {summary_path}")
    print(f"wrote {profile_path}")
    print(f"runner: {platform.platform()} / CPython {env_info['python']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

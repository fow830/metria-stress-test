"""metria CLI — status-first StressRun producer."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from metria import __version__
from metria.runner import load_policy, run_subject
from metria.render import render_report

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "policy" / "cold_external.yaml"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


def _parse_target(target: str) -> Tuple[str, Optional[str], bool]:
    """Return (path_or_url, sha_or_none, is_git_url).

    SHA is taken from a trailing @ only when it matches ^[0-9a-f]{7,40}$.
    Userinfo @ in URLs (e.g. https://oauth2@host/repo) is not a SHA separator (N-4).
    """
    if target.startswith("git@"):
        # git@host:path[@sha] — last @sha only if hex
        if target.count("@") >= 2:
            base, _, maybe = target.rpartition("@")
            if _SHA_RE.match(maybe):
                return base, maybe.lower(), True
        return target, None, True
    if target.startswith("http://") or target.startswith("https://"):
        parsed = urlparse(target)
        # rpartition on full string; accept SHA only when hex-shaped and not userinfo.
        base, sep, maybe = target.rpartition("@")
        if sep and _SHA_RE.match(maybe) and "://" in base:
            # Ensure the @ we split is not the userinfo separator.
            # userinfo form: scheme://userinfo@host/... — after rpartition, base still contains ://
            # and maybe is path-like without scheme. Hex SHA won't equal a hostname/path typically;
            # additionally require no "/" in maybe and that urlparse(base).netloc is non-empty.
            base_parsed = urlparse(base)
            if base_parsed.scheme and base_parsed.netloc and "/" not in maybe:
                return base, maybe.lower(), True
        # Ignore parsed beyond validation that URL is well-formed enough to attempt.
        _ = parsed
        return target, None, True
    path = Path(target).expanduser().resolve()
    return str(path), None, False


def _checkout(url: str, sha: Optional[str], dest: Path) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "init", "-q"], cwd=str(dest))
    subprocess.check_call(["git", "remote", "add", "origin", url], cwd=str(dest))
    if sha:
        subprocess.check_call(["git", "fetch", "-q", "--depth", "1", "origin", sha], cwd=str(dest))
        subprocess.check_call(["git", "checkout", "-q", "FETCH_HEAD"], cwd=str(dest))
        return sha.lower()
    subprocess.check_call(["git", "fetch", "-q", "--depth", "1", "origin", "HEAD"], cwd=str(dest))
    subprocess.check_call(["git", "checkout", "-q", "FETCH_HEAD"], cwd=str(dest))
    return (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(dest), text=True).strip().lower()
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="metria",
        description=(
            "Metria Stress Test — status-first evidence protocol (score always null/uncalibrated). "
            "WARNING: `metria run` may execute subject test code in an isolated scrubbed venv "
            "unless --no-tests is set."
        ),
    )
    p.add_argument("--version", action="version", version=f"metria {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run cold_external stress protocol on a path or git URL@sha")
    run.add_argument("target", help="Local path or https://github.com/org/repo@sha")
    run.add_argument("--sha", help="Commit SHA (for git URLs or to pin a local checkout)", default=None)
    run.add_argument("--repo-url", help="Override subject.repo_url", default=None)
    run.add_argument(
        "--profile",
        default="cold_external",
        help="Policy profile name (default: cold_external)",
    )
    run.add_argument("--policy", type=Path, default=None, help="Path to policy YAML")
    run.add_argument("--out", type=Path, default=Path("metria-out"), help="Output directory")
    run.add_argument("--no-osv", action="store_true", help="Skip OSV network queries")
    run.add_argument(
        "--no-tests",
        action="store_true",
        help="Static test discovery only; do not import or execute subject code",
    )
    run.add_argument("--print-report", action="store_true", help="Print human report to stdout")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd != "run":
        return 2

    policy_path = args.policy or DEFAULT_POLICY
    if not policy_path.is_file():
        print(f"policy not found: {policy_path}", file=sys.stderr)
        return 2
    policy = load_policy(policy_path)
    if args.profile and policy.get("name") and policy["name"] != args.profile:
        print(
            f"warning: --profile {args.profile!r} does not match policy name "
            f"{policy.get('name')!r}; using policy file as-is",
            file=sys.stderr,
        )

    target, embedded_sha, is_url = _parse_target(args.target)
    sha = args.sha or embedded_sha
    tmp = None
    worktree = None
    try:
        if is_url:
            tmp = Path(tempfile.mkdtemp(prefix="metria-subject-"))
            resolved_sha = _checkout(target, sha, tmp)
            root = tmp
            repo_url = args.repo_url or target
        else:
            root = Path(target)
            if not root.is_dir():
                print(f"not a directory: {root}", file=sys.stderr)
                return 2
            if sha:
                # N-3: never git checkout inside the operator's working copy.
                worktree = Path(tempfile.mkdtemp(prefix="metria-worktree-"))
                subprocess.check_call(
                    ["git", "worktree", "add", "--detach", str(worktree), sha],
                    cwd=str(root),
                )
                root = worktree
            repo_url = args.repo_url or target
            resolved_sha = sha

        out_dir = args.out.resolve()
        run = run_subject(
            root,
            repo_url=repo_url,
            policy=policy,
            out_dir=out_dir,
            commit_sha=resolved_sha,
            query_osv=not args.no_osv,
            run_tests=not args.no_tests,
        )
        print(f"wrote {out_dir / 'stress-run.json'}")
        print(f"wrote {out_dir / 'report.txt'}")
        print(f"wrote {out_dir / 'evidence.zip'}")
        print(f"overall={run['overall']}")
        if args.print_report:
            print()
            print(render_report(run), end="")
        return 0 if run["overall"] != "fail" else 1
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        if worktree is not None:
            # Best-effort worktree cleanup from the original target if it is a git repo.
            try:
                origin = Path(target)
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=str(origin),
                    capture_output=True,
                    check=False,
                )
            except Exception:
                pass
            shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

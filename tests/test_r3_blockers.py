"""Regression tests for Claude r3 blockers B1–B3 and listed nits."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from metria.adapters import supply_chain, test_adequacy
from metria.discovery import _dep_name_spec
from metria.runner import load_policy, run_subject
from metria.sandbox import scrub_text_paths
import metria.sandbox as sb


ROOT = Path(__file__).resolve().parents[1]


def _stub_venv_with_pytest(work: Path):
    """Reuse r2 offline-safe stub (system-site + optional host .pth)."""
    from tests.test_r2_blockers import _stub_venv_with_pytest as _stub

    return _stub(work)


def test_r3_b1_scrub_does_not_eat_tmpfoo() -> None:
    a = scrub_text_paths("python /tmp/metria-adeq-8v60pk2h/venv/bin/python", Path("/tmp/subject-root"))
    assert a == "python <metria-tmp>/venv/bin/python" or "<metria-tmp>" in a
    assert "metria-adeq-8v60pk2h" not in a
    b = scrub_text_paths("see /tmpfoo/bar and /tmp/build.log", Path("/x"))
    assert "/tmpfoo/bar" in b
    assert "<metria-tmp>foo" not in b


def test_r3_b2_no_tests_skips_editable_install(tmp_path: Path, monkeypatch) -> None:
    calls = []
    real = sb.run_scrubbed

    def spy(argv, **kw):
        calls.append(list(map(str, argv)))
        return real(argv, **kw)

    monkeypatch.setattr(supply_chain, "run_scrubbed", spy)

    subj = tmp_path / "subj"
    (subj / "tests").mkdir(parents=True)
    (subj / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='s', version='0', install_requires=['requests>=2.0'])\n",
        encoding="utf-8",
    )
    (subj / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    subprocess.check_call(["git", "init", "-q"], cwd=str(subj))
    subprocess.check_call(["git", "config", "user.email", "t@t"], cwd=str(subj))
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=str(subj))
    subprocess.check_call(["git", "add", "."], cwd=str(subj))
    subprocess.check_call(["git", "commit", "-q", "-m", "i"], cwd=str(subj))

    run_subject(
        subj,
        out_dir=tmp_path / "out",
        policy=load_policy(ROOT / "policy" / "cold_external.yaml"),
        repo_url="probe",
        query_osv=True,
        run_tests=False,
    )
    editable = [c for c in calls if "install" in c and "-e" in c]
    assert editable == [], f"unexpected editable installs under --no-tests: {editable}"


def test_r3_b3_third_party_import_in_test_body_is_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(test_adequacy, "create_subject_venv", _stub_venv_with_pytest)
    monkeypatch.setattr(
        test_adequacy,
        "install_subject_test_deps",
        lambda *a, **k: {"installed": ["(stubbed)"], "attempts": [], "ok": True},
    )
    subj = tmp_path / "s"
    (subj / "tests").mkdir(parents=True)
    (subj / "mylib.py").write_text("def ok(): return 1\n", encoding="utf-8")
    (subj / "tests" / "test_a.py").write_text(
        "import mylib\ndef test_ok():\n    assert mylib.ok() == 1\n", encoding="utf-8"
    )
    (subj / "tests" / "test_b.py").write_text(
        "def test_dbm_backend():\n    import _dbm_not_on_this_runner\n", encoding="utf-8"
    )
    r = test_adequacy.run_test_adequacy(subj, tmp_path / "ev", run_tests=True)
    assert r["status"] == "null"
    assert r.get("null_reason") == "test_env_unavailable"
    assert r.get("null_display") == "NOT MEASURED"
    # N-r4-1: prove classification, not a failed stub venv.
    assert r["raw_metrics"].get("classify") == "env"
    assert r["raw_metrics"].get("suite") in {"env_error", "collect_failed"}


def test_r3_n2_wildcard_not_pinned() -> None:
    for s in ["httpcore==1.*", "foo~=2.1", "bar>=1,<2"]:
        d = _dep_name_spec(s)
        assert d is not None
        assert supply_chain._pinned_version(d["specifier"]) is None
    assert supply_chain._pinned_version("==2.6.3") == "2.6.3"

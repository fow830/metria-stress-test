"""Regression tests for Claude r1 blockers B1–B6 and code-fixable nits."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from metria.adapters import structural, supply_chain
from metria.cli import _parse_target
from metria.discovery import inventory_packages, is_test_fixture_path
from metria.evidence import write_evidence_pack
from metria.sandbox import scrub_env


ROOT = Path(__file__).resolve().parents[1]


def _git_init(path: Path) -> None:
    subprocess.check_call(["git", "init", "-q"], cwd=str(path))
    subprocess.check_call(["git", "config", "user.email", "t@example.com"], cwd=str(path))
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=str(path))
    subprocess.check_call(["git", "add", "."], cwd=str(path))
    subprocess.check_call(["git", "commit", "-q", "-m", "i"], cwd=str(path))


def test_b1_no_tests_does_not_import_subject(tmp_path: Path) -> None:
    subj = tmp_path / "subj"
    tests = subj / "tests"
    tests.mkdir(parents=True)
    sink = tmp_path / "leaked.txt"
    (tests / "conftest.py").write_text(
        "import os, pathlib\n"
        f"pathlib.Path({str(sink)!r}).write_text('LEAKED')\n",
        encoding="utf-8",
    )
    (tests / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    _git_init(subj)
    env = {**os.environ, "FAKE_GITHUB_TOKEN": "ghp_should_not_reach", "GH_TOKEN": "x"}
    proc = subprocess.run(
        [sys.executable, "-m", "metria", "run", str(subj), "--no-osv", "--no-tests", "--out", str(tmp_path / "out")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode in {0, 1}
    assert not sink.exists(), "subject conftest must not run under --no-tests"


def test_b1_scrub_env_drops_tokens() -> None:
    os.environ["FAKE_GITHUB_TOKEN"] = "secret"
    os.environ["GH_TOKEN"] = "secret"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "secret"
    try:
        env = scrub_env(home=Path(tempfile.mkdtemp()))
        assert "FAKE_GITHUB_TOKEN" not in env
        assert "GH_TOKEN" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "HOME" in env
        assert "PATH" in env
    finally:
        os.environ.pop("FAKE_GITHUB_TOKEN", None)
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("AWS_SECRET_ACCESS_KEY", None)


def test_b2_osv_unavailable_not_pass(tmp_path: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise urllib.error.URLError("network down (test)")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\nurllib3==1.26.0\n", encoding="utf-8")
    # Without pyproject, root requirements count as runtime.
    r = supply_chain.run_supply_chain(tmp_path, tmp_path / "ev", query_osv=True)
    assert r["status"] == "null"
    assert r["null_display"] == "NOT MEASURED"
    assert r["null_reason"] == "tool_error"
    assert r["raw_metrics"]["osv_errors"] > 0
    assert r["status"] != "pass"


def test_b5_example_requirements_not_runtime(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0"\ndependencies=["requests==2.32.3"]\n',
        encoding="utf-8",
    )
    ex = tmp_path / "examples" / "celery"
    ex.mkdir(parents=True)
    (ex / "requirements.txt").write_text("flask==2.3.2\n", encoding="utf-8")
    pkgs = inventory_packages(tmp_path)
    by_name = {p["name"].lower(): p for p in pkgs}
    assert by_name["requests"]["class"] == "runtime"
    assert by_name["flask"]["class"] == "example"


def test_b5_severity_from_database_specific() -> None:
    from metria.adapters.supply_chain import severity_from_osv

    assert severity_from_osv({"database_specific": {"severity": "CRITICAL"}}) == "critical"
    assert (
        severity_from_osv(
            {"severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]}
        )
        == "critical"
    )
    assert severity_from_osv({"summary": "no sev"}) is None


def test_b6_test_certs_not_hard_gate(tmp_path: Path) -> None:
    certs = tmp_path / "tests" / "certs"
    certs.mkdir(parents=True)
    (certs / "key.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert is_test_fixture_path("tests/certs/key.pem")
    r = structural.run_structural(tmp_path, tmp_path / "ev")
    assert r["hard_gate_failures"] == []
    assert r["status"] != "fail"
    assert any("Test-fixture" in f["title"] or "test" in f["title"].lower() for f in r["findings"])


def test_n1_re_compile_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("import re\nPAT = re.compile(r'^a+$')\n", encoding="utf-8")
    r = structural.run_structural(tmp_path, tmp_path / "ev")
    titles = [f["title"] for f in r["findings"]]
    assert not any("compile()" in t for t in titles)


def test_n4_parse_target_userinfo() -> None:
    url, sha, is_url = _parse_target("https://oauth2@gitlab.example.com/group/repo")
    assert is_url
    assert sha is None
    assert url == "https://oauth2@gitlab.example.com/group/repo"
    url2, sha2, _ = _parse_target(
        "https://github.com/pallets/click@006ae84d651846c944331fff78e340701fe58562"
    )
    assert sha2 == "006ae84d651846c944331fff78e340701fe58562"
    assert url2 == "https://github.com/pallets/click"


def test_n6_evidence_zip_deterministic(tmp_path: Path) -> None:
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "a.txt").write_text("hello\n", encoding="utf-8")
    z1 = tmp_path / "1.zip"
    z2 = tmp_path / "2.zip"
    s1, _ = write_evidence_pack(ev, z1)
    s2, _ = write_evidence_pack(ev, z2)
    assert s1 == s2

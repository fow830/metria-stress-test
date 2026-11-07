"""Regression tests for Claude r2 blockers B1–B2 and code-fixable nits."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from metria.adapters import structural, supply_chain, test_adequacy
from metria.discovery import is_test_fixture_path, parse_setup_py_requires
from metria.sandbox import discover_install_targets, scrub_env, scrub_text_paths


ROOT = Path(__file__).resolve().parents[1]


def test_b2_path_segments_not_substrings() -> None:
    assert is_test_fixture_path("tests/certs/server.key") is True
    assert is_test_fixture_path("test/data/key.pem") is True
    assert is_test_fixture_path("deploy/latest/server.key") is False
    assert is_test_fixture_path("infra/attest/signing.pem") is False
    assert is_test_fixture_path("src/contest/key.pem") is False
    assert is_test_fixture_path("greatest/key.pem") is False


def test_b2_deploy_latest_key_hard_gates(tmp_path: Path) -> None:
    k = tmp_path / "deploy" / "latest"
    k.mkdir(parents=True)
    (k / "server.key").write_text(
        "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    r = structural.run_structural(tmp_path, tmp_path / "ev")
    assert r["status"] == "fail"
    assert r["hard_gate_failures"]
    assert any("deploy/latest" in h for h in r["hard_gate_failures"])


def _stub_venv_with_pytest(work: Path):
    """Offline-safe stub (N-r4-1): system-site-packages + optional host .pth, no pip."""
    import site

    d = work / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(d)],
        check=True,
        capture_output=True,
    )
    py, pip = d / "bin" / "python", d / "bin" / "pip"
    # If host keeps pytest in user-site (macOS), add a venv .pth so PYTHONNOUSERSITE
    # still leaves pytest importable — without opening product scrub_env or hitting PyPI.
    check = subprocess.run(
        [str(py), "-c", "import pytest"],
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "HOME": str(work / "home")},
    )
    if check.returncode != 0:
        host = site.getusersitepackages()
        if isinstance(host, str) and Path(host).is_dir():
            ver = f"{sys.version_info.major}.{sys.version_info.minor}"
            sp = d / "lib" / f"python{ver}" / "site-packages"
            if sp.is_dir():
                (sp / "_metria_host_usersite.pth").write_text(host + "\n", encoding="utf-8")
    return py, pip


def test_b1_broken_subject_import_is_fail(tmp_path: Path, monkeypatch) -> None:
    subj = tmp_path / "s"
    (subj / "pkg").mkdir(parents=True)
    (subj / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (subj / "pkg" / "core.py").write_text("from pkg.helperz import twice\n", encoding="utf-8")
    (subj / "pkg" / "helpers.py").write_text("def twice(x): return 2*x\n", encoding="utf-8")
    (subj / "tests").mkdir()
    (subj / "tests" / "test_core.py").write_text(
        "def test_core():\n    import pkg.core\n    assert pkg.core.twice(2) == 4\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(test_adequacy, "create_subject_venv", _stub_venv_with_pytest)
    monkeypatch.setattr(
        test_adequacy,
        "install_subject_test_deps",
        lambda *a, **k: {"installed": ["(stubbed)"], "attempts": [], "ok": True},
    )
    r = test_adequacy.run_test_adequacy(subj, tmp_path / "ev", run_tests=True)
    assert r["status"] == "fail"
    assert r.get("null_reason") is None


def test_b1_assertion_plus_importerror_text_is_fail(tmp_path: Path, monkeypatch) -> None:
    subj = tmp_path / "s"
    (subj / "tests").mkdir(parents=True)
    (subj / "tests" / "test_a.py").write_text(
        "def test_real_bug():\n    assert 1 + 1 == 3\n", encoding="utf-8"
    )
    (subj / "tests" / "test_b.py").write_text(
        "def test_msg():\n"
        "    raise RuntimeError('ImportError while loading plugin is expected to be handled')\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(test_adequacy, "create_subject_venv", _stub_venv_with_pytest)
    monkeypatch.setattr(
        test_adequacy,
        "install_subject_test_deps",
        lambda *a, **k: {"installed": ["(stubbed)"], "attempts": [], "ok": True},
    )
    r = test_adequacy.run_test_adequacy(subj, tmp_path / "ev", run_tests=True)
    assert r["status"] == "fail"
    assert r.get("null_display") != "NOT MEASURED"


def test_b1_third_party_missing_module_is_env() -> None:
    # Classification helper: missing pytest-httpbin-like third party → env
    out = "ERROR collecting\nModuleNotFoundError: No module named 'pytest_httpbin'\n"
    kind = test_adequacy.classify_pytest_result(out, "", 2, Path("/tmp/nosuch"), phase="collect")
    assert kind == "env"


def test_n_r2_2_install_targets_continue_to_requirements(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0"\n'
        '[project.optional-dependencies]\ndev=["pytest"]\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements-dev.txt").write_text("trustme\n", encoding="utf-8")
    targets = discover_install_targets(tmp_path)
    # Declared extra 'dev' + bare editable + requirements-dev.txt; no phantom [test].
    assert ["-e", ".[test]"] not in targets
    assert ["-e", ".[dev]"] in targets
    assert ["-e", "."] in targets
    assert ["-r", "requirements-dev.txt"] in targets
    # requirements come after editable so install loop can reach them
    assert targets.index(["-r", "requirements-dev.txt"]) > targets.index(["-e", "."])


def test_n_r2_3_scrub_linux_tmp_paths() -> None:
    text = "python /tmp/metria-adeq-8v60pk2h/venv/bin/python -m pytest\n"
    scrubbed = scrub_text_paths(text, Path("/tmp/subject-root"))
    assert "/tmp/metria-adeq-8v60pk2h" not in scrubbed
    assert "metria-adeq" not in scrubbed
    assert "<metria-tmp>" in scrubbed
    # r3 B1: bare /tmp must not rewrite unrelated paths like /tmpfoo
    other = scrub_text_paths("see /tmpfoo/bar and /tmp/build.log", Path("/x"))
    assert "/tmpfoo/bar" in other
    assert "<metria-tmp>foo" not in other


def test_n_r2_5_parse_setup_py_install_requires(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n"
        "setup(\n"
        "  name='click',\n"
        "  install_requires=[\n"
        "    \"colorama; platform_system == 'Windows'\",\n"
        "    \"importlib-metadata; python_version < '3.8'\",\n"
        "  ],\n"
        ")\n",
        encoding="utf-8",
    )
    pkgs, err = parse_setup_py_requires(tmp_path / "setup.py")
    assert err is None
    names = {p["name"] for p in pkgs}
    assert "colorama" in names
    assert "importlib-metadata" in names


def test_n_r2_1_pythonpath_not_inherited() -> None:
    import os

    os.environ["PYTHONPATH"] = "/operator/injected/path"
    try:
        env = scrub_env(home=Path(tempfile.mkdtemp()))
        assert "PYTHONPATH" not in env
        assert "PYTHONHOME" not in env
        assert "PYTHONUSERBASE" not in env
        assert env.get("PYTHONNOUSERSITE") == "1"
    finally:
        os.environ.pop("PYTHONPATH", None)


def test_n_r2_4_unresolved_not_unpinned_warn(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0"\ndependencies=["requests>=2.0"]\n',
        encoding="utf-8",
    )

    def fake_resolve(root, runtime, allow_subject_install=True):
        return {}, {"method": "unresolved", "errors": ["forced"]}

    monkeypatch.setattr(supply_chain, "resolve_runtime_versions", fake_resolve)

    def boom(*a, **k):
        return [], None

    monkeypatch.setattr(supply_chain, "_osv_query", boom)
    r = supply_chain.run_supply_chain(tmp_path, tmp_path / "ev", query_osv=True)
    titles = [f["title"] for f in r["findings"]]
    assert not any("Unpinned runtime dependency" in t for t in titles)
    assert any("adapter_gap" in t for t in titles)

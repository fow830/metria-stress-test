"""Regression tests for Claude r2 blockers B1–B2 and listed nits."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import metria.adapters.test_adequacy as ta
from metria.adapters import structural, supply_chain
from metria.discovery import (
    inventory_packages,
    is_test_fixture_path,
    parse_setup_py_requires,
)
from metria.sandbox import (
    discover_install_targets,
    scrub_env,
    scrub_text_paths,
)


ROOT = Path(__file__).resolve().parents[1]


def _stub_venv_system_site(monkeypatch):
    def venv_sys(work: Path):
        d = work / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(d)],
            check=True,
            capture_output=True,
            text=True,
        )
        py = d / "bin" / "python"
        pip = d / "bin" / "pip"
        # User-site pytest is invisible to --system-site-packages; seed the venv.
        subprocess.run(
            [str(pip), "install", "-q", "pytest"],
            check=True,
            capture_output=True,
            text=True,
        )
        return py, pip

    monkeypatch.setattr(ta, "create_subject_venv", venv_sys)
    monkeypatch.setattr(
        ta,
        "install_subject_test_deps",
        lambda *a, **k: {"installed": ["(stubbed)"], "attempts": [], "ok": True},
    )


def test_b1_broken_internal_import_is_fail(tmp_path: Path, monkeypatch) -> None:
    _stub_venv_system_site(monkeypatch)
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
    r = ta.run_test_adequacy(subj, tmp_path / "ev", run_tests=True)
    assert r["status"] == "fail", r
    assert r.get("null_reason") is None


def test_b1_assertion_plus_importerror_text_is_fail(tmp_path: Path, monkeypatch) -> None:
    _stub_venv_system_site(monkeypatch)
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
    r = ta.run_test_adequacy(subj, tmp_path / "ev", run_tests=True)
    assert r["status"] == "fail", r
    assert r.get("null_display") != "NOT MEASURED"


def test_b1_external_missing_module_still_not_measured(tmp_path: Path, monkeypatch) -> None:
    _stub_venv_system_site(monkeypatch)
    subj = tmp_path / "s"
    (subj / "tests").mkdir(parents=True)
    (subj / "tests" / "test_x.py").write_text(
        "def test_x():\n    import totally_missing_ext_dep_xyz\n    assert True\n",
        encoding="utf-8",
    )
    r = ta.run_test_adequacy(subj, tmp_path / "ev", run_tests=True)
    assert r["status"] == "null"
    assert r.get("null_reason") == "test_env_unavailable"


def test_b2_path_segments_not_substring() -> None:
    assert is_test_fixture_path("tests/certs/server.key") is True
    assert is_test_fixture_path("deploy/latest/server.key") is False
    assert is_test_fixture_path("infra/attest/signing.pem") is False
    assert is_test_fixture_path("src/contest/key.pem") is False


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


def test_nr2_2_missing_extra_does_not_block_requirements(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup(name='x')\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("pytest-httpbin\n", encoding="utf-8")
    targets = discover_install_targets(tmp_path)
    # No undeclared .[test] — only -e . then -r requirements-dev.txt
    assert ["-e", ".[test]"] not in targets
    assert ["-e", "."] in targets
    assert ["-r", "requirements-dev.txt"] in targets
    # requirements come after editable
    assert targets.index(["-r", "requirements-dev.txt"]) > targets.index(["-e", "."])


def test_nr2_3_linux_tmp_paths_scrubbed() -> None:
    text = "/tmp/metria-adeq-8v60pk2h/venv/bin/python -m pytest\n"
    out = scrub_text_paths(text, Path("/tmp/subject-xyz"))
    assert "/tmp/metria-adeq-8v60pk2h" not in out
    assert "<metria-tmp>" in out


def test_nr2_4_unpinned_is_adapter_gap_not_warn(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0"\ndependencies=["requests>=2.0"]\n',
        encoding="utf-8",
    )

    def no_resolve(root, runtime):
        return {}, {"resolved": False, "attempted": True, "error": "offline"}

    monkeypatch.setattr(supply_chain, "_resolve_runtime_versions", no_resolve)
    r = supply_chain.run_supply_chain(tmp_path, tmp_path / "ev", query_osv=True)
    assert r["status"] == "pass", r
    assert any("adapter_gap" in f["fingerprint"] for f in r["findings"])
    assert not any("Unpinned runtime dependency" in f["title"] for f in r["findings"])


def test_nr2_5_setup_py_parsed() -> None:
    text = (
        "from setuptools import setup\n"
        "setup(name='click', install_requires=[\n"
        "    \"colorama; platform_system == 'Windows'\",\n"
        "    'importlib-metadata; python_version < \"3.8\"',\n"
        "])\n"
    )
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "setup.py"
        p.write_text(text, encoding="utf-8")
        pkgs, err = parse_setup_py_requires(p)
        assert err is None
        names = {x["name"] for x in pkgs}
        assert "colorama" in names
        assert "importlib-metadata" in names


def test_nr2_5_unparsed_setup_py_not_components_zero(tmp_path: Path) -> None:
    # setup() built dynamically — static scrape fails → adapter_gap, not "no deps"
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n"
        "reqs = []\n"
        "setup(name='x', install_requires=reqs)\n",
        encoding="utf-8",
    )
    pkgs = inventory_packages(tmp_path)
    assert any(p.get("class") == "adapter_gap" for p in pkgs)
    r = supply_chain.run_supply_chain(tmp_path, tmp_path / "ev", query_osv=False)
    assert r["status"] == "null"
    assert r.get("null_reason") == "adapter_gap"
    assert not any("components=0" in f.get("title", "") for f in r.get("findings") or [])


def test_pythonpath_not_inherited() -> None:
    os.environ["PYTHONPATH"] = "/operator/injected/path"
    try:
        env = scrub_env(home=Path(tempfile.mkdtemp()))
        assert "PYTHONPATH" not in env
        assert "PYTHONHOME" not in env
    finally:
        os.environ.pop("PYTHONPATH", None)

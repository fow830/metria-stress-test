"""Regression tests for Claude r4 blocker B1 and listed nits."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from metria.adapters import supply_chain, test_adequacy
from metria.discovery import is_test_fixture_path, is_test_path


ROOT = Path(__file__).resolve().parents[1]


def _stub_venv_system_site(work: Path):
    """Reuse r2 offline-safe stub (system-site + optional host .pth)."""
    from tests.test_r2_blockers import _stub_venv_with_pytest as _stub

    return _stub(work)


def test_r4_b1_src_layout_dist_name_mismatch_is_fail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(test_adequacy, "create_subject_venv", _stub_venv_system_site)
    monkeypatch.setattr(
        test_adequacy,
        "install_subject_test_deps",
        lambda *a, **k: {"installed": ["(stubbed)"], "attempts": [], "ok": True},
    )
    subj = tmp_path / "s"
    (subj / "src" / "imgkit").mkdir(parents=True)
    (subj / "src" / "imgkit" / "__init__.py").write_text("", encoding="utf-8")
    (subj / "src" / "imgkit" / "core.py").write_text(
        "from imgkit.helperz import twice\n", encoding="utf-8"
    )
    (subj / "src" / "imgkit" / "helpers.py").write_text(
        "def twice(x): return 2*x\n", encoding="utf-8"
    )
    (subj / "tests").mkdir()
    (subj / "tests" / "conftest.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))\n",
        encoding="utf-8",
    )
    (subj / "tests" / "test_core.py").write_text(
        "def test_core():\n    import imgkit.core\n    assert imgkit.core.twice(2) == 4\n",
        encoding="utf-8",
    )
    (subj / "pyproject.toml").write_text(
        '[project]\nname = "Imaging-Kit"\nversion = "0"\n', encoding="utf-8"
    )
    names = test_adequacy._subject_module_names(subj)
    assert "imgkit" in names
    r = test_adequacy.run_test_adequacy(subj, tmp_path / "ev", run_tests=True)
    assert r["status"] == "fail"
    assert r.get("null_reason") is None


def test_r4_n1_test_file_predicate() -> None:
    assert is_test_path("test_six.py") is True
    assert is_test_fixture_path("test_six.py") is True
    assert is_test_fixture_path("deploy/latest/server.key") is False


def test_r4_n3_marker_excluded_not_queried(tmp_path: Path, monkeypatch) -> None:
    queried = []

    def fake_osv(name, version, timeout=12.0):
        queried.append(f"{name}=={version}")
        return [], None

    monkeypatch.setattr(supply_chain, "_osv_query", fake_osv)
    monkeypatch.setattr(
        supply_chain, "pip_list_versions", lambda *a, **k: {"werkzeug": "3.1.9"}
    )

    class P:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(supply_chain, "run_scrubbed", lambda *a, **k: P())
    monkeypatch.setattr(
        supply_chain,
        "create_subject_venv",
        lambda work: (work / "venv" / "bin" / "python", work / "venv" / "bin" / "pip"),
    )
    # create_subject_venv is called — make dirs so Path exists
    real_create = supply_chain.create_subject_venv

    def fake_create(work):
        d = work / "venv" / "bin"
        d.mkdir(parents=True, exist_ok=True)
        (d / "python").write_text("", encoding="utf-8")
        (d / "pip").write_text("", encoding="utf-8")
        return d / "python", d / "pip"

    monkeypatch.setattr(supply_chain, "create_subject_venv", fake_create)

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0"\n'
        'dependencies=["werkzeug>=3.0.0", '
        "\"importlib-metadata>=3.6.0; python_version < '3.10'\"]\n",
        encoding="utf-8",
    )
    r = supply_chain.run_supply_chain(
        tmp_path, tmp_path / "ev", query_osv=True, allow_subject_install=True
    )
    meta = json.loads((tmp_path / "ev" / "resolved_versions.json").read_text())["meta"]
    # Never invent a lower-bound version for OSV (r4 N-3).
    assert all("importlib-metadata" not in q for q in queried)
    assert "range_fallback" not in meta or not meta.get("range_fallback")
    # On runners where the marker excludes the dep (e.g. 3.10+), it is not_applicable.
    # On 3.9 the marker applies but the package is simply unresolved (not in pip list).
    resolved = json.loads((tmp_path / "ev" / "resolved_versions.json").read_text())["resolved"]
    assert "importlib-metadata" not in resolved
    assert "werkzeug" in resolved
    _ = r
    _ = real_create


def test_r4_n2_zero_osv_only_gaps_is_null(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supply_chain, "_osv_query", lambda *a, **k: ([], None))
    monkeypatch.setattr(supply_chain, "pip_list_versions", lambda *a, **k: {})

    def fake_create(work: Path):
        d = work / "venv" / "bin"
        d.mkdir(parents=True, exist_ok=True)
        (d / "python").write_text("", encoding="utf-8")
        (d / "pip").write_text("", encoding="utf-8")
        return d / "python", d / "pip"

    class P:
        returncode = 1
        stdout = ""
        stderr = "ERROR: file:///tmp/metria-corpus-httpx-nazkfegf is not a Python project\n"

    monkeypatch.setattr(supply_chain, "create_subject_venv", fake_create)
    monkeypatch.setattr(supply_chain, "run_scrubbed", lambda *a, **k: P())

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0"\ndependencies=["httpcore>=1.0"]\n',
        encoding="utf-8",
    )
    r = supply_chain.run_supply_chain(
        tmp_path, tmp_path / "ev", query_osv=True, allow_subject_install=True
    )
    assert r["status"] == "null"
    assert r.get("null_reason") == "adapter_gap"
    meta = json.loads((tmp_path / "ev" / "resolved_versions.json").read_text())["meta"]
    blob = "\n".join(meta.get("errors") or [])
    assert "/tmp/metria-corpus" not in blob
    assert "metria-corpus-httpx" not in blob


def test_r4_n4_scrub_file_uri_corpus_tmp() -> None:
    from metria.sandbox import scrub_text_paths

    raw = "ERROR: file:///tmp/metria-corpus-httpx-nazkfegf does not appear to be a Python project"
    out = scrub_text_paths(raw, Path("/tmp/other-subject"))
    assert "metria-corpus-httpx" not in out
    assert "/tmp/metria-" not in out
    assert "<metria-tmp>" in out
    assert "/tmpfoo" in scrub_text_paths("keep /tmpfoo/bar", Path("/x"))

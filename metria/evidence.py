"""Evidence pack writer — zip + MANIFEST.sha256."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Dict, Tuple


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_evidence_pack(evidence_root: Path, zip_path: Path) -> Tuple[str, str]:
    """Zip evidence_root → zip_path. Returns (zip_sha256, manifest_sha256)."""
    evidence_root.mkdir(parents=True, exist_ok=True)
    entries: Dict[str, str] = {}
    for path in sorted(evidence_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "MANIFEST.sha256":
            continue
        rel = str(path.relative_to(evidence_root)).replace("\\", "/")
        entries[rel] = _file_sha256(path)

    manifest_lines = [f"{digest}  {name}" for name, digest in sorted(entries.items())]
    manifest_text = "\n".join(manifest_lines) + ("\n" if manifest_lines else "")
    manifest_path = evidence_root / "MANIFEST.sha256"
    manifest_path.write_text(manifest_text, encoding="utf-8")
    manifest_sha = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(evidence_root.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=str(path.relative_to(evidence_root)).replace("\\", "/"))
    return _file_sha256(zip_path), manifest_sha


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n", encoding="utf-8")

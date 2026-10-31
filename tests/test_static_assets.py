"""Tests for the vendored frontend assets: manifest check, update step and serving (#115)."""

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = REPO_ROOT / "src" / "resilient_triage" / "api" / "static" / "vendor"


def _load_vendor_script() -> ModuleType:
    script = REPO_ROOT / "scripts" / "vendor.py"
    spec = importlib.util.spec_from_file_location("vendor_script", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vendor = _load_vendor_script()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_tree(root: Path, files: dict[str, bytes], manifest: dict) -> Path:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _small_tree(root: Path) -> Path:
    """A one-package vendor tree whose manifest matches its files."""
    lib = b"export const x = 1;\n"
    license_text = b"MIT License\n"
    manifest = {
        "packages": [
            {
                "name": "lib",
                "version": "1.0.0",
                "license": "MIT",
                "license_file": "LICENSE",
                "files": [
                    {"path": "LICENSE", "sha256": _sha256(license_text)},
                    {"path": "dist/lib.mjs", "sha256": _sha256(lib), "specifier": "lib"},
                ],
            }
        ]
    }
    files = {"lib@1.0.0/LICENSE": license_text, "lib@1.0.0/dist/lib.mjs": lib}
    return _write_tree(root, files, manifest)


def test_verify_accepts_a_matching_tree(tmp_path: Path):
    """A tree whose files match the manifest has no problems."""
    assert vendor.verify(_small_tree(tmp_path)) == []


def test_verify_reports_a_changed_file(tmp_path: Path):
    """A file whose bytes no longer match its sha256 is reported."""
    root = _small_tree(tmp_path)
    (root / "lib@1.0.0/dist/lib.mjs").write_bytes(b"export const x = 2;\n")
    problems = vendor.verify(root)
    assert len(problems) == 1
    assert "lib@1.0.0/dist/lib.mjs" in problems[0] and "sha256" in problems[0]


def test_verify_reports_a_missing_file(tmp_path: Path):
    """A listed file that is absent is reported."""
    root = _small_tree(tmp_path)
    (root / "lib@1.0.0/LICENSE").unlink()
    problems = vendor.verify(root)
    assert len(problems) == 1
    assert "lib@1.0.0/LICENSE" in problems[0] and "missing" in problems[0]


def test_verify_reports_a_package_without_its_license(tmp_path: Path):
    """A package whose license file is not among its listed files is reported."""
    root = _small_tree(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["packages"][0]["files"] = manifest["packages"][0]["files"][1:]
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "lib@1.0.0/LICENSE").unlink()
    problems = vendor.verify(root)
    assert len(problems) == 1
    assert "lib@1.0.0" in problems[0] and "LICENSE" in problems[0]


def test_verify_reports_an_unlisted_file(tmp_path: Path):
    """A file under vendor/ that no package lists is reported, so nothing ships unverified."""
    root = _small_tree(tmp_path)
    (root / "lib@1.0.0/dist/standalone.mjs").write_bytes(b"// a second copy\n")
    problems = vendor.verify(root)
    assert len(problems) == 1
    assert "lib@1.0.0/dist/standalone.mjs" in problems[0] and "not listed" in problems[0]


def test_verify_reports_a_path_outside_the_package(tmp_path: Path):
    """A manifest path that climbs out of its package directory is rejected."""
    root = _small_tree(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["packages"][0]["files"].append({"path": "../escape.mjs", "sha256": "0" * 64})
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    problems = vendor.verify(root)
    assert any("../escape.mjs" in p and "unsafe" in p for p in problems)


def test_verify_reports_a_duplicate_specifier(tmp_path: Path):
    """Two files claiming the same bare import specifier are reported."""
    root = _small_tree(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["packages"][0]["files"][0]["specifier"] = "lib"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    problems = vendor.verify(root)
    assert len(problems) == 1
    assert "'lib'" in problems[0]


def test_verify_cli_exits_nonzero_on_a_problem(tmp_path: Path, capsys: pytest.CaptureFixture):
    """`vendor.py verify --dir` prints each problem and exits 1."""
    root = _small_tree(tmp_path)
    shutil.rmtree(root / "lib@1.0.0" / "dist")
    assert vendor.main(["verify", "--dir", str(root)]) == 1
    assert "lib@1.0.0/dist/lib.mjs" in capsys.readouterr().out
    assert vendor.main(["verify", "--dir", str(_small_tree(tmp_path / "ok"))]) == 0

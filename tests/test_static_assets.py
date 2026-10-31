"""Tests for the vendored frontend assets: manifest check, update step and serving (#115)."""

import base64
import hashlib
import importlib.util
import io
import json
import shutil
import tarfile
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


def _tarball(members: dict[str, bytes]) -> bytes:
    """Build an npm-style .tgz holding `members` (paths under `package/`)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


class FakeRegistry:
    """Serves npm version metadata and tarballs from memory, so `update` runs offline."""

    def __init__(self) -> None:
        self.tarballs: dict[tuple[str, str], bytes] = {}
        self.integrity: dict[tuple[str, str], str] = {}

    @staticmethod
    def tarball_url(name: str, version: str) -> str:
        return f"https://registry.example/{name}/-/{name}-{version}.tgz"

    def publish(self, name: str, version: str, members: dict[str, bytes]) -> None:
        self.tarballs[name, version] = _tarball(members)
        self.integrity[name, version] = _integrity(self.tarballs[name, version])

    def fetch(self, url: str) -> bytes:
        for (name, version), data in self.tarballs.items():
            if url == f"{vendor.REGISTRY}/{name}/{version}":
                dist = {
                    "tarball": self.tarball_url(name, version),
                    "integrity": self.integrity[name, version],
                }
                return json.dumps({"name": name, "version": version, "dist": dist}).encode()
            if url == self.tarball_url(name, version):
                return data
        raise AssertionError(f"unexpected fetch {url}")


def _unpinned_manifest(root: Path) -> Path:
    """A manifest naming the files to take from `lib` 1.0.0, with no hashes recorded yet."""
    manifest = {
        "packages": [
            {
                "name": "lib",
                "version": "1.0.0",
                "license": "MIT",
                "license_file": "LICENSE",
                "files": [{"path": "LICENSE"}, {"path": "dist/lib.mjs", "specifier": "lib"}],
            }
        ]
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


LIB_MEMBERS = {
    "package/LICENSE": b"MIT License\n",
    "package/dist/lib.mjs": b"export const x = 1;\n",
    "package/dist/lib.standalone.mjs": b"// not listed, never extracted\n",
}


def test_update_vendors_the_listed_files_and_pins_them(tmp_path: Path):
    """Update extracts only the listed files and records tarball, integrity and sha256."""
    registry = FakeRegistry()
    registry.publish("lib", "1.0.0", LIB_MEMBERS)
    root = _unpinned_manifest(tmp_path)

    assert vendor.update(root, registry.fetch) == []

    assert (root / "lib@1.0.0/dist/lib.mjs").read_bytes() == b"export const x = 1;\n"
    assert (root / "lib@1.0.0/LICENSE").read_bytes() == b"MIT License\n"
    assert not (root / "lib@1.0.0/dist/lib.standalone.mjs").exists()
    package = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["packages"][0]
    assert package["tarball"] == "https://registry.example/lib/-/lib-1.0.0.tgz"
    assert package["integrity"] == registry.integrity["lib", "1.0.0"]
    assert package["files"][1] == {
        "path": "dist/lib.mjs",
        "specifier": "lib",
        "sha256": "b40dedde60828bf61d1fadbfc3bb7ea2e0421e9511d22f1b5fb44ae5ba07dbb3",
    }
    assert vendor.verify(root) == []


def test_update_rejects_a_tarball_that_fails_its_integrity(tmp_path: Path):
    """A tarball whose sha512 differs from the registry's integrity aborts with nothing written."""
    registry = FakeRegistry()
    registry.publish("lib", "1.0.0", LIB_MEMBERS)
    registry.integrity["lib", "1.0.0"] = _integrity(b"something else")
    root = _unpinned_manifest(tmp_path)
    before = (root / "manifest.json").read_bytes()

    with pytest.raises(vendor.VendorError, match="integrity"):
        vendor.update(root, registry.fetch)

    assert (root / "manifest.json").read_bytes() == before
    assert not (root / "lib@1.0.0").exists()


def test_update_rejects_a_republished_version(tmp_path: Path):
    """The same version served with a different integrity than recorded is refused."""
    registry = FakeRegistry()
    registry.publish("lib", "1.0.0", LIB_MEMBERS)
    root = _unpinned_manifest(tmp_path)
    vendor.update(root, registry.fetch)
    registry.publish("lib", "1.0.0", {**LIB_MEMBERS, "package/dist/lib.mjs": b"evil();\n"})

    with pytest.raises(vendor.VendorError, match="lib@1.0.0"):
        vendor.update(root, registry.fetch)

    assert (root / "lib@1.0.0/dist/lib.mjs").read_bytes() == b"export const x = 1;\n"


def test_update_rejects_a_listed_file_missing_from_the_tarball(tmp_path: Path):
    """A listed path that the tarball lacks aborts the update."""
    registry = FakeRegistry()
    registry.publish("lib", "1.0.0", {"package/LICENSE": b"MIT License\n"})
    root = _unpinned_manifest(tmp_path)

    with pytest.raises(vendor.VendorError, match="dist/lib.mjs"):
        vendor.update(root, registry.fetch)


def test_update_after_a_version_bump_replaces_the_old_directory(tmp_path: Path):
    """Bumping `version` and updating leaves only the new version's files under vendor/."""
    registry = FakeRegistry()
    registry.publish("lib", "1.0.0", LIB_MEMBERS)
    bumped = {**LIB_MEMBERS, "package/dist/lib.mjs": b"export const x = 11;\n"}
    registry.publish("lib", "1.1.0", bumped)
    root = _unpinned_manifest(tmp_path)
    vendor.update(root, registry.fetch)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["packages"][0]["version"] = "1.1.0"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert vendor.update(root, registry.fetch) == []

    assert not (root / "lib@1.0.0").exists()
    assert (root / "lib@1.1.0/dist/lib.mjs").read_bytes() == b"export const x = 11;\n"

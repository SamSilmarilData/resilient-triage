"""Tests for the vendored frontend assets: manifest check, update step and serving (#115)."""

import base64
import glob
import hashlib
import importlib.resources
import importlib.util
import io
import json
import mimetypes
import shutil
import tarfile
import tomllib
from pathlib import Path, PurePosixPath
from types import ModuleType

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

from resilient_triage.api.static_assets import StaticAssets

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


def test_verify_reports_a_package_name_that_escapes_vendor(tmp_path: Path):
    """A package name or version that would put its directory outside vendor/ is rejected."""
    root = _small_tree(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["packages"][0]["name"] = "../lib"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    problems = vendor.verify(root)
    assert any("../lib@1.0.0" in p and "unsafe" in p for p in problems)


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


def test_committed_vendor_tree_matches_its_manifest():
    """Every vendored file matches its manifest sha256 and every package ships its LICENSE."""
    assert vendor.verify(VENDOR_DIR) == []


def test_manifest_pins_the_approved_packages():
    """The manifest pins exactly the packages and versions approved in #94 (ADR 0010)."""
    manifest = json.loads((VENDOR_DIR / "manifest.json").read_text(encoding="utf-8"))
    pinned = {(p["name"], p["version"], p["license"]) for p in manifest["packages"]}
    assert pinned == {
        ("preact", "10.29.8", "MIT"),
        ("htm", "3.1.1", "Apache-2.0"),
        ("@preact/signals", "2.11.4", "MIT"),
        ("@preact/signals-core", "1.14.5", "MIT"),
        ("@fontsource-variable/inter", "5.3.0", "OFL-1.1"),
        ("@fontsource-variable/jetbrains-mono", "5.3.0", "OFL-1.1"),
    }
    specifiers = {
        f["specifier"]: f"{p['name']}@{p['version']}/{f['path']}"
        for p in manifest["packages"]
        for f in p["files"]
        if "specifier" in f
    }
    assert specifiers == {
        "preact": "preact@10.29.8/dist/preact.mjs",
        "preact/hooks": "preact@10.29.8/hooks/dist/hooks.mjs",
        "htm": "htm@3.1.1/dist/htm.mjs",
        "htm/preact": "htm@3.1.1/preact/index.mjs",
        "@preact/signals": "@preact/signals@2.11.4/dist/signals.mjs",
        "@preact/signals-core": "@preact/signals-core@1.14.5/dist/signals-core.mjs",
    }


IMMUTABLE = "public, max-age=31536000, immutable"
# Independent of the code under test: what a browser needs for each vendored file kind.
EXPECTED_CONTENT_TYPE = {
    ".mjs": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    "": "text/plain; charset=utf-8",  # LICENSE
}


def _vendored_paths() -> list[str]:
    manifest = json.loads((VENDOR_DIR / "manifest.json").read_text(encoding="utf-8"))
    return [
        f"{p['name']}@{p['version']}/{f['path']}" for p in manifest["packages"] for f in p["files"]
    ]


@pytest.fixture
def host_without_mime_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `mimetypes` know only Python's built-in table, like python:3.12-slim with no
    /etc/mime.types (which has no `.woff2` entry)."""
    monkeypatch.setattr(mimetypes, "_db", mimetypes.MimeTypes())


@pytest.mark.usefixtures("host_without_mime_database")
@pytest.mark.parametrize("rel", _vendored_paths())
async def test_vendored_file_is_served_immutable_with_its_type(client: httpx.AsyncClient, rel: str):
    """Each vendored file is served byte for byte, typed for the browser, cached for a year."""
    response = await client.get(f"/static/vendor/{rel}")

    assert response.status_code == 200
    assert response.content == (VENDOR_DIR / rel).read_bytes()
    assert response.headers["content-type"] == EXPECTED_CONTENT_TYPE[PurePosixPath(rel).suffix]
    assert response.headers["cache-control"] == IMMUTABLE


async def test_manifest_is_served(client: httpx.AsyncClient):
    """The manifest itself is reachable, as JSON, so a deployment can be checked over HTTP."""
    response = await client.get("/static/vendor/manifest.json")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json()["packages"][0]["name"] == "preact"


async def test_revalidation_keeps_the_cache_header(client: httpx.AsyncClient):
    """A conditional request gets 304 and still carries the immutable Cache-Control."""
    path = "/static/vendor/preact@10.29.8/dist/preact.mjs"
    etag = (await client.get(path)).headers["etag"]

    response = await client.get(path, headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.headers["cache-control"] == IMMUTABLE


async def test_head_request_carries_the_same_headers(client: httpx.AsyncClient):
    """HEAD answers with the GET headers and no body."""
    response = await client.head("/static/vendor/htm@3.1.1/dist/htm.mjs")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    assert response.headers["cache-control"] == IMMUTABLE


@pytest.mark.parametrize(
    "path",
    [
        "/static/vendor/preact@10.29.8/dist/missing.mjs",
        "/static/vendor/..%2f..%2fapp.py",
        "/static/../app.py",
    ],
)
async def test_unknown_or_escaping_path_is_404(client: httpx.AsyncClient, path: str):
    """A missing file or a path that climbs out of static/ is a 404, never a 500."""
    response = await client.get(path)

    assert response.status_code == 404


async def test_unversioned_static_file_is_revalidated(tmp_path: Path):
    """Files outside vendor/ have no version in their path, so browsers must revalidate them."""
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib@1.0.0.mjs").write_bytes(b"export {};\n")
    (tmp_path / "app.mjs").write_bytes(b"export {};\n")
    app = Starlette(routes=[Mount("/static", StaticAssets(directory=tmp_path))])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        page_module = await ac.get("/static/app.mjs")
        vendored = await ac.get("/static/vendor/lib@1.0.0.mjs")

    assert page_module.headers["cache-control"] == "no-cache"
    assert page_module.headers["content-type"] == "text/javascript; charset=utf-8"
    assert vendored.headers["cache-control"] == IMMUTABLE


def test_package_data_ships_every_static_file():
    """pyproject's package-data globs (as setuptools expands them) cover every file in static/."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"]["resilient_triage.api"]
    api_dir = VENDOR_DIR.parents[1]
    shipped = {
        Path(match).as_posix()
        for pattern in patterns
        for match in glob.glob(pattern, root_dir=api_dir, recursive=True)
        if (api_dir / match).is_file()
    }
    on_disk = {
        p.relative_to(api_dir).as_posix() for p in (api_dir / "static").rglob("*") if p.is_file()
    }

    assert on_disk
    assert on_disk <= shipped


def test_installed_package_contains_the_vendored_files():
    """The importable package carries static/vendor/manifest.json and every file it lists."""
    installed = importlib.resources.files("resilient_triage.api") / "static/vendor/manifest.json"

    assert installed.is_file()
    assert vendor.verify(vendor.installed_vendor_dir()) == []

"""Tests for the page's static files: vendored assets (#115) and the page shell (#116)."""

import base64
import glob
import hashlib
import importlib.resources
import importlib.util
import io
import json
import math
import mimetypes
import re
import shutil
import tarfile
import tomllib
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from types import ModuleType
from urllib.parse import urljoin

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


# --- The page shell (#116) ---

STATIC_DIR = VENDOR_DIR.parent


class _PageParser(HTMLParser):
    """Collects every start tag with its attributes, and each <script>'s attributes and text."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self._script: dict[str, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._script, self._text = dict(attrs), []

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.scripts.append((self._script, "".join(self._text)))
            self._script = None


def _page() -> _PageParser:
    parser = _PageParser()
    parser.feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    parser.close()
    return parser


async def test_root_serves_the_page_shell(client: httpx.AsyncClient):
    """`/` answers with static/index.html, revalidated on every visit like unversioned files."""
    response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.content == (STATIC_DIR / "index.html").read_bytes()


def test_page_shell_has_landmarks_sections_and_a_noscript_note():
    """The static HTML has the landmarks, the four sections and a note for visitors with no JS."""
    page = _page()
    tags = [tag for tag, _ in page.tags]
    ids = {attrs.get("id") for _, attrs in page.tags}

    assert page.tags[0] == ("html", {"lang": "en"})
    assert tags.count("main") == 1 and tags.count("h1") == 1
    assert {"header", "nav", "footer", "noscript"} <= set(tags)
    assert {"hook", "guided", "free-play", "how-its-built"} <= ids
    assert ("link", {"rel": "stylesheet", "href": "/static/styles.css"}) in page.tags
    assert ({"type": "module", "src": "/static/app.mjs"}, "") in page.scripts


def test_page_declares_a_shipped_icon():
    """A declared icon keeps the browser from requesting /favicon.ico, which would 404."""
    icons = [attrs for tag, attrs in _page().tags if tag == "link" and attrs.get("rel") == "icon"]

    assert len(icons) == 1
    assert (STATIC_DIR / str(icons[0]["href"]).removeprefix("/static/")).is_file()


def test_old_console_template_is_gone():
    """The Tailwind-CDN console and its package-data entry went with the new page."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert not (STATIC_DIR.parent / "templates").exists()
    assert pyproject["tool"]["setuptools"]["package-data"]["resilient_triage.api"] == ["static/**"]


# --- Import map and module closure (#116, ADR 0010) ---

# A module specifier in `… from "x"` (import or re-export, minified `from"x"` included),
# a side-effect `import "x"`, or a dynamic `import("x")`.
_SPECIFIER = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s*)(["'])([^"'\n]+)\1""")


def module_specifiers(source: str) -> list[str]:
    """Every module specifier a JS module's static, re-export or dynamic imports name."""
    return [match.group(2) for match in _SPECIFIER.finditer(source)]


def test_module_specifiers_reads_every_import_shape():
    """The scanner sees minified, re-export, side-effect and dynamic imports, and nothing else."""
    source = "\n".join(
        [
            'import{options as n}from"preact";',
            "import e from'htm';",
            'export{h,render,Component}from"preact/hooks";',
            'export*from"@preact/signals-core";',
            'export * as ns from "./ns.mjs";',
            "import {\n  a,\n  b,\n} from './multi.mjs';",
            'import"./side.mjs";',
            "import './side2.mjs';",
            'const lazy = import("./lazy.mjs");',
            "const lazier = import ( 'lazier' );",
            "const here = import.meta.url;",
            "const parts = Array.from(items); const from = 1;",
        ]
    )

    assert module_specifiers(source) == [
        "preact",
        "htm",
        "preact/hooks",
        "@preact/signals-core",
        "./ns.mjs",
        "./multi.mjs",
        "./side.mjs",
        "./side2.mjs",
        "./lazy.mjs",
        "lazier",
    ]


def _manifest() -> dict:
    return json.loads((VENDOR_DIR / "manifest.json").read_text(encoding="utf-8"))


def _import_map() -> dict:
    maps = [text for attrs, text in _page().scripts if attrs.get("type") == "importmap"]
    assert len(maps) == 1, "the page has exactly one import map"
    return json.loads(maps[0])


def test_import_map_matches_the_manifest():
    """Each bare name in the manifest maps to its one same-origin vendored URL, and no more."""
    expected = {
        entry["specifier"]: f"/static/vendor/{p['name']}@{p['version']}/{entry['path']}"
        for p in _manifest()["packages"]
        for entry in p["files"]
        if "specifier" in entry
    }

    assert _import_map() == {"imports": expected}


def test_import_map_comes_before_any_module_script():
    """A module script before the import map would resolve bare names without it."""
    kinds = [attrs.get("type") for attrs, _ in _page().scripts]

    assert kinds.index("importmap") < kinds.index("module")


def _static_path(url: str) -> Path:
    """The file under static/ that a same-origin /static/ URL names."""
    assert url.startswith("/static/"), f"{url} is not a same-origin /static/ URL"
    return STATIC_DIR / url.removeprefix("/static/")


def _url_of(path: Path) -> str:
    return "/static/" + path.relative_to(STATIC_DIR).as_posix()


def _is_bare(specifier: str) -> bool:
    return not specifier.startswith(("/", "./", "../")) and ":" not in specifier


def test_every_module_import_resolves_to_a_file():
    """Every import in our and vendored .mjs resolves to an existing static file; bare names
    only through the import map, and vendored modules only by bare name (one Preact)."""
    imports = _import_map()["imports"]
    vendored = {_static_path(url).resolve() for url in imports.values()}
    modules = sorted(STATIC_DIR.rglob("*.mjs"))
    checked: list[str] = []

    for module in modules:
        for specifier in module_specifiers(module.read_text(encoding="utf-8")):
            where = f"{_url_of(module)} imports {specifier!r}"
            assert "standalone" not in specifier, f"{where}: htm/preact/standalone is banned"
            if _is_bare(specifier):
                assert specifier in imports, f"{where}: bare name missing from the import map"
                target = _static_path(imports[specifier])
            else:
                url = urljoin(f"https://page.test{_url_of(module)}", specifier)
                assert url.startswith("https://page.test/static/"), f"{where}: leaves /static/"
                target = _static_path(url.removeprefix("https://page.test"))
                assert target.resolve() not in vendored, f"{where}: import it by its bare name"
            assert target.is_file(), f"{where}: {target} does not exist"
            checked.append(specifier)

    assert {module.name for module in modules} >= {"app.mjs", "preact.mjs", "signals.mjs"}
    # The vendored files' own imports are part of the check, so it covers all six names.
    assert set(imports) <= set(checked)


def test_page_entry_module_is_type_checked():
    """Our own JS opts into editor type checking (CODING_STANDARDS.md, Frontend)."""
    ours = [p for p in STATIC_DIR.rglob("*.mjs") if VENDOR_DIR not in p.parents]

    assert ours
    for module in ours:
        assert module.read_text(encoding="utf-8").startswith("// @ts-check\n"), module.name


# --- Same origin only, and the stylesheet's contract (#116, ADR 0010) ---

# Attributes through which a page makes the browser fetch something. An <a href> is a link the
# visitor may follow, not a request the page makes, so it may point elsewhere.
_FETCHING_ATTRIBUTES = {"src", "srcset", "href", "poster", "data", "action", "formaction"}


def _same_origin(url: str) -> bool:
    return not re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", url.strip(), re.IGNORECASE)


def _css() -> str:
    return (STATIC_DIR / "styles.css").read_text(encoding="utf-8")


def test_same_origin_check_flags_other_origins():
    """The origin check rejects absolute, protocol-relative and data URLs."""
    assert _same_origin("/static/app.mjs") and _same_origin("vendor/x.woff2")
    for url in ("https://cdn.example/x.js", "//cdn.example/x.js", "HTTP://x", "data:text/css,"):
        assert not _same_origin(url), url


def test_page_html_fetches_nothing_from_another_origin():
    """Every URL the page's HTML makes the browser fetch, import map included, is same-origin."""
    page = _page()
    fetched = [
        value
        for tag, attrs in page.tags
        for name, value in attrs.items()
        if value is not None and name in _FETCHING_ATTRIBUTES and tag != "a"
    ]
    fetched += list(_import_map()["imports"].values())

    assert "/static/styles.css" in fetched and "/static/app.mjs" in fetched
    assert [url for url in fetched if not _same_origin(url)] == []
    assert "@import" not in (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _css_urls(css: str) -> list[str]:
    return [m.group(2) for m in re.finditer(r"""url\(\s*(["']?)([^"')]+)\1\s*\)""", css)]


def test_stylesheet_fetches_only_files_it_ships():
    """Every url() in styles.css is a same-origin file under static/; there is no @import."""
    css = _css()
    urls = _css_urls(css)

    assert urls, "the stylesheet loads the self-hosted fonts"
    assert "@import" not in css
    for url in urls:
        assert _same_origin(url), url
        assert (STATIC_DIR / url).resolve().is_file(), f"{url} does not exist under static/"


def _blocks(css: str) -> list[tuple[str, str]]:
    """Every `{…}` block in the stylesheet as (header, body), nested blocks included."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    blocks: list[tuple[str, str]] = []
    stack: list[tuple[str, int]] = []
    start = 0
    for i, char in enumerate(css):
        if char == "{":
            header = re.split(r"[;{}]", css[start:i])[-1].strip()
            stack.append((header, i + 1))
            start = i + 1
        elif char == "}":
            header, body_start = stack.pop()
            blocks.append((header, css[body_start:i]))
            start = i + 1
        elif char == ";":
            start = i + 1
    assert not stack, "unbalanced braces in styles.css"
    return blocks


def _tokens(header: str) -> dict[str, str]:
    """The custom properties declared directly in the one block with this header."""
    bodies = [body for head, body in _blocks(_css()) if head == header]
    assert len(bodies) == 1, f"expected one {header!r} block, found {len(bodies)}"
    top_level = re.sub(r"\{[^{}]*\}", "", bodies[0])
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", top_level))


LIGHT = ":root"
DARK = ':root[data-theme="dark"]'
DARK_BY_PREFERENCE = ':root:not([data-theme="light"])'


def test_stylesheet_uses_layers_tokens_and_nesting():
    """One cascade-layer order, OKLCH colour tokens on :root and native nesting."""
    css = _css()
    tokens = _tokens(LIGHT)

    assert re.search(r"^@layer [\w-]+(, [\w-]+)+;", css, re.MULTILINE)
    colours = {name: value for name, value in tokens.items() if value.startswith("oklch(")}
    assert len(colours) >= 8
    assert any(head.startswith("&") for head, _ in _blocks(css)), "no native nesting found"


def test_dark_theme_follows_the_system_unless_overridden():
    """Dark tokens apply under prefers-color-scheme: dark (unless data-theme="light") and under
    data-theme="dark", with the same values, and every colour token has a dark value."""
    css = _css()
    media = [body for head, body in _blocks(css) if head == "@media (prefers-color-scheme: dark)"]
    dark = _tokens(DARK)

    assert media and any(DARK_BY_PREFERENCE in body for body in media)
    assert _tokens(DARK_BY_PREFERENCE) == dark
    light_colours = {k for k, v in _tokens(LIGHT).items() if v.startswith("oklch(")}
    assert light_colours == {k for k, v in dark.items() if v.startswith("oklch(")}


def test_motion_is_gated_on_reduced_motion():
    """The stylesheet turns motion off for visitors who ask for less."""
    assert "@media (prefers-reduced-motion: reduce)" in _css()


def test_fonts_are_self_hosted_and_swap():
    """Both vendored families are declared with font-display: swap from vendored files."""
    faces = [body for head, body in _blocks(_css()) if head == "@font-face"]
    families = set()
    for body in faces:
        assert re.search(r"font-display:\s*swap;", body), body
        family = re.search(r"font-family:\s*\"([^\"]+)\"", body)
        assert family, body
        families.add(family.group(1))
        assert all(url.startswith("vendor/@fontsource-variable/") for url in _css_urls(body))

    assert families == {"Inter Variable", "JetBrains Mono Variable"}


def _luminance(oklch: str) -> float:
    """WCAG relative luminance of an `oklch(L C H)` colour (sRGB, clipped to gamut)."""
    match = re.fullmatch(r"oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+([\d.]+)\s*\)", oklch.strip())
    assert match, f"unparsed colour {oklch!r}"
    lightness = float(match.group(1)) / (100 if match.group(2) else 1)
    chroma, hue = float(match.group(3)), math.radians(float(match.group(4)))
    a, b = chroma * math.cos(hue), chroma * math.sin(hue)
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    red = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    green = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    blue = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    red, green, blue = (min(max(c, 0.0), 1.0) for c in (red, green, blue))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(fg: str, bg: str) -> float:
    high, low = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_contrast_helper_matches_known_values():
    """Black on white is 21:1; #767676 on white is 4.54:1 (a grey's OKLab L is cbrt(Y))."""
    assert _contrast("oklch(0 0 0)", "oklch(1 0 0)") == pytest.approx(21, abs=0.01)
    assert _contrast("oklch(0.5659 0 0)", "oklch(1 0 0)") == pytest.approx(4.54, abs=0.03)


@pytest.mark.parametrize("theme", [LIGHT, DARK])
def test_theme_tokens_meet_wcag_aa_contrast(theme: str):
    """Text tokens reach 4.5:1 on every surface and the focus ring 3:1, in both themes."""
    tokens = _tokens(LIGHT) | (_tokens(DARK) if theme == DARK else {})
    surfaces = ["--bg", "--surface", "--sunken"]
    failures = [
        f"{fg} on {bg}: {_contrast(tokens[fg], tokens[bg]):.2f} < {minimum}"
        for fg, minimum in [("--fg", 4.5), ("--muted", 4.5), ("--accent", 4.5), ("--focus", 3.0)]
        for bg in surfaces
        if _contrast(tokens[fg], tokens[bg]) < minimum
    ]
    if _contrast(tokens["--accent-fg"], tokens["--accent"]) < 4.5:
        failures.append("--accent-fg on --accent")

    assert failures == []

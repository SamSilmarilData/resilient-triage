"""Verify or update the vendored frontend files listed in `static/vendor/manifest.json`.

Standard library only (ADR 0010). Each manifest package is an npm package pinned by name and
version; its files live at `static/vendor/<name>@<version>/<path>`, where `<path>` is the file's
path inside the npm tarball's `package/` directory.

    python scripts/vendor.py verify              # offline: hashes, LICENSEs, no stray files
    python scripts/vendor.py verify --installed  # the same, on the installed package's copy
    python scripts/vendor.py update              # network: re-download every pinned package

`update` is a deliberate manual step (never run by tests or CI). To bump a package, edit its
`version` in the manifest and run `update`: it reads the version's tarball URL and integrity from
the npm registry, checks the tarball against that integrity, extracts only the listed files,
records their sha256 and rewrites the vendor directory to match the manifest.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, NotRequired, TypedDict, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = REPO_ROOT / "src" / "resilient_triage" / "api" / "static" / "vendor"
MANIFEST_NAME = "manifest.json"
REGISTRY = "https://registry.npmjs.org"
TARBALL_ROOT = "package"
FETCH_TIMEOUT_S = 60

Fetch = Callable[[str], bytes]


class FileEntry(TypedDict):
    """One vendored file: its path in the tarball's `package/`, sha256 and import-map name."""

    path: str
    sha256: NotRequired[str]
    specifier: NotRequired[str]


class Package(TypedDict):
    """One pinned npm package; `update` records its tarball URL and integrity."""

    name: str
    version: str
    license: str
    license_file: str
    tarball: NotRequired[str]
    integrity: NotRequired[str]
    files: list[FileEntry]


class Manifest(TypedDict):
    """The manifest file's shape (other top-level keys, such as `$comment`, are kept as is)."""

    packages: list[Package]


class VendorError(Exception):
    """An update that cannot proceed safely; nothing has been written."""


def package_dir(package: Package) -> str:
    """Return a package's directory under vendor/: `<name>@<version>`."""
    return f"{package['name']}@{package['version']}"


def load_manifest(vendor_dir: Path) -> Manifest:
    """Read the manifest in `vendor_dir`."""
    manifest: Manifest = json.loads((vendor_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    return manifest


def _is_safe(path: str) -> bool:
    """Whether a manifest path stays inside its package directory."""
    pure = PurePosixPath(path)
    return bool(path) and not pure.is_absolute() and ".." not in pure.parts and "\\" not in path


def _sha256(data: bytes) -> str:
    """Return the hex sha256 of `data`."""
    return hashlib.sha256(data).hexdigest()


def verify(vendor_dir: Path) -> list[str]:
    """Check `vendor_dir` against its manifest; return one message per problem, empty if none."""
    manifest = load_manifest(vendor_dir)
    problems: list[str] = []
    expected = {MANIFEST_NAME}
    specifiers: dict[str, str] = {}

    for package in manifest["packages"]:
        pkg_dir = package_dir(package)
        if not _is_safe(pkg_dir):
            problems.append(f"{pkg_dir}: unsafe package name or version")
            continue
        listed = {entry["path"] for entry in package["files"]}
        if package["license_file"] not in listed:
            problems.append(
                f"{pkg_dir}: license file {package['license_file']!r} is not a listed file"
            )
        for entry in package["files"]:
            rel = f"{pkg_dir}/{entry['path']}"
            if not _is_safe(entry["path"]):
                problems.append(f"{rel}: unsafe path, it must stay inside {pkg_dir}/")
                continue
            expected.add(rel)
            target = vendor_dir / rel
            if not target.is_file():
                problems.append(f"{rel}: missing")
            elif _sha256(target.read_bytes()) != entry.get("sha256"):
                problems.append(f"{rel}: sha256 does not match the manifest")
            if "specifier" in entry:
                owner = specifiers.setdefault(entry["specifier"], rel)
                if owner != rel:
                    problems.append(
                        f"{rel}: specifier {entry['specifier']!r} is already mapped to {owner}"
                    )

    for path in sorted(vendor_dir.rglob("*")):
        rel = path.relative_to(vendor_dir).as_posix()
        if path.is_file() and rel not in expected:
            problems.append(f"{rel}: not listed in the manifest")
    return problems


def fetch_url(url: str) -> bytes:
    """Download `url` over HTTPS."""
    if not url.startswith("https://"):
        raise VendorError(f"refusing to fetch a non-HTTPS URL: {url}")
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:
        data: bytes = response.read()
    return data


def _check_integrity(data: bytes, integrity: str, label: str) -> None:
    """Raise unless `data` matches an npm `sha512-<base64>` integrity string."""
    algorithm, _, digest = integrity.partition("-")
    if algorithm != "sha512":
        raise VendorError(f"{label}: unsupported integrity algorithm {algorithm!r}")
    if base64.b64encode(hashlib.sha512(data).digest()).decode() != digest:
        raise VendorError(f"{label}: tarball does not match its registry integrity")


def _download(package: Package, fetch: Fetch) -> dict[str, bytes]:
    """Fetch one pinned package and return its listed files; pin tarball, integrity, sha256."""
    label = package_dir(package)
    if not _is_safe(label):
        raise VendorError(f"{label}: unsafe package name or version")
    meta = json.loads(fetch(f"{REGISTRY}/{package['name']}/{package['version']}"))
    if meta.get("version") != package["version"]:
        raise VendorError(f"{label}: registry answered version {meta.get('version')!r}")
    tarball, integrity = meta["dist"]["tarball"], meta["dist"]["integrity"]
    recorded = package.get("integrity")
    if package.get("tarball") == tarball and recorded not in (None, integrity):
        raise VendorError(
            f"{label}: registry integrity {integrity} differs from the recorded {recorded}"
        )
    data = fetch(tarball)
    _check_integrity(data, integrity, label)

    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for entry in package["files"]:
            path = entry["path"]
            if not _is_safe(path):
                raise VendorError(f"{label}: unsafe path {path!r}")
            try:
                member = tar.getmember(f"{TARBALL_ROOT}/{path}")
            except KeyError:
                raise VendorError(f"{label}: {path} is not in the tarball") from None
            extracted = tar.extractfile(member) if member.isfile() else None
            if extracted is None:
                raise VendorError(f"{label}: {path} is not a regular file in the tarball")
            files[path] = extracted.read()

    package["tarball"], package["integrity"] = tarball, integrity
    for entry in package["files"]:
        entry["sha256"] = _sha256(files[entry["path"]])
    return files


def _ordered(entry: Mapping[str, Any], first: tuple[str, ...]) -> dict[str, Any]:
    """Copy a manifest entry with the `first` keys leading, then any others as they come."""
    return {k: entry[k] for k in first if k in entry} | {
        k: v for k, v in entry.items() if k not in first
    }


def _canonical(package: Package) -> Package:
    """Order a package's keys for a stable, readable manifest."""
    keys = ("name", "version", "license", "license_file", "tarball", "integrity")
    ordered = _ordered(package, keys)
    del ordered["files"]  # re-added last
    ordered["files"] = [
        _ordered(entry, ("path", "specifier", "sha256")) for entry in package["files"]
    ]
    return cast(Package, ordered)


def _remove_unlisted(vendor_dir: Path, keep: set[str]) -> None:
    """Delete files under `vendor_dir` not in `keep`, then any directory left empty."""
    for path in sorted(vendor_dir.rglob("*"), reverse=True):
        rel = path.relative_to(vendor_dir).as_posix()
        if path.is_file() and rel not in keep:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def update(vendor_dir: Path, fetch: Fetch = fetch_url) -> list[str]:
    """Re-vendor every manifest package from npm; return `verify`'s problems afterwards."""
    manifest = load_manifest(vendor_dir)
    downloads = {package_dir(p): _download(p, fetch) for p in manifest["packages"]}

    keep = {MANIFEST_NAME}
    for pkg_dir, files in downloads.items():
        for path, data in files.items():
            target = vendor_dir / pkg_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            keep.add(f"{pkg_dir}/{path}")
    manifest["packages"] = [_canonical(p) for p in manifest["packages"]]
    (vendor_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    _remove_unlisted(vendor_dir, keep)
    return verify(vendor_dir)


def installed_vendor_dir() -> Path:
    """Locate static/vendor/ inside the importable `resilient_triage.api` package."""
    spec = importlib.util.find_spec("resilient_triage.api")
    if spec is None or spec.origin is None:
        raise SystemExit("resilient_triage is not installed")
    return Path(spec.origin).parent / "static" / "vendor"


def main(argv: list[str] | None = None) -> int:
    """Run the `verify` or `update` command; return the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify", help="check files against the manifest (offline)")
    where = check.add_mutually_exclusive_group()
    where.add_argument("--dir", type=Path, help="vendor directory to check")
    where.add_argument(
        "--installed", action="store_true", help="check the installed package's copy"
    )
    commands.add_parser("update", help="re-download every pinned package (network)")
    args = parser.parse_args(argv)

    if args.command == "verify":
        vendor_dir = installed_vendor_dir() if args.installed else (args.dir or VENDOR_DIR)
        problems = verify(vendor_dir)
        for problem in problems:
            print(f"FAIL {problem}")
        if problems:
            return 1
        count = sum(len(p["files"]) for p in load_manifest(vendor_dir)["packages"])
        print(f"OK {count} vendored files match {vendor_dir / MANIFEST_NAME}")
        return 0

    try:
        problems = update(VENDOR_DIR)
    except VendorError as exc:
        problems = [f"{exc} (nothing was written)"]
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print(f"OK updated {VENDOR_DIR / MANIFEST_NAME}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

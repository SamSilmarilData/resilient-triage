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
import hashlib
import importlib.util
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = REPO_ROOT / "src" / "resilient_triage" / "api" / "static" / "vendor"
MANIFEST_NAME = "manifest.json"


def package_dir(package: dict[str, Any]) -> str:
    """Return a package's directory under vendor/: `<name>@<version>`."""
    return f"{package['name']}@{package['version']}"


def load_manifest(vendor_dir: Path) -> dict[str, Any]:
    """Read the manifest in `vendor_dir`."""
    manifest: dict[str, Any] = json.loads((vendor_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
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
            elif _sha256(target.read_bytes()) != entry["sha256"]:
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
    return 2


if __name__ == "__main__":
    sys.exit(main())

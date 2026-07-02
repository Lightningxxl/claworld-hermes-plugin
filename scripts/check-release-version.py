#!/usr/bin/env python3
"""Validate Claworld Hermes release version metadata."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTING_VERSION_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}-testing\.\d+$")
STABLE_VERSION_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}$")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_python_string_constant(path: Path, name: str) -> str | None:
    match = re.search(
        rf"^{re.escape(name)}\s*=\s*[\"']([^\"']+)[\"']\s*$",
        read_text(path),
        flags=re.MULTILINE,
    )
    return match.group(1) if match else None


def extract_frontmatter_version(path: Path) -> str | None:
    for line in read_text(path).splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


def collect_versions() -> list[tuple[str, str | None]]:
    versions: list[tuple[str, str | None]] = [
        ("version.py:PLUGIN_VERSION", extract_python_string_constant(ROOT / "version.py", "PLUGIN_VERSION")),
        ("plugin.yaml:version", extract_frontmatter_version(ROOT / "plugin.yaml")),
    ]
    for skill_path in sorted((ROOT / "skills").glob("*/SKILL.md")):
        versions.append((str(skill_path.relative_to(ROOT)) + ":version", extract_frontmatter_version(skill_path)))
    return versions


def validate_version_shape(version: str, channel: str) -> list[str]:
    if channel == "testing" and not TESTING_VERSION_RE.fullmatch(version):
        return [f"testing releases must use yyyy.m.d-testing.N; found {version}"]
    if channel == "stable" and not STABLE_VERSION_RE.fullmatch(version):
        return [f"stable releases must use yyyy.m.d; found {version}"]
    if channel == "any" and not (
        TESTING_VERSION_RE.fullmatch(version) or STABLE_VERSION_RE.fullmatch(version)
    ):
        return [f"release version must use yyyy.m.d or yyyy.m.d-testing.N; found {version}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel",
        choices=("any", "testing", "stable"),
        default="any",
        help="release lane to validate",
    )
    parser.add_argument(
        "--print-version",
        action="store_true",
        help="print only the validated version",
    )
    args = parser.parse_args()

    versions = collect_versions()
    errors: list[str] = []
    missing = [(label, value) for label, value in versions if not value]
    if missing:
        errors.extend(f"missing version in {label}" for label, _value in missing)

    observed_values = {value for _label, value in versions if value}
    version = next(iter(observed_values)) if len(observed_values) == 1 else None
    if len(observed_values) > 1:
        errors.append("release version metadata is not consistent:")
        errors.extend(f"  {label}: {value or '<missing>'}" for label, value in versions)

    if version:
        errors.extend(validate_version_shape(version, args.channel))

    if errors:
        print("Release version check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    if not version:
        print("Release version check failed: no version found", file=sys.stderr)
        return 1

    if args.print_version:
        print(version)
    else:
        print(f"PASS release version check: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

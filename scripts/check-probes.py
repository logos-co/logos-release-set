#!/usr/bin/env python3
"""Check the committed probe sources match what the specs write.

Each headless spec writes its probe module inline, with `file:` steps, and
builds it on Linux and macOS. The Windows leg cannot run those steps: it
installs the probe flake.nix cross-builds from doctests/probes/<name>. That
copy must be the spec's own, or Windows would validate a different module than
the one the spec shows.

Usage:
    python3 scripts/check-probes.py
"""

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# spec -> the probe directory flake.nix builds for it.
PROBES = {
    "headless-storage-module": "doctests/probes/storage",
    "headless-delivery-module": "doctests/probes/delivery",
}


def spec_files(spec):
    """{path under probe/: content} for every file the spec writes there."""
    with open(os.path.join(ROOT, "doctests", f"{spec}.test.yaml"), encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    files = {}
    for section in doc.get("sections", []):
        for step in section.get("steps", []):
            path = (step.get("file") or {}).get("path", "")
            if path.startswith("probe/"):
                files[path[len("probe/"):]] = step["file"].get("content", "")
    return files


def committed_files(directory):
    files = {}
    base = os.path.join(ROOT, directory)
    for dirpath, _, names in os.walk(base):
        for name in names:
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as handle:
                files[os.path.relpath(path, base)] = handle.read()
    return files


def main():
    problems = []
    for spec, directory in PROBES.items():
        written, committed = spec_files(spec), committed_files(directory)
        if not written:
            problems.append(f"{spec}: writes no probe/ files")
        for path in sorted(set(written) | set(committed)):
            if path not in committed:
                problems.append(f"{spec}: writes probe/{path}, missing from {directory}")
            elif path not in written:
                problems.append(f"{directory}/{path}: not written by {spec}")
            elif written[path] != committed[path]:
                problems.append(f"{directory}/{path}: differs from what {spec} writes")
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        print("Copy each spec's probe/ files into its doctests/probes/ directory.",
              file=sys.stderr)
        return 1
    print(f"probe sources match their specs ({', '.join(PROBES)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

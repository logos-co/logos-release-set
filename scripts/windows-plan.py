#!/usr/bin/env python3
"""Plan the release set's own Windows doc-test legs.

Nix does not run on Windows. logos-windows-ci builds this repo's flake.nix on
Linux, stages each target as a directory, and runs the spec's Windows half
against them on windows-latest. From release-set.lock.json this decides:

    matrix  one leg per spec that can run, with the targets it stages
    any     whether there is a leg at all
    args    --override-input pairs pinning flake.nix to the release set: the
            builder tag it pins, and the logosctl Windows zip of the release
            it pins

and writes skip records naming why the other specs do not run on Windows.

Usage:
    python3 scripts/windows-plan.py release-set.lock.json \
        --skips-out results-windows-x86_64-skipped.json --format github
"""

import argparse
import importlib.util
import json
import os
import sys


def _load_select_specs():
    """select-specs.py is not an importable module name (hyphen)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "select-specs.py")
    spec = importlib.util.spec_from_file_location("select_specs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


select = _load_select_specs()
PLATFORM = select.WINDOWS
# Every leg stages these: the released logosctl, and the pins plus the helper
# that reads them (Windows has no lock).
COMMON_TARGETS = ["logosctl", "release-set"]


def override_args(lock):
    entries = select.index_by_name(lock)
    args = []
    builder = entries.get("logos-module-builder")
    if builder:
        ref = builder.get("flakeRef") or f"github:{builder['repo']}/{builder['tag']}"
        args += ["--override-input", "logos-module-builder", ref]
    ctl = entries.get("logos-logoscore-cli") or {}
    for asset in ctl.get("assets", []):
        if asset.get("platform") == PLATFORM and asset["name"].startswith("logosctl"):
            args += ["--override-input", "logosctl-windows", asset["url"]]
            break
    return args


def plan(lock, requested):
    entries = select.index_by_name(lock)
    legs, skipped = [], []
    for spec in sorted(select.SPECS):
        if requested and spec not in requested:
            skipped.append({"spec": spec, "platform": PLATFORM, "status": "skipped",
                            "reason": "not in the requested spec list"})
            continue
        reasons = select.missing_for(spec, lock, entries, PLATFORM)
        if reasons:
            skipped.append({"spec": spec, "platform": PLATFORM, "status": "skipped",
                            "reason": reasons[0], "allReasons": reasons})
            continue
        targets = COMMON_TARGETS + select.SPECS[spec]["windows"]["targets"]
        legs.append({"spec": spec, "targets": " ".join(targets)})
    for entry in skipped:
        entry["runner"] = "windows-latest"
    return legs, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lock")
    parser.add_argument("--specs", default="",
                        help="space-separated specs requested (default: all)")
    parser.add_argument("--skips-out", default=None)
    parser.add_argument("--format", choices=("text", "github"), default="text")
    args = parser.parse_args()

    lock = json.load(open(args.lock, encoding="utf-8"))
    legs, skipped = plan(lock, set(args.specs.split()))
    overrides = override_args(lock)

    if args.skips_out:
        with open(args.skips_out, "w", encoding="utf-8") as handle:
            json.dump(skipped, handle, indent=2)
            handle.write("\n")

    if args.format == "github":
        print(f"matrix={json.dumps({'include': legs})}")
        print(f"any={'true' if legs else 'false'}")
        print(f"args={' '.join(overrides)}")
    else:
        print(f"platform: {PLATFORM}")
        for leg in legs:
            print(f"  RUN   {leg['spec']}  (stages {leg['targets']})")
        for entry in skipped:
            print(f"  SKIP  {entry['spec']}: {entry['reason']}")
        print("flake inputs: " + (" ".join(overrides) or "(the lock's)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

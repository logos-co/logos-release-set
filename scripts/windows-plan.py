#!/usr/bin/env python3
"""Plan the release set's own Windows doc-test legs.

Nix does not run on Windows. logos-windows-ci builds this repo's flake.nix on
Linux, stages each target as a directory, and runs the spec's Windows half
against them on windows-latest. From release-set.lock.json this decides:

    matrix  one leg per spec that can run, with the targets it stages, the
            extra targets (Basecamp's own outputs at the pinned commit) and
            whether its staged .lgx files must carry a Windows variant
    any     whether there is a leg at all
    args    --override-input pairs pinning flake.nix to the release set: the
            builder tag it pins, the logosctl and lgpm Windows zips and the
            Basecamp installer of the releases it pins

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

# The released Windows artifacts flake.nix stages: (app, asset name prefix,
# flake input).
WINDOWS_ASSETS = [("logos-logoscore-cli", "logosctl", "logosctl-windows"),
                  ("logos-package-manager", "lgpm", "lgpm-windows"),
                  ("logos-basecamp", "LogosBasecamp", "basecamp-setup")]


def override_args(lock):
    entries = select.index_by_name(lock)
    args = []
    builder = entries.get("logos-module-builder")
    if builder:
        ref = builder.get("flakeRef") or f"github:{builder['repo']}/{builder['tag']}"
        args += ["--override-input", "logos-module-builder", ref]
    for app, prefix, flake_input in WINDOWS_ASSETS:
        for asset in (entries.get(app) or {}).get("assets", []):
            if asset.get("platform") == PLATFORM and asset["name"].startswith(prefix):
                url = asset["url"]
                # A zip unpacks like a tarball; the installer is one file.
                if url.endswith(".exe"):
                    url = "file+" + url
                args += ["--override-input", flake_input, url]
                break
    return args


# Basecamp's inspector bundle and its test driver, from its own flake at the
# commit the release set resolved, staged under the names the spec uses.
BASECAMP_TARGETS = ["bin-bundle-dir-inspector", "logos-qt-mcp"]


def extra_targets(windows, entries):
    if not windows.get("basecamp"):
        return {}
    commit = entries["logos-basecamp"]["commit"]
    return {name: {"ref": f"github:logos-co/logos-basecamp/{commit}#packages.x86_64-windows.{name}"}
            for name in BASECAMP_TARGETS}


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
        windows = select.SPECS[spec]["windows"]
        targets = COMMON_TARGETS + windows["targets"]
        legs.append({"spec": spec, "targets": " ".join(targets),
                     "extra_targets": json.dumps(extra_targets(windows, entries)),
                     # The probes are .lgx files; Basecamp stages none.
                     "lgx_variant": any(t.startswith("probe-") for t in targets)})
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
            extra = " ".join(json.loads(leg["extra_targets"]))
            print(f"  RUN   {leg['spec']}  (stages {leg['targets']}{' + ' + extra if extra else ''})")
        for entry in skipped:
            print(f"  SKIP  {entry['spec']}: {entry['reason']}")
        print("flake inputs: " + (" ".join(overrides) or "(the lock's)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

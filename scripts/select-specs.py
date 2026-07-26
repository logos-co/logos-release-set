#!/usr/bin/env python3
"""Decide which doc-tests can run on a platform, and why the rest cannot.

A pinned release may simply not publish an artifact for every platform — a
release with no linux-arm64 AppImage leaves nothing to smoke-test on an arm64
Linux runner. That is not a failure of the release set, so the workflow skips
the affected spec and flags it in the release description instead of going red.

This decides that up front, from release-set.lock.json, rather than letting a
spec discover it half-way through and fail.

Usage:
    python3 scripts/select-specs.py release-set.lock.json --platform linux-arm64
    python3 scripts/select-specs.py release-set.lock.json --platform linux-arm64 --format github
"""

import argparse
import json
import sys

TOOLS = ["logos-logoscore-cli", "logos-package-downloader", "logos-package-manager"]
PM_TOOLS = ["logos-package-downloader", "logos-package-manager"]

BUILDER = ["logos-module-builder"]

# What each spec needs on the runner's own platform.
#   apps      — released binaries that must exist for this platform
#   packages  — catalog packages that must publish this platform's variant
#               ("*" means every module and UI app in the set)
#   devUtils  — dev utils the spec exercises. Not gating (they are consumed as
#               flake refs and publish no per-platform binaries), but recorded
#               so each artifact can point at the doc-tests that covered it.
SPECS = {
    "headless-storage-module":    {"apps": TOOLS, "packages": ["storage_module"],
                                   "devUtils": BUILDER},
    "headless-delivery-module":   {"apps": TOOLS, "packages": ["delivery_module"],
                                   "devUtils": BUILDER},
    "headless-blockchain-module": {"apps": TOOLS, "packages": ["blockchain_module"],
                                   "devUtils": BUILDER},
    # Launches the shipped Basecamp artifact, so that artifact must exist.
    "basecamp-appimage-smoke":    {"apps": PM_TOOLS + ["logos-basecamp"],
                                   "packages": ["*"], "devUtils": []},
    # Builds Basecamp from the pinned commit, so it needs no Basecamp asset.
    "basecamp-ui":                {"apps": PM_TOOLS, "packages": ["*"], "devUtils": []},
}


def specs_covering(name, lock):
    """Which doc-tests exercise this component."""
    every_package = [i["name"] for i in lock.get("modules", []) + lock.get("uiApps", [])]
    covering = []
    for spec, needs in sorted(SPECS.items()):
        packages = every_package if needs["packages"] == ["*"] else needs["packages"]
        if name in needs["apps"] or name in packages or name in needs.get("devUtils", []):
            covering.append(spec)
    return covering


def index_by_name(lock):
    entries = {}
    for group in ("apps", "devUtils", "modules", "uiApps"):
        for item in lock.get(group, []):
            entries[item["name"]] = item
    return entries


def missing_for(spec, lock, entries, platform):
    """Reasons this spec cannot run on this platform. Empty list = it can."""
    reasons = []
    needs = SPECS[spec]

    for name in needs["apps"]:
        item = entries.get(name)
        if item is None:
            reasons.append(f"{name} is not in the release set")
        elif platform not in (item.get("platforms") or []):
            label = item.get("tag") or item.get("version")
            reasons.append(f"no {platform} artifact published for {name}@{label}")

    packages = needs["packages"]
    if packages == ["*"]:
        packages = [i["name"] for i in lock.get("modules", []) + lock.get("uiApps", [])]
    for name in packages:
        item = entries.get(name)
        if item is None:
            reasons.append(f"{name} is not in the release set")
        elif platform not in (item.get("platforms") or []):
            reasons.append(
                f"no {platform} variant published for {name}@{item.get('version')}"
            )
    return reasons


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lock")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--format", choices=("text", "json", "github"), default="text")
    args = parser.parse_args()

    lock = json.load(open(args.lock, encoding="utf-8"))
    entries = index_by_name(lock)

    runnable, skipped = [], []
    for spec in sorted(SPECS):
        reasons = missing_for(spec, lock, entries, args.platform)
        if reasons:
            skipped.append({
                "spec": spec,
                "platform": args.platform,
                "status": "skipped",
                # One spec can be blocked by several missing artifacts; the first
                # is the actionable one, the rest are listed for completeness.
                "reason": reasons[0],
                "allReasons": reasons,
            })
        else:
            runnable.append(spec)

    if args.format == "json":
        json.dump({"run": runnable, "skipped": skipped}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif args.format == "github":
        # Consumed by the workflow: a space-separated spec list plus the skip
        # records, written to $GITHUB_OUTPUT.
        specs = " ".join(f"doctests/{name}.test.yaml" for name in runnable)
        print(f"specs={specs}")
        print(f"skipped={json.dumps(skipped)}")
        print(f"any={'true' if runnable else 'false'}")
    else:
        print(f"platform: {args.platform}")
        for name in runnable:
            print(f"  RUN   {name}")
        for entry in skipped:
            print(f"  SKIP  {entry['spec']}: {entry['reason']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

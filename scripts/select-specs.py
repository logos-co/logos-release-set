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

# The headless specs drive logosctl, which bundles package management, so
# logos-logoscore-cli is the only binary they need. The Basecamp specs download
# with logosctl too, and install into Basecamp's user dir with lgpm.
CTL = ["logos-logoscore-cli"]
PM_TOOLS = CTL + ["logos-package-manager"]

BUILDER = ["logos-module-builder"]

# What each spec needs on the runner's own platform.
#   apps      — released binaries that must exist for this platform
#   packages  — catalog packages that must publish this platform's variant
#               ("*" means every module and UI app in the set)
#   devUtils  — dev utils the spec exercises. Not gating (they are consumed as
#               flake refs and publish no per-platform binaries), but recorded
#               so each artifact can point at the doc-tests that covered it.
#   windows   — the spec's Windows half, if it has one: what that half needs
#               instead of `packages`, the flake.nix targets its leg stages
#               besides logosctl/ and release-set/, and `basecamp` when it also
#               stages Basecamp's own outputs at the pinned commit. A spec
#               without one is skipped on Windows.
SPECS = {
    # Each headless spec also loads `openmetrics` and scrapes /metrics, so that
    # package must publish this platform's variant too. It publishes no Windows
    # build, so the specs' metrics sections are Linux and macOS only.
    "headless-storage-module":    {"apps": CTL,
                                   "packages": ["storage_module", "openmetrics"],
                                   "devUtils": BUILDER,
                                   "windows": {"packages": ["storage_module"],
                                               "targets": ["probe-storage"]}},
    "headless-delivery-module":   {"apps": CTL,
                                   "packages": ["delivery_module", "openmetrics"],
                                   "devUtils": BUILDER,
                                   "windows": {"packages": ["delivery_module"],
                                               "targets": ["probe-delivery"]}},
    "headless-blockchain-module": {"apps": CTL,
                                   "packages": ["blockchain_module", "openmetrics"],
                                   "devUtils": BUILDER},
    # Launches the shipped Basecamp artifact, so that artifact must exist.
    "basecamp-appimage-smoke":    {"apps": PM_TOOLS + ["logos-basecamp"],
                                   "packages": ["*"], "devUtils": []},
    # Builds Basecamp from the pinned commit, so it needs no Basecamp asset. On
    # Windows it installs what publishes a Windows variant and asserts the
    # packages below, which must.
    "basecamp-ui":                {"apps": PM_TOOLS, "packages": ["*"], "devUtils": [],
                                   "windows": {"packages": ["storage_ui", "chat_ui",
                                                            "storage_module", "chat_module",
                                                            "delivery_module"],
                                               "targets": ["lgpm"],
                                               "basecamp": True}},
}


WINDOWS = "windows-x86_64"


def specs_covering(name, lock, platform=None):
    """Which doc-tests exercise this component (on `platform`, if given)."""
    every_package = [i["name"] for i in lock.get("modules", []) + lock.get("uiApps", [])]
    covering = []
    for spec, needs in sorted(SPECS.items()):
        packages = needs["packages"]
        if platform == WINDOWS:
            if "windows" not in needs:
                continue
            packages = needs["windows"]["packages"]
        packages = every_package if packages == ["*"] else packages
        if name in needs["apps"] or name in packages or name in needs.get("devUtils", []):
            covering.append(spec)
    return covering


def index_by_name(lock):
    entries = {}
    for group in ("apps", "devUtils", "tools", "modules", "uiApps"):
        for item in lock.get(group, []):
            entries[item["name"]] = item
    return entries


def missing_for(spec, lock, entries, platform):
    """Reasons this spec cannot run on this platform. Empty list = it can."""
    needs = SPECS[spec]
    if platform == WINDOWS and "windows" not in needs:
        # Say whether the artifacts such a half would need exist there at all.
        gaps = artifact_gaps(needs["apps"], needs["packages"], lock, entries, platform)
        return ["the spec has no Windows half" + (f"; {gaps[0]}" if gaps else "")]
    packages = needs["windows"]["packages"] if platform == WINDOWS else needs["packages"]
    return artifact_gaps(needs["apps"], packages, lock, entries, platform)


def artifact_gaps(apps, packages, lock, entries, platform):
    """The named artifacts that publish nothing for this platform."""
    reasons = []
    for name in apps:
        item = entries.get(name)
        if item is None:
            reasons.append(f"{name} is not in the release set")
        elif platform not in (item.get("platforms") or []):
            # `apps` entries are pinned by tag; never fall back to a catalog
            # entry's source-repo tag here.
            label = item.get("tag") or item.get("version")
            reasons.append(f"no {platform} artifact published for {name}@{label}")

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
    parser.add_argument("--only", default=None,
                        help="consider just this one spec (the workflow runs one "
                             "spec per job, so each job asks only about its own)")
    parser.add_argument("--format", choices=("text", "json", "github"), default="text")
    args = parser.parse_args()

    lock = json.load(open(args.lock, encoding="utf-8"))
    entries = index_by_name(lock)

    if args.only and args.only not in SPECS:
        sys.exit(f"unknown spec {args.only!r}; known: {', '.join(sorted(SPECS))}")
    considered = [args.only] if args.only else sorted(SPECS)

    runnable, skipped = [], []
    for spec in considered:
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

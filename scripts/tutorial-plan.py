#!/usr/bin/env python3
"""Plan the tutorial run from the `tutorial` entry of release-set.lock.json.

resolve.py has already pinned every repo the tutorial builds against (the
release set's own version where it pins one). This turns that into what the
workflow needs:

    --override-out FILE   {repo: ref}, handed to the tutorial's own
                          `scripts/tutorial-set.py doctest-args --override`
    --skips-out FILE      a skipped result per spec on each release-set platform
                          the tutorial does not run on, and on Windows for each
                          spec without a Windows leg
    --specs "A B"         the workflow's `specs` input: Windows legs not named
                          are skipped here (their jobs cannot skip themselves)
    --format github       matrix=, any=, windows_matrix=, windows_any=, repo=,
                          commit= for $GITHUB_OUTPUT
    --format markdown     the pins, for the job summary

Usage:
    python3 scripts/tutorial-plan.py release-set.lock.json --format markdown
"""

import argparse
import json
import sys

WINDOWS = "windows-x86_64"

# Must match the platform -> runner pairs of the doctests job.
RUNNERS = {
    "linux-x86_64": "ubuntu-latest",
    "linux-arm64": "ubuntu-24.04-arm",
    "macos-arm64": "macos-latest",
}


def pins_markdown(tutorial):
    tag = f"`{tutorial['tag']}` " if tutorial.get("tag") else ""
    rows = [f"Tutorial: [{tutorial['name']}]({tutorial['repoUrl']}) "
            f"{tag}@ `{tutorial['commit'][:10]}`", "",
            "| Repo | Ref | Commit | From |", "|---|---|---|---|"]
    for pin in tutorial["pins"]:
        ref = pin["ref"] if pin["ref"] != pin["commit"] else pin["commit"][:10]
        rows.append(f"| [{pin['name']}]({pin['repoUrl']}) | `{ref}` "
                    f"| [`{pin['commit'][:10]}`]({pin['repoUrl']}/commit/{pin['commit']}) "
                    f"| {pin['source']} |")
    return "\n".join(rows) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lock")
    parser.add_argument("--override-out", default=None)
    parser.add_argument("--skips-out", default=None)
    parser.add_argument("--specs", default="")
    parser.add_argument("--format", choices=("github", "markdown"), default="markdown")
    args = parser.parse_args()

    lock = json.load(open(args.lock, encoding="utf-8"))
    tutorial = lock.get("tutorial")

    include, skipped = [], []
    if tutorial:
        for spec in tutorial["specs"]:
            for platform in lock["platforms"]:
                if platform in tutorial["platforms"] and platform in RUNNERS:
                    include.append({"spec": spec, "platform": platform,
                                    "runner": RUNNERS[platform]})
                else:
                    skipped.append({
                        "spec": spec, "platform": platform, "status": "skipped",
                        "reason": f"{tutorial['name']} does not run on {platform} "
                                  "(tutorial-set.json platforms)",
                    })

    # Windows is not a release-set platform; only the tutorial's legs run it.
    windows = []
    requested = set(args.specs.split())
    if tutorial:
        legs = {leg["spec"]: leg for leg in tutorial.get("windows", [])}
        for spec in tutorial["specs"]:
            reason = None
            if spec not in legs:
                reason = "no Windows leg in tutorial-set.json"
            elif requested and spec not in requested:
                reason = "not in the requested spec list"
            if reason:
                skipped.append({"spec": spec, "platform": WINDOWS,
                                "status": "skipped", "reason": reason})
            else:
                windows.append({"spec": spec, "targets": " ".join(legs[spec]["targets"])})

    if args.override_out:
        override = {p["name"]: p["ref"] for p in (tutorial or {}).get("pins", [])}
        with open(args.override_out, "w", encoding="utf-8") as handle:
            json.dump(override, handle, indent=2)
            handle.write("\n")
    if args.skips_out:
        with open(args.skips_out, "w", encoding="utf-8") as handle:
            json.dump(skipped, handle, indent=2)
            handle.write("\n")

    if args.format == "github":
        print(f"matrix={json.dumps({'include': include})}")
        print(f"any={'true' if include else 'false'}")
        print(f"windows_matrix={json.dumps({'include': windows})}")
        print(f"windows_any={'true' if windows else 'false'}")
        print(f"repo={(tutorial or {}).get('repo', '')}")
        print(f"commit={(tutorial or {}).get('commit', '')}")
    elif tutorial:
        sys.stdout.write(pins_markdown(tutorial))
    else:
        print("_This release set pins no tutorial._")
    return 0


if __name__ == "__main__":
    sys.exit(main())

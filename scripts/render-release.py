#!/usr/bin/env python3
"""Merge per-platform test results into the lock and render the release notes.

Inputs:
    the resolved release-set.lock.json
    results-<platform>.json files produced by each doc-test job

Outputs:
    release-set.lock.json  with `tests[]` filled in (the "output JSON")
    RELEASE.md             the release description

The release links out to every artifact rather than mirroring it: the binaries
already live in their own repos' releases and the .lgx files in the catalog, and
a full mirror would be multiple GB per release set.

Usage:
    python3 scripts/render-release.py release-set.lock.json results-*.json \
        --out-lock release-set.lock.json --out-notes RELEASE.md
"""

import argparse
import json
import sys

PLATFORM_LABELS = {
    "linux-x86_64": "linux x86_64",
    "linux-arm64": "linux arm64",
    "macos-arm64": "macOS arm64",
}

STATUS_MARK = {"passed": "✅", "failed": "❌", "skipped": "⏭️"}


def short(commit):
    return (commit or "")[:10] or "—"


def link(text, url):
    return f"[{text}]({url})" if url else text


def binary_table(items, title):
    if not items:
        return ""
    rows = [f"### {title}", "",
            "| Component | Tag | Commit | Downloads |",
            "|---|---|---|---|"]
    for item in items:
        if item.get("consumedAs") == "flake":
            downloads = f"`{item.get('flakeRef', '')}`"
        else:
            # Label each download by the platform it is for — asset filenames
            # alone are ambiguous (two of them end in "-linux.tar.gz").
            downloads = " · ".join(
                link(PLATFORM_LABELS.get(asset.get("platform"), asset["name"]), asset["url"])
                for asset in sorted(item.get("assets", []),
                                    key=lambda a: list(PLATFORM_LABELS).index(a["platform"])
                                    if a.get("platform") in PLATFORM_LABELS else 99)
            ) or "—"
        commit = link(f"`{short(item.get('commit'))}`",
                      f"{item['repoUrl']}/commit/{item['commit']}" if item.get("commit") else None)
        rows.append(
            f"| {link(item['name'], item.get('repoUrl'))} "
            f"| {link(item.get('tag') or '—', item.get('releaseUrl'))} "
            f"| {commit} | {downloads} |"
        )
    return "\n".join(rows) + "\n"


def package_table(items, title):
    if not items:
        return ""
    rows = [f"### {title}", "",
            "| Package | Version | Source commit | Package |",
            "|---|---|---|---|"]
    for item in items:
        commit = link(f"`{short(item.get('commit'))}`",
                      f"{item['repoUrl']}/commit/{item['commit']}" if item.get("commit") else None)
        lgx = (item.get("lgx") or {}).get("url")
        rows.append(
            f"| {item['name']} "
            f"| {link(item['version'], item.get('catalogReleaseUrl'))} "
            f"| {link(item.get('repo') or '—', item.get('repoUrl'))} @ {commit} "
            f"| {link('.lgx', lgx)} |"
        )
    return "\n".join(rows) + "\n"


def results_table(tests):
    if not tests:
        return "_No doc-tests were run._\n"
    platforms = sorted({t["platform"] for t in tests}, key=lambda p: list(PLATFORM_LABELS).index(p)
                       if p in PLATFORM_LABELS else 99)
    specs = sorted({t["spec"] for t in tests})

    rows = ["| Doc-test | " + " | ".join(PLATFORM_LABELS.get(p, p) for p in platforms) + " |",
            "|---" * (len(platforms) + 1) + "|"]
    by_key = {(t["spec"], t["platform"]): t for t in tests}
    for spec in specs:
        cells = []
        for platform in platforms:
            test = by_key.get((spec, platform))
            if test is None:
                cells.append("—")
                continue
            mark = STATUS_MARK.get(test["status"], test["status"])
            cells.append(link(mark, test.get("reportUrl")) if test.get("reportUrl") else mark)
        rows.append(f"| `{spec}` | " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


def skips_section(tests):
    skipped = [t for t in tests if t["status"] == "skipped"]
    if not skipped:
        return ""
    rows = ["### ⚠️ Not validated on all platforms", "",
            "These doc-tests could not run because the release set pins an "
            "artifact that is not published for that platform.", "",
            "| Doc-test | Platform | Reason |", "|---|---|---|"]
    for test in skipped:
        rows.append(f"| `{test['spec']}` "
                    f"| {PLATFORM_LABELS.get(test['platform'], test['platform'])} "
                    f"| {test.get('reason', '')} |")
    return "\n".join(rows) + "\n"


def render(lock):
    tests = lock.get("tests", [])
    failed = [t for t in tests if t["status"] == "failed"]

    parts = [
        f"# Logos Release Set {lock['version']}",
        "",
        lock.get("description", ""),
        "",
        "Every artifact below was resolved to an immutable commit and validated "
        "together. `release-set.lock.json` (attached) carries the full machine-"
        "readable provenance: repository URL, commit, tag, download URL, "
        "checksum and producing CI run for every entry.",
        "",
        "## Validation",
        "",
        results_table(tests),
        "",
        skips_section(tests),
        "",
        "## Artifacts",
        "",
        binary_table(lock.get("apps"), "Apps"),
        "",
        binary_table(lock.get("devUtils"), "Dev utils"),
        "",
        package_table(lock.get("modules"), "Modules"),
        "",
        package_table(lock.get("uiApps"), "UI apps"),
        "",
        "---",
        "",
        f"Modules and UI apps are installed from "
        f"[{lock['catalog']['repo']}]({lock['catalog']['repoUrl']}) with `lgpd` "
        f"and `lgpm`. The source commit shown for each is the submodule gitlink "
        f"the catalog release was built from.",
    ]
    if failed:
        parts += ["", f"> **{len(failed)} doc-test(s) failed.** See the linked reports."]
    return "\n".join(p for p in parts if p is not None) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lock")
    parser.add_argument("results", nargs="*", help="results-<platform>.json files")
    parser.add_argument("--out-lock", default=None)
    parser.add_argument("--out-notes", default=None)
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args()

    lock = json.load(open(args.lock, encoding="utf-8"))
    if args.generated_at:
        lock["generatedAt"] = args.generated_at

    tests = []
    for path in args.results:
        with open(path, encoding="utf-8") as handle:
            tests.extend(json.load(handle))
    lock["tests"] = sorted(tests, key=lambda t: (t["spec"], t["platform"]))

    if args.out_lock:
        with open(args.out_lock, "w", encoding="utf-8") as handle:
            json.dump(lock, handle, indent=2)
            handle.write("\n")
    notes = render(lock)
    if args.out_notes:
        with open(args.out_notes, "w", encoding="utf-8") as handle:
            handle.write(notes)
    else:
        sys.stdout.write(notes)

    failed = [t for t in lock["tests"] if t["status"] == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

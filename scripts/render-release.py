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


def attach_validation(lock):
    """Give every artifact the doc-tests and report links that covered it.

    The release set promises per-asset build provenance: the CI run that
    produced it (resolved earlier) and the report of the test that exercised it.
    `tests[]` alone is indexed by (spec, platform), so without this an asset has
    no direct pointer to its own evidence.
    """
    selector = _load_select_specs()
    tutorial = lock.get("tutorial") or {}
    # A tutorial spec is evidence for an artifact only if it built against that
    # artifact's repo, at the release set's version.
    pinned = {pin["repo"] for pin in tutorial.get("pins", [])
              if pin["source"].startswith("release set")}
    spec_repos = tutorial.get("specRepos", {})
    for group in ("apps", "devUtils", "tools", "modules", "uiApps"):
        for item in lock.get(group, []):
            tutorial_specs = []
            if item.get("repo") in pinned:
                tutorial_specs = sorted(spec for spec, repos in spec_repos.items()
                                        if item["repo"] in repos)
            # Per platform: a spec's Windows half exercises less than the rest.
            item["validatedBy"] = [
                {
                    "spec": test["spec"],
                    "platform": test["platform"],
                    "status": test["status"],
                    "reportUrl": test.get("reportUrl"),
                }
                for test in lock.get("tests", [])
                if test["spec"] in tutorial_specs
                or test["spec"] in selector.specs_covering(item["name"], lock, test["platform"])
            ]

PLATFORM_LABELS = {
    "linux-x86_64": "linux x86_64",
    "linux-arm64": "linux arm64",
    "macos-arm64": "macOS arm64",
    "windows-x86_64": "windows x86_64",
}


def platform_order(platform):
    return list(PLATFORM_LABELS).index(platform) if platform in PLATFORM_LABELS else 99

STATUS_MARK = {"passed": "✅", "failed": "❌", "skipped": "⏭️"}


def short(commit):
    return (commit or "")[:10] or "—"


def link(text, url, title=None):
    if not url:
        return text
    return f'[{text}]({url} "{title}")' if title else f"[{text}]({url})"


# The artifact tables' platform columns. Distinct from the validation table's
# ✅/❌, which say whether a doc-test passed.
AVAILABLE, MISSING = "✔️", "✖️"


def table_head(columns):
    return ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]


def commit_link(item):
    return link(f"`{short(item.get('commit'))}`",
                f"{item['repoUrl']}/commit/{item['commit']}"
                if item.get("commit") and item.get("repoUrl") else None)


def binary_table(items, title):
    """Apps, dev utils and tools: downloads per platform, or a Nix flake."""
    if not items:
        return ""
    binaries = [i for i in items if i.get("consumedAs") != "flake"]
    flakes = [i for i in items if i.get("consumedAs") == "flake"]
    rows = [f"### {title}", ""]
    if binaries:
        rows += table_head(["Component", "Version", "Commit"] + list(PLATFORM_LABELS.values()))
        for item in binaries:
            # Each mark links to that platform's download; hovering names the file.
            cells = [" ".join(link(AVAILABLE, a["url"], a["name"]) for a in item.get("assets", [])
                              if a.get("platform") == platform) or MISSING
                     for platform in PLATFORM_LABELS]
            rows.append(f"| {link(item['name'], item.get('repoUrl'))} "
                        f"| {link(item.get('tag') or '—', item.get('releaseUrl'))} "
                        f"| {commit_link(item)} | " + " | ".join(cells) + " |")
    if flakes:
        if binaries:
            rows.append("")
        rows += table_head(["Component", "Version", "Commit", "Nix flake"])
        for item in flakes:
            rows.append(f"| {link(item['name'], item.get('repoUrl'))} "
                        f"| {link(item.get('tag') or '—', item.get('releaseUrl'))} "
                        f"| {commit_link(item)} | `{item.get('flakeRef', '')}` |")
    return "\n".join(rows) + "\n"


def package_table(items, title):
    """Modules and UI apps: one .lgx each, carrying a variant per platform."""
    if not items:
        return ""
    rows = [f"### {title}", ""]
    rows += table_head(["Component", "Version", "Commit", "Package"] + list(PLATFORM_LABELS.values()))
    for item in items:
        have = set(item.get("platforms") or [])
        cells = [AVAILABLE if platform in have else MISSING for platform in PLATFORM_LABELS]
        lgx = (item.get("lgx") or {}).get("url")
        rows.append(f"| {link(item['name'], item.get('repoUrl'))} "
                    f"| {link(item['version'], item.get('catalogReleaseUrl'))} "
                    f"| {commit_link(item)} | {link('.lgx', lgx)} | " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


def results_table(tests):
    if not tests:
        return "_No doc-tests were run._\n"
    platforms = sorted({t["platform"] for t in tests}, key=platform_order)
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


def windows_note(tests):
    if not any(t["platform"] == "windows-x86_64" and t["status"] != "skipped" for t in tests):
        return ""
    return ("Nix does not run on Windows, so each Windows leg is built on Linux and "
            "run on `windows-latest`, executing the steps its spec marks for Windows. "
            "The release set's own legs run the released `logosctl` zip and the "
            "catalog's modules, with each spec's probe cross-built by the pinned "
            "builder.\n")


def skips_section(tests):
    skipped = [t for t in tests if t["status"] == "skipped"]
    if not skipped:
        return ""
    rows = ["### ⚠️ Not validated on all platforms", "",
            "These doc-tests did not run on every platform — usually because "
            "the release set pins an artifact that is not published for it.", "",
            "| Doc-test | Platform | Reason |", "|---|---|---|"]
    for test in skipped:
        rows.append(f"| `{test['spec']}` "
                    f"| {PLATFORM_LABELS.get(test['platform'], test['platform'])} "
                    f"| {test.get('reason', '')} |")
    return "\n".join(rows) + "\n"


def tutorial_section(tutorial):
    if not tutorial:
        return ""
    tag = f"`{tutorial['tag']}` " if tutorial.get("tag") else ""
    commit = link(f"`{short(tutorial['commit'])}`",
                  f"{tutorial['repoUrl']}/commit/{tutorial['commit']}")
    rows = ["### Tutorial", "",
            f"{link(tutorial['name'], tutorial['repoUrl'])} {tag}@ {commit}. "
            "Its specs (`tutorial-*` above) built against these repos — the "
            "release set's own version wherever it pins one:", "",
            "| Repo | Ref | Commit | From |", "|---|---|---|---|"]
    for pin in tutorial["pins"]:
        shown = pin["ref"] if pin["ref"] != pin["commit"] else short(pin["commit"])
        pin_commit = link(f"`{short(pin['commit'])}`",
                          f"{pin['repoUrl']}/commit/{pin['commit']}")
        rows.append(f"| {link(pin['name'], pin['repoUrl'])} | `{shown}` "
                    f"| {pin_commit} | {pin['source']} |")
    return "\n".join(rows) + "\n"


def render(lock):
    tests = lock.get("tests", [])
    failed = [t for t in tests if t["status"] == "failed"]

    # Versions first: they are what a reader comes for. The evidence follows.
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
        "## Artifacts",
        "",
        f"{AVAILABLE} published for that platform, {MISSING} not. An app's "
        f"{AVAILABLE} links to that platform's download.",
        "",
        binary_table(lock.get("apps"), "Apps"),
        "",
        binary_table(lock.get("devUtils"), "Dev utils"),
        "",
        binary_table(lock.get("tools"), "Tools"),
        "",
        package_table(lock.get("modules"), "Modules"),
        "",
        package_table(lock.get("uiApps"), "UI apps"),
        "",
        f"Modules and UI apps are installed from "
        f"[{lock['catalog']['repo']}]({lock['catalog']['repoUrl']}) with "
        f"`logosctl` (`lgpm` installs them into Basecamp's user directory). "
        f"For each, the commit is the submodule gitlink the catalog release was "
        f"built from, and {AVAILABLE} marks a platform its `.lgx` carries a variant for.",
        "",
        "## Validation",
        "",
        results_table(tests),
        "",
        windows_note(tests),
        skips_section(tests),
        "",
        tutorial_section(lock.get("tutorial")),
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
    attach_validation(lock)

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
    passed = [t for t in lock["tests"] if t["status"] == "passed"]
    if failed:
        print(f"{len(failed)} doc-test(s) failed", file=sys.stderr)
        return 1
    if not passed:
        # "If everything succeeds, publish" must not degrade into "if nothing
        # failed, publish". A set whose every spec was skipped on every platform
        # has been validated by nothing at all.
        print("no doc-test passed on any platform — nothing was actually "
              "validated, refusing to publish", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

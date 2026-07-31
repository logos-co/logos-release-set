#!/usr/bin/env python3
"""Rewrite release-set.json in its canonical layout.

`release-set.json` is hand-edited, and it is laid out for reading: one entry
per line, columns aligned, groups separated by blank lines. `json.dump` cannot
reproduce that — it reflows every entry onto four lines each, which turns a
two-line pin bump into a ninety-line diff and buries the actual change.

So any tool that edits this file should hand it back to this formatter, and CI
checks that what is committed is already canonical. The output is a pure
function of the data: format twice, get the same bytes.

Entries are sorted by name within each group, so an added component lands in a
predictable place instead of wherever the editing script happened to append it.

    python3 scripts/format-release-set.py                  # rewrite in place
    python3 scripts/format-release-set.py --check          # exit 1 if not canonical
    python3 scripts/format-release-set.py --stdout         # print, don't write
"""

import argparse
import json
import sys

# Groups pinned by GitHub release tag share one set of column widths; groups
# pinned by catalog version share another. That is what makes `apps` and
# `devUtils` line up with each other, and `modules` with `uiApps`.
TAG_GROUPS = ("apps", "devUtils")
VERSION_GROUPS = ("modules", "uiApps")
ORDER = ("apps", "devUtils", "modules", "uiApps")


def q(value):
    """A JSON string, quoted exactly as json.dumps would."""
    return json.dumps(value, ensure_ascii=False)


def entry_line(item, widths, kind):
    """One `{ ... }` entry, padded to the group's column widths."""
    name = f'"name": {q(item["name"])},'
    if kind == "tag":
        repo = f'"repo": {q(item["repo"])},'
        return (f'    {{ {name:<{widths["name"]}} '
                f'{repo:<{widths["repo"]}} '
                f'"releaseTag": {q(item["releaseTag"])} }}')
    return f'    {{ {name:<{widths["name"]}} "version": {q(item["version"])} }}'


def widths_for(spec, groups, kind):
    """Column widths shared across a family of groups."""
    names, repos = [], []
    for group in groups:
        for item in spec.get(group, []):
            names.append(f'"name": {q(item["name"])},')
            if kind == "tag":
                repos.append(f'"repo": {q(item["repo"])},')
    return {
        "name": max((len(x) for x in names), default=0),
        "repo": max((len(x) for x in repos), default=0),
    }


def render(spec):
    tag_widths = widths_for(spec, TAG_GROUPS, "tag")
    version_widths = widths_for(spec, VERSION_GROUPS, "version")

    out = ["{"]
    for key in ("schemaVersion", "version", "name", "displayName", "description"):
        if key in spec:
            out.append(f"  {q(key)}: {json.dumps(spec[key], ensure_ascii=False)},")

    catalog = spec.get("catalog")
    if catalog is not None:
        out.append("")
        out.append('  "catalog": {')
        items = list(catalog.items())
        for i, (k, v) in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            out.append(f"    {q(k)}: {json.dumps(v, ensure_ascii=False)}{comma}")
        out.append("  },")

    for gi, group in enumerate(ORDER):
        if group not in spec:
            continue
        kind = "tag" if group in TAG_GROUPS else "version"
        widths = tag_widths if kind == "tag" else version_widths
        entries = sorted(spec[group], key=lambda item: item["name"])
        out.append("")
        out.append(f"  {q(group)}: [")
        for i, item in enumerate(entries):
            comma = "," if i < len(entries) - 1 else ""
            out.append(entry_line(item, widths, kind) + comma)
        last_group = gi == len(ORDER) - 1
        out.append("  ]" + ("" if last_group else ","))

    out.append("}")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", nargs="?", default="release-set.json")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the file is not already canonical")
    parser.add_argument("--stdout", action="store_true",
                        help="write to stdout instead of the file")
    args = parser.parse_args()

    with open(args.spec, encoding="utf-8") as handle:
        original = handle.read()
    spec = json.loads(original)
    formatted = render(spec)

    # The formatter must never change the data — only its layout and, within a
    # group, the order of entries. Compare with every group sorted so the
    # intended reorder passes while a dropped entry or an edited value does not.
    def comparable(doc):
        doc = dict(doc)
        for group in ORDER:
            if group in doc:
                doc[group] = sorted(doc[group], key=lambda item: item["name"])
        return doc

    if comparable(json.loads(formatted)) != comparable(spec):
        print("error: formatting changed the data — refusing to write", file=sys.stderr)
        return 2

    if args.check:
        if formatted != original:
            print(f"error: {args.spec} is not canonically formatted.\n"
                  f"       Run: python3 scripts/format-release-set.py {args.spec}",
                  file=sys.stderr)
            return 1
        print(f"{args.spec}: canonical", file=sys.stderr)
        return 0

    if args.stdout:
        sys.stdout.write(formatted)
        return 0

    if formatted == original:
        print(f"{args.spec}: already canonical", file=sys.stderr)
        return 0
    with open(args.spec, "w", encoding="utf-8") as handle:
        handle.write(formatted)
    print(f"{args.spec}: reformatted", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

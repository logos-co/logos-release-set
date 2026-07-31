#!/usr/bin/env python3
"""Generate the landing page for the published doc-test reports.

The workflow publishes each run's reports under `<run_id>/<platform>/<spec>/`
on gh-pages, which is fine for the per-cell links in a release description and
useless if you just open the site: there is nothing at the root.

This walks what is actually on gh-pages and writes an index over it. It reads
the branch rather than a local directory, so it describes every run ever
published, not only the one that happens to be building.

Runs published before the per-spec job split have the older
`<run_id>/<platform>/` layout; both are rendered.

    python3 scripts/render-pages-index.py --repo logos-co/logos-release-set
    python3 scripts/render-pages-index.py --repo ... -o root/index.html
"""

import argparse
import base64
import collections
import html
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def api(path):
    req = urllib.request.Request(f"{API}{path}", headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "logos-release-set-pages-index",
    })
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def collect(repo, branch):
    """{run_id: {"platforms": {platform: [spec, ...]}, "meta": {...}}}"""
    tree = api(f"/repos/{repo}/git/trees/{branch}?recursive=1")
    if tree is None:
        sys.exit(f"no {branch} branch on {repo} — nothing published yet")
    if tree.get("truncated"):
        print("warning: tree listing truncated; index may be incomplete",
              file=sys.stderr)

    runs = collections.defaultdict(lambda: {"platforms": collections.defaultdict(list),
                                            "meta": None})
    for entry in tree.get("tree", []):
        if entry["type"] != "blob":
            continue
        parts = entry["path"].split("/")
        if not parts[0].isdigit():
            continue
        run = parts[0]
        if parts[-1] == "meta.json" and len(parts) == 2:
            blob = api(f"/repos/{repo}/contents/{entry['path']}?ref={branch}")
            if blob and blob.get("encoding") == "base64":
                try:
                    runs[run]["meta"] = json.loads(base64.b64decode(blob["content"]))
                except json.JSONDecodeError:
                    pass
            continue
        if parts[-1] != "index.html":
            continue
        if len(parts) == 4:          # <run>/<platform>/<spec>/index.html
            runs[run]["platforms"][parts[1]].append(parts[2])
        elif len(parts) == 3:        # <run>/<platform>/index.html  (pre-split)
            runs[run]["platforms"][parts[1]].append("")
    return runs


CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.5 system-ui, sans-serif; margin: 0 auto; padding: 2.5rem 1.25rem;
       max-width: 60rem; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 2rem 0 .5rem; font-weight: 600; }
p.sub { margin: 0 0 2rem; opacity: .7; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 0; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid rgba(128,128,128,.25);
         vertical-align: top; }
th { font-weight: 600; opacity: .75; font-size: .85rem; }
td.spec { white-space: nowrap; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
@media (prefers-color-scheme: dark) { a { color: #6ea8fe; } }
code { font-size: .9em; opacity: .8; }
.meta { opacity: .7; font-size: .9rem; margin: .15rem 0 0; }
"""


def render(repo, runs):
    out = ['<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>Logos release-set doc-test reports</title>",
           f"<style>{CSS}</style>",
           "<h1>Logos release-set doc-test reports</h1>",
           f'<p class="sub">Every validation run published from '
           f'<a href="https://github.com/{html.escape(repo)}">{html.escape(repo)}</a>, '
           f"newest first.</p>"]

    if not runs:
        out.append("<p>No reports published yet.</p>")
        return "\n".join(out) + "\n"

    for run in sorted(runs, key=int, reverse=True):
        data = runs[run]
        meta = data["meta"] or {}
        version = meta.get("version")
        title = f"Release set {html.escape(version)}" if version else f"Run {run}"
        out.append(f'<h2>{title}</h2>')

        bits = [f'<a href="https://github.com/{html.escape(repo)}/actions/runs/{run}">'
                f"run {run}</a>"]
        if meta.get("ref"):
            bits.append(f"<code>{html.escape(meta['ref'])}</code>")
        if meta.get("generatedAt"):
            bits.append(html.escape(meta["generatedAt"]))
        out.append(f'<p class="meta">{" · ".join(bits)}</p>')

        platforms = sorted(data["platforms"])
        specs = sorted({s for ss in data["platforms"].values() for s in ss})

        out.append("<table>")
        out.append("<tr><th>Doc-test</th>"
                   + "".join(f"<th>{html.escape(p)}</th>" for p in platforms)
                   + "</tr>")
        for spec in specs:
            label = html.escape(spec) if spec else "all specs (combined report)"
            cells = []
            for platform in platforms:
                if spec in data["platforms"][platform]:
                    href = f"{run}/{platform}/{spec}/" if spec else f"{run}/{platform}/"
                    cells.append(f'<td><a href="{href}">report</a></td>')
                else:
                    cells.append("<td>—</td>")
            out.append(f'<tr><td class="spec">{label}</td>' + "".join(cells) + "</tr>")
        out.append("</table>")

    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--branch", default="gh-pages")
    parser.add_argument("-o", "--output", default="-")
    args = parser.parse_args()

    runs = collect(args.repo, args.branch)
    page = render(args.repo, runs)

    if args.output == "-":
        sys.stdout.write(page)
    else:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(page)
        print(f"wrote {args.output} ({len(runs)} run(s))", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

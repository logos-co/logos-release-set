#!/usr/bin/env python3
"""Resolve a release-set.json into a fully-provenanced release-set.lock.json.

The input (`release-set.json`) carries the minimum a human types: a release tag
per app/dev-util, a version per catalog package. Everything else — repository
URL, commit hash, download URLs, checksums, the CI run that produced each
artifact, and which platforms each artifact covers — is derived here.

The interesting part is catalog provenance. A catalog package is published by
`logos-modules-release` as a GitHub release tagged `<name>-v<version>`; the
commit that release points at has the module's source repo pinned as a git
submodule. So:

    (name, version)
        -> publisherRef            "<name>-v<version>"
        -> catalog commit          git/ref/tags/<publisherRef>
        -> submodule gitlink sha   contents/submodules?ref=<catalogCommit>
        -> source repo URL         .gitmodules at <catalogCommit>

The module-name -> submodule-directory mapping is NOT guessed from naming
conventions (the real directories include `logos-execution-zone-module` for
package `logos_execution_zone`, and `lez-indexer-module` with no `logos-`
prefix at all). It is read from each submodule's own `metadata.json` at its
pinned commit, which is the source of truth.

Usage:
    python3 scripts/resolve.py release-set.json -o release-set.lock.json

Set GITHUB_TOKEN (or GH_TOKEN) — the unauthenticated API allowance of 60
requests/hour is nowhere near enough.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

PLACEHOLDER = "CHANGE-ME"

API = "https://api.github.com"

# The three platforms a release set is validated on.
PLATFORMS = ["linux-x86_64", "linux-arm64", "macos-arm64"]

# Catalog .lgx variant name per platform (manifest `main` keys).
LGX_VARIANT = {
    "linux-x86_64": "linux-amd64",
    "linux-arm64": "linux-arm64",
    "macos-arm64": "darwin-arm64",
}

PLATFORM_ORDER = {name: i for i, name in enumerate(PLATFORMS)}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class GitHubError(RuntimeError):
    pass


def _request(url, accept="application/vnd.github+json", retries=3):
    headers = {"Accept": accept, "User-Agent": "logos-release-set-resolver"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                return None
            # Secondary rate limit / transient 5xx: back off and retry.
            if exc.code in (403, 429, 500, 502, 503) and attempt < retries - 1:
                if exc.code == 403 and not token:
                    raise GitHubError(
                        f"403 from {url} and no GITHUB_TOKEN is set — the "
                        "unauthenticated rate limit is 60 requests/hour. "
                        "Export GITHUB_TOKEN and re-run."
                    ) from exc
                time.sleep(2 ** attempt * 3)
                continue
            raise GitHubError(f"HTTP {exc.code} for {url}: {exc.read()[:200]!r}") from exc
        except urllib.error.URLError as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 3)
                continue
            raise GitHubError(f"network error for {url}: {exc}") from exc
    raise GitHubError(f"giving up on {url}: {last}")


def get_json(url):
    raw = _request(url)
    return None if raw is None else json.loads(raw)


def api(path):
    return get_json(f"{API}{path}")


_contents_cache = {}


def get_file(repo, path, ref):
    """Fetch a text file from a repo at a ref. Returns str or None."""
    key = (repo, path, ref)
    if key in _contents_cache:
        return _contents_cache[key]
    data = api(f"/repos/{repo}/contents/{path}?ref={ref}")
    text = None
    if data and data.get("encoding") == "base64":
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    _contents_cache[key] = text
    return text


# --------------------------------------------------------------------------
# Generic GitHub resolution
# --------------------------------------------------------------------------

def resolve_tag_commit(repo, tag):
    """Return the commit sha a tag points at, dereferencing annotated tags."""
    ref = api(f"/repos/{repo}/git/ref/tags/{tag}")
    if not ref:
        raise GitHubError(f"{repo}: no tag {tag!r}")
    obj = ref["object"]
    if obj["type"] == "tag":
        annotated = api(f"/repos/{repo}/git/tags/{obj['sha']}")
        return annotated["object"]["sha"]
    return obj["sha"]


def ci_run_url(repo, sha, prefer=None, before=None):
    """The workflow run that BUILT this commit.

    Not simply the newest run at that sha. A catalog repo runs a periodic
    "Rebuild index" cron on its default branch, so the most recent run at a
    release commit is usually that cron — identical for every package sharing
    the commit, and telling you nothing about how any artifact was built.

    `prefer` is a workflow-name prefix to favour (e.g. "Release logos-storage-
    module"); `before` is an ISO timestamp the run must not post-date, so a
    later re-run cannot be mistaken for the original build.
    """
    try:
        runs = api(f"/repos/{repo}/actions/runs?head_sha={sha}&per_page=50")
    except GitHubError:
        return None
    items = (runs or {}).get("workflow_runs") or []
    if not items:
        return None

    # Scheduled runs are housekeeping, never a build of a specific artifact.
    candidates = [r for r in items if r.get("event") != "schedule"] or items

    if prefer:
        named = [r for r in candidates
                 if (r.get("name") or "").lower().startswith(prefer.lower())]
        if named:
            candidates = named

    if before:
        # Runs come newest-first; take the newest that does not post-date the
        # artifact it supposedly produced.
        not_after = [r for r in candidates if (r.get("created_at") or "") <= before]
        if not_after:
            candidates = not_after

    return candidates[0].get("html_url")


# Preference among several assets for the same platform: plain tarballs are the
# easiest to consume, then AppImages, then macOS app bundles and disk images.
# Without an explicit order the pick is whatever order GitHub returned.
ASSET_PREFERENCE = [".app.tar.gz", ".dmg", ".pkg", ".appimage", ".tar.gz", ".tgz"]


def asset_rank(name):
    low = name.lower()
    if low.endswith(".tar.gz") and not low.endswith(".app.tar.gz"):
        return 0
    if low.endswith(".appimage"):
        return 1
    if low.endswith(".app.tar.gz") or low.endswith(".app.zip"):
        return 2
    if low.endswith(".dmg"):
        return 3
    return 9


def classify_asset(name):
    """Map a release-asset filename to one of PLATFORMS, or None.

    Handles both naming schemes in use: the CLI tools' `<tool>-<arch>-<os>.tar.gz`
    and Basecamp's `LogosBasecamp-Desktop-v<ver>-<sha>-<arch>.{AppImage,dmg}`.
    """
    low = name.lower()
    # `.app` bundles and `.pkg` installers are macOS artifacts even when the
    # filename says only the architecture — logos-basecamp publishes
    # `logos-basecamp-aarch64-unsigned.app.tar.gz` beside its Linux AppImage,
    # and without this a linux-arm64 runner would download a Mach-O bundle.
    is_mac = (".dmg" in low or "macos" in low or "darwin" in low
              or ".app.tar" in low or ".app.zip" in low or low.endswith(".pkg"))
    is_arm = "aarch64" in low or "arm64" in low
    is_x86 = "x86_64" in low or "amd64" in low

    if is_mac:
        return "macos-arm64" if is_arm else None
    if is_arm:
        return "linux-arm64"
    if is_x86:
        return "linux-x86_64"
    return None


def asset_entry(asset):
    entry = {
        "name": asset["name"],
        "url": asset["browser_download_url"],
        "size": asset.get("size"),
        "platform": classify_asset(asset["name"]),
    }
    # `digest` looks like "sha256:abc..." when GitHub has one. It often does not;
    # the field is then simply absent — we never download a file just to hash it.
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        entry["sha256"] = digest.split(":", 1)[1]
    return entry


def resolve_binary_repo(name, repo, tag, kind):
    """Resolve an app / dev-util pinned by GitHub release tag."""
    commit = resolve_tag_commit(repo, tag)
    release = api(f"/repos/{repo}/releases/tags/{tag}")
    assets = [asset_entry(a) for a in (release or {}).get("assets", [])]
    # Sort so that "the first asset for platform X" is always the best one —
    # consumers (doctests/release-set.sh, the release notes) take the first match.
    assets.sort(key=lambda a: (PLATFORM_ORDER.get(a["platform"], 99), asset_rank(a["name"])))

    entry = {
        "name": name,
        "repo": repo,
        "repoUrl": f"https://github.com/{repo}",
        "tag": tag,
        "commit": commit,
        "releaseUrl": (release or {}).get("html_url"),
        "publishedAt": (release or {}).get("published_at"),
        "ciRunUrl": ci_run_url(repo, commit),
        "assets": assets,
        "platforms": sorted({a["platform"] for a in assets if a["platform"]}),
    }
    if kind == "devUtil" and not assets:
        # module-builder publishes no binaries: it is consumed as a flake ref
        # pinned to the tag. Absent assets are expected, not a coverage gap.
        entry["consumedAs"] = "flake"
        entry["flakeRef"] = f"github:{repo}/{tag}"
    return entry


# --------------------------------------------------------------------------
# Catalog resolution
# --------------------------------------------------------------------------

def load_catalog_index(logos_repo_url):
    raw = _request(logos_repo_url, accept="application/json")
    if raw is None:
        raise GitHubError(f"could not fetch {logos_repo_url}")
    logos_repo = json.loads(raw)
    index_url = logos_repo["indexUrl"]
    index_raw = _request(index_url, accept="application/json")
    if index_raw is None:
        raise GitHubError(f"could not fetch index {index_url}")
    return logos_repo, index_url, json.loads(index_raw)


def parse_gitmodules(text):
    """path -> url, from a .gitmodules file."""
    out, path = {}, None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("path"):
            path = line.split("=", 1)[1].strip()
        elif line.startswith("url") and path:
            out[path] = line.split("=", 1)[1].strip()
            path = None
    return out


_module_map_cache = {}
_metadata_cache = {}


def module_map(catalog_repo, catalog_commit):
    """{module name -> {dir, repo, repoUrl, commit}} at a catalog commit.

    Two calls give every submodule directory with its gitlink sha and URL; the
    module name then comes from each submodule's own metadata.json. No guessing.
    """
    if catalog_commit in _module_map_cache:
        return _module_map_cache[catalog_commit]

    listing = api(f"/repos/{catalog_repo}/contents/submodules?ref={catalog_commit}") or []
    urls = parse_gitmodules(get_file(catalog_repo, ".gitmodules", catalog_commit))

    mapping = {}
    for item in listing:
        directory, sha = item.get("name"), item.get("sha")
        # The directory listing reports submodules as type "file"; the .gitmodules
        # entry is what actually confirms one, and gives us its URL.
        url = urls.get(f"submodules/{directory}")
        if not url or not sha:
            continue
        slug = url.rstrip("/").removesuffix(".git").split("github.com/")[-1]

        cache_key = (slug, sha)
        if cache_key not in _metadata_cache:
            text = get_file(slug, "metadata.json", sha)
            name = None
            if text:
                try:
                    name = json.loads(text).get("name")
                except json.JSONDecodeError:
                    name = None
            _metadata_cache[cache_key] = name
        name = _metadata_cache[cache_key]
        if not name:
            continue

        mapping[name] = {
            "dir": directory,
            "repo": slug,
            "repoUrl": f"https://github.com/{slug}",
            "commit": sha,
        }

    _module_map_cache[catalog_commit] = mapping
    return mapping


_tags_cache = {}


def source_tag_for(repo, commit):
    """The tag in a source repo pointing at `commit`, or None if untagged.

    /tags reports the commit sha directly, so annotated tags need no extra
    dereference round-trip.
    """
    if repo not in _tags_cache:
        try:
            _tags_cache[repo] = api(f"/repos/{repo}/tags?per_page=100") or []
        except GitHubError:
            _tags_cache[repo] = []
    for tag in _tags_cache[repo]:
        if (tag.get("commit") or {}).get("sha") == commit:
            return tag.get("name")
    return None


def find_index_version(index, name, version):
    for pkg in index.get("packages", []):
        if pkg.get("name") != name:
            continue
        for ver in pkg.get("versions", []):
            if (ver.get("manifest") or {}).get("version") == version:
                return ver
    return None


def resolve_catalog_package(name, version, catalog_repo, index):
    entry_index = find_index_version(index, name, version)
    if entry_index is None:
        raise GitHubError(
            f"{name}@{version} is not in the catalog index — check the version, "
            f"or run `lgpd info {name}` to list what is published"
        )

    publisher_ref = entry_index.get("publisherRef") or f"{name}-v{version}"
    catalog_commit = resolve_tag_commit(catalog_repo, publisher_ref)
    source = module_map(catalog_repo, catalog_commit).get(name)
    if source is None:
        # The release set promises a commit for every artifact. Degrading to a
        # null here would publish a lock — and release notes — with no
        # provenance for this package, and exit 0 while doing it.
        raise GitHubError(
            f"{name}@{version}: no submodule of {catalog_repo}@{catalog_commit} "
            f"has a metadata.json declaring name {name!r}, so its source commit "
            "cannot be determined"
        )

    manifest = entry_index.get("manifest") or {}
    variants = sorted((manifest.get("main") or {}).keys())
    platforms = sorted(p for p, v in LGX_VARIANT.items() if v in variants)

    return {
        "name": name,
        "version": version,
        "type": manifest.get("type"),
        "publisherRef": publisher_ref,
        "repo": source["repo"],
        "repoUrl": source["repoUrl"],
        "commit": source["commit"],
        # A source repo may or may not tag the commit the catalog built from.
        # Report the tag when one exists; null means genuinely untagged, not
        # "we didn't look".
        "tag": source_tag_for(source["repo"], source["commit"]),
        "submoduleDir": source["dir"],
        "catalogCommit": catalog_commit,
        "catalogReleaseUrl": f"https://github.com/{catalog_repo}/releases/tag/{publisher_ref}",
        # The catalog's per-module release workflow is named "Release <dir>";
        # without that hint every package at this commit would report the
        # periodic index-rebuild cron instead.
        "ciRunUrl": ci_run_url(catalog_repo, catalog_commit,
                               prefer=f"Release {source['dir']}",
                               before=entry_index.get("releasedAt")),
        "lgx": {
            "url": entry_index.get("url"),
            "size": entry_index.get("size"),
            "sha256": entry_index.get("sha256"),
            "rootHash": entry_index.get("rootHash"),
        },
        "variants": variants,
        "platforms": platforms,
        "releasedAt": entry_index.get("releasedAt"),
    }


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def check_version_shape(version):
    """The release set's own version must be A.B.C.D — it becomes the tag."""
    if version == PLACEHOLDER:
        return None
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version or ""):
        return (f"version {version!r} is not of the form A.B.C.D "
                "(four dot-separated numbers, e.g. 0.2.1.0)")
    return None


def find_placeholders(spec):
    out = []
    if spec.get("version") == PLACEHOLDER:
        out.append("version")
    for group in ("apps", "devUtils"):
        for item in spec.get(group, []):
            if item.get("releaseTag") == PLACEHOLDER:
                out.append(f"{group}[{item['name']}].releaseTag")
    for group in ("modules", "uiApps"):
        for item in spec.get(group, []):
            if item.get("version") == PLACEHOLDER:
                out.append(f"{group}[{item['name']}].version")
    return out


def platform_coverage(lock):
    """Per-platform gaps, so the workflow can skip a spec and flag it."""
    gaps = []
    for group in ("apps", "devUtils", "modules", "uiApps"):
        for item in lock.get(group, []):
            if item.get("consumedAs") == "flake":
                continue
            covered = set(item.get("platforms") or [])
            for platform in PLATFORMS:
                if platform not in covered:
                    label = item.get("tag") or item.get("version")
                    gaps.append({
                        "component": item["name"],
                        "version": label,
                        "platform": platform,
                        "reason": (
                            f"no {platform} artifact published for "
                            f"{item['name']}@{label}"
                        ),
                    })
    return gaps


def resolve(spec, generated_at=None):
    catalog_repo = spec["catalog"]["repo"]
    logos_repo, index_url, index = load_catalog_index(spec["catalog"]["logosRepoUrl"])

    lock = {
        "schemaVersion": 1,
        "version": spec["version"],
        "name": spec.get("name"),
        "displayName": spec.get("displayName"),
        "description": spec.get("description"),
        "catalog": {
            "repo": catalog_repo,
            "repoUrl": f"https://github.com/{catalog_repo}",
            "name": logos_repo.get("name"),
            "logosRepoUrl": spec["catalog"]["logosRepoUrl"],
            "indexUrl": index_url,
        },
        "platforms": PLATFORMS,
        "apps": [],
        "devUtils": [],
        "modules": [],
        "uiApps": [],
        "tests": [],
    }
    if generated_at:
        lock["generatedAt"] = generated_at

    for kind, group in (("app", "apps"), ("devUtil", "devUtils")):
        for item in spec.get(group, []):
            print(f"  resolving {group}/{item['name']}@{item['releaseTag']}", file=sys.stderr)
            lock[group].append(
                resolve_binary_repo(item["name"], item["repo"], item["releaseTag"], kind)
            )

    for group in ("modules", "uiApps"):
        for item in spec.get(group, []):
            print(f"  resolving {group}/{item['name']}@{item['version']}", file=sys.stderr)
            lock[group].append(
                resolve_catalog_package(item["name"], item["version"], catalog_repo, index)
            )

    lock["platformGaps"] = platform_coverage(lock)
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", nargs="?", default="release-set.json",
                        help="input release-set.json (default: %(default)s)")
    parser.add_argument("-o", "--output", default="-",
                        help="output lock file, or - for stdout (default: %(default)s)")
    parser.add_argument("--allow-placeholders", action="store_true",
                        help="do not fail on CHANGE-ME values (schema check only)")
    parser.add_argument("--check", action="store_true",
                        help="validate pins and exit without resolving anything")
    parser.add_argument("--generated-at", default=None,
                        help="ISO timestamp to stamp into the lock (CI supplies this; "
                             "omitted by default so output is byte-stable)")
    args = parser.parse_args()

    with open(args.spec, encoding="utf-8") as handle:
        spec = json.load(handle)

    placeholders = find_placeholders(spec)
    if placeholders and not args.allow_placeholders:
        print(f"error: {len(placeholders)} unresolved placeholder(s) in {args.spec}:",
              file=sys.stderr)
        for item in placeholders:
            print(f"  {item} is still {PLACEHOLDER}", file=sys.stderr)
        print("\nFill these in on a release branch — main is expected to keep "
              "placeholders.", file=sys.stderr)
        return 1

    shape_error = check_version_shape(spec.get("version"))
    if shape_error:
        print(f"error: {shape_error}", file=sys.stderr)
        return 1

    if args.check:
        print(f"{args.spec}: pins OK", file=sys.stderr)
        return 0

    try:
        lock = resolve(spec, generated_at=args.generated_at)
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(lock, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {args.output}", file=sys.stderr)

    for gap in lock["platformGaps"]:
        print(f"warning: {gap['reason']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

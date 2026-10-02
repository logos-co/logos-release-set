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
package `lez_core`, and `lez-indexer-module` with no `logos-`
prefix at all). It is read from each submodule's own `metadata.json` at its
pinned commit, which is the source of truth. A submodule holding several modules
lists each subdirectory as a `module = <dir>` line in .gitmodules, and then each
`<dir>/metadata.json` names one.

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

# The platforms a release set is VALIDATED on, and reports coverage gaps for.
# Windows runs only the specs with a Windows half (scripts/windows-plan.py) and
# the tutorial's Windows legs; every other spec is a skip there.
PLATFORMS = ["linux-x86_64", "linux-arm64", "macos-arm64", "windows-x86_64"]

# Platforms whose artifacts we can RECOGNISE.
KNOWN_PLATFORMS = PLATFORMS

# Catalog .lgx variant name per platform (manifest `main` keys).
LGX_VARIANT = {
    "linux-x86_64": "linux-amd64",
    "linux-arm64": "linux-arm64",
    "macos-arm64": "darwin-arm64",
    # lgpm computes exactly this and has no alias fallback for it, so the
    # spelling is a contract, not a preference (package_manager_lib.cpp
    # currentPlatformVariant / platformVariantsToTry).
    "windows-x86_64": "windows-x86_64",
}

PLATFORM_ORDER = {name: i for i, name in enumerate(KNOWN_PLATFORMS)}


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
    """Map a release-asset filename to one of KNOWN_PLATFORMS, or None.

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
    # Match "windows", never a bare "win": "darwin" CONTAINS "win", so the short
    # form would classify every macOS asset as Windows.
    is_windows = ("windows" in low or "win64" in low or "mingw" in low
                  or low.endswith(".exe") or low.endswith(".msi"))
    is_arm = "aarch64" in low or "arm64" in low
    is_x86 = "x86_64" in low or "amd64" in low

    # Windows is tested BEFORE the bare-architecture fallbacks. Otherwise
    # `logosctl-x86_64-windows.zip` matches is_x86 and is handed to a Linux
    # runner as `linux-x86_64` — a silent mislabel, not a drop, and the same
    # shape of bug as nix-bundle-lgx calling a cross build "linux-amd64".
    if is_windows:
        return None if is_arm else "windows-x86_64"
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
    if kind in ("devUtil", "tool") and not assets:
        # module-builder, lm and lgx publish no binaries: each is consumed as a
        # flake ref pinned to the tag. Absent assets are expected, not a coverage gap.
        entry["consumedAs"] = "flake"
        entry["flakeRef"] = f"github:{repo}/{tag}"
    return entry


# --------------------------------------------------------------------------
# Tutorial resolution
# --------------------------------------------------------------------------

COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")

# What a tutorial spec builds against: doctest pins each `github:<o>/<r>{release}`
# URL, and a `requires:` chain runs the specs it names first.
RELEASE_URL = re.compile(r"github:([^/\s\"']+)/([^/\s{\"']+)\{release\}")
REQUIRES = re.compile(r"^requires:[ \t]*\n((?:[ \t]+-[ \t]*\S+[ \t]*\n)+)", re.M)


def spec_repos(repo, commit, spec, seen=None):
    """owner/repo slugs a tutorial spec builds against, through its `requires:`."""
    seen = set() if seen is None else seen
    if spec in seen:
        return set()
    seen.add(spec)
    text = get_file(repo, f"tests/{spec}.test.yaml", commit)
    if text is None:
        raise GitHubError(f"{repo}@{commit[:10]}: no tests/{spec}.test.yaml")
    used = {f"{owner}/{name}" for owner, name in RELEASE_URL.findall(text)}
    match = REQUIRES.search(text)
    for line in (match.group(1).splitlines() if match else []):
        required = line.strip().lstrip("-").strip().strip("\"'")
        used |= spec_repos(repo, commit, required.removesuffix(".test.yaml"), seen)
    return used


def resolve_ref_commit(repo, ref):
    """A tag or full commit sha -> (commit, tag or None). Branches are refused:
    a release set pins things that cannot move."""
    if COMMIT_SHA.match(ref):
        if not api(f"/repos/{repo}/commits/{ref}"):
            raise GitHubError(f"{repo}: no commit {ref}")
        return ref, None
    if not api(f"/repos/{repo}/git/ref/tags/{ref}"):
        raise GitHubError(f"{repo}: {ref!r} is neither a tag nor a full commit sha")
    return resolve_tag_commit(repo, ref), ref


def default_branch_commit(repo):
    info = api(f"/repos/{repo}")
    if not info:
        raise GitHubError(f"{repo}: repository not found")
    branch = info["default_branch"]
    return api(f"/repos/{repo}/commits/{branch}")["sha"], branch


def release_set_pins(lock):
    """{repo slug: (ref, commit, label)} for every repo the release set pins.

    Apps, dev utils and tools are pinned by tag, which is what the tutorial is handed.
    Catalog packages have no tag of their own, so their source commit is."""
    pins = {}
    for group in ("apps", "devUtils", "tools", "modules", "uiApps"):
        for item in lock.get(group, []):
            if not item.get("repo") or not item.get("commit"):
                continue
            by_tag = group in ("apps", "devUtils", "tools")
            ref = item["tag"] if by_tag else item["commit"]
            label = f"{group}/{item['name']}@{item['tag'] if by_tag else item['version']}"
            previous = pins.get(item["repo"])
            if previous and previous[1] != item["commit"]:
                # Two packages from one repo at different commits: there is no
                # single version of that repo to build the tutorial against.
                pins[item["repo"]] = (None, None, f"{previous[2]} and {label} disagree")
            elif not previous:
                pins[item["repo"]] = (ref, item["commit"], label)
    return pins


def resolve_tutorial(entry, lock):
    """Pin the tutorial, then every repo its specs build against.

    The tutorial's tutorial-set.json names those repos. A repo the release set
    pins gets the release set's version; any other keeps the tutorial's own
    pin, and one the tutorial leaves on its default branch is frozen at that
    branch's head now, so every platform builds the same commit and the lock
    says which."""
    repo = entry["repo"]
    commit, tag = resolve_ref_commit(repo, entry["ref"])
    text = get_file(repo, "tutorial-set.json", commit)
    if text is None:
        raise GitHubError(f"{repo}@{entry['ref']} has no tutorial-set.json — pin a "
                          "commit that has one")
    tutorial_set = json.loads(text)

    ours = release_set_pins(lock)
    pins = []
    for dep in tutorial_set["repos"]:
        print(f"  resolving tutorial/{dep['name']}", file=sys.stderr)
        if dep["repo"] in ours:
            ref, dep_commit, label = ours[dep["repo"]]
            if ref is None:
                raise GitHubError(f"tutorial dependency {dep['repo']}: {label}")
            source = f"release set ({label})"
        elif dep["ref"]:
            dep_commit, _ = resolve_ref_commit(dep["repo"], dep["ref"])
            ref, source = dep["ref"], "tutorial-set.json"
        else:
            dep_commit, branch = default_branch_commit(dep["repo"])
            ref, source = dep_commit, f"{branch} at resolve time"
        pins.append({
            "name": dep["name"],
            "repo": dep["repo"],
            "repoUrl": f"https://github.com/{dep['repo']}",
            "ref": ref,
            "commit": dep_commit,
            "source": source,
        })

    return {
        "name": repo.split("/")[-1],
        "repo": repo,
        "repoUrl": f"https://github.com/{repo}",
        "ref": entry["ref"],
        "tag": tag,
        "commit": commit,
        "specs": tutorial_set["specs"],
        "platforms": tutorial_set["platforms"],
        # Specs with a Windows leg, and the flake targets logos-windows-ci
        # stages for each. Absent from tutorials older than that leg.
        "windows": tutorial_set.get("windows", []),
        # Per spec, so an artifact is credited only to the specs that built it.
        "specRepos": {spec: sorted(spec_repos(repo, commit, spec))
                      for spec in tutorial_set["specs"]},
        "pins": pins,
    }


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
    """path -> {"url", "modules"}, from a .gitmodules file.

    `modules` holds the submodule's `module = <dir>` lines: the subdirectories
    the catalog builds instead of the submodule root. Empty for a root module.
    """
    out, section = {}, None

    def close():
        if section and section.get("path"):
            out[section["path"]] = {"url": section.get("url"),
                                    "modules": section["modules"]}

    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("["):
            close()
            section = {"modules": []}
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if section is None:
            continue
        if key == "module":
            section["modules"].append(value)
        elif key in ("path", "url"):
            section[key] = value
    close()
    return out


_module_map_cache = {}
_metadata_cache = {}


def metadata_name(slug, path, sha):
    """The `name` in a repo's metadata file at `sha`, or None."""
    cache_key = (slug, path, sha)
    if cache_key not in _metadata_cache:
        text = get_file(slug, path, sha)
        name = None
        if text:
            try:
                name = json.loads(text).get("name")
            except json.JSONDecodeError:
                name = None
        _metadata_cache[cache_key] = name
    return _metadata_cache[cache_key]


def module_map(catalog_repo, catalog_commit):
    """{module name -> {dir, moduleDir, repo, repoUrl, commit}} at a catalog commit.

    Two calls give every submodule directory with its gitlink sha and URL; the
    module name then comes from each submodule's own metadata.json, or from
    each `<moduleDir>/metadata.json` when it holds several. No guessing.
    """
    if catalog_commit in _module_map_cache:
        return _module_map_cache[catalog_commit]

    listing = api(f"/repos/{catalog_repo}/contents/submodules?ref={catalog_commit}") or []
    entries = parse_gitmodules(get_file(catalog_repo, ".gitmodules", catalog_commit))

    mapping = {}
    for item in listing:
        directory, sha = item.get("name"), item.get("sha")
        # The directory listing reports submodules as type "file"; the .gitmodules
        # entry is what actually confirms one, and gives us its URL.
        entry = entries.get(f"submodules/{directory}") or {}
        url = entry.get("url")
        if not url or not sha:
            continue
        slug = url.rstrip("/").removesuffix(".git").split("github.com/")[-1]

        for module_dir in entry.get("modules") or [None]:
            path = f"{module_dir}/metadata.json" if module_dir else "metadata.json"
            name = metadata_name(slug, path, sha)
            if not name:
                continue
            mapping[name] = {
                "dir": directory,
                "moduleDir": module_dir,
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
            f"or run `logosctl package show {name}` to list what is published"
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
        # The subdirectory of a submodule that holds several modules; null when
        # the module is the submodule root.
        "moduleDir": source["moduleDir"],
        "catalogCommit": catalog_commit,
        "catalogReleaseUrl": f"https://github.com/{catalog_repo}/releases/tag/{publisher_ref}",
        # The catalog's per-module release workflow is named "Release <dir>",
        # after the module's own subdirectory when the submodule holds several;
        # without that hint every package at this commit would report the
        # periodic index-rebuild cron instead.
        "ciRunUrl": ci_run_url(catalog_repo, catalog_commit,
                               prefer=f"Release {source['moduleDir'] or source['dir']}",
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
    """Validate the base SemVer the release branch declares."""
    if version == PLACEHOLDER:
        return None
    # SemVer's numeric identifiers must not carry leading zeroes. CI appends the
    # build identifier (`+<short Git SHA>`) from the commit it checked out, so
    # the release-set file itself declares only the reproducible base version.
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
                        r"-r\.(?:0|[1-9]\d*)", version or ""):
        return (f"version {version!r} is not of the form X.Y.Z-r.N "
                "(for example 0.2.1-r.1)")
    return None


def release_version(base_version, release_set_commit):
    """Add CI's immutable release-set commit identity to a base SemVer."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", release_set_commit or ""):
        return (None,
                "release-set commit must be a 7–40 character lowercase Git SHA; "
                f"got {release_set_commit!r}")
    return f"{base_version}+{release_set_commit[:7]}", None


def find_placeholders(spec):
    out = []
    if spec.get("version") == PLACEHOLDER:
        out.append("version")
    for group in ("apps", "devUtils", "tools"):
        for item in spec.get(group, []):
            if item.get("releaseTag") == PLACEHOLDER:
                out.append(f"{group}[{item['name']}].releaseTag")
    for group in ("modules", "uiApps"):
        for item in spec.get(group, []):
            if item.get("version") == PLACEHOLDER:
                out.append(f"{group}[{item['name']}].version")
    if (spec.get("tutorial") or {}).get("ref") == PLACEHOLDER:
        out.append("tutorial.ref")
    return out


def platform_coverage(lock):
    """Per-platform gaps, so the workflow can skip a spec and flag it."""
    gaps = []
    for group in ("apps", "devUtils", "tools", "modules", "uiApps"):
        for item in lock.get(group, []):
            if item.get("consumedAs") == "flake":
                continue
            covered = set(item.get("platforms") or [])
            for platform in PLATFORMS:
                if platform not in covered:
                    # Identify by the field that IS the pin for this group; a
                    # catalog entry's `tag` is its source repo's tag, not its pin.
                    label = item["tag"] if group in ("apps", "devUtils", "tools") else item["version"]
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


def resolve(spec, release_set_commit, generated_at=None):
    catalog_repo = spec["catalog"]["repo"]
    logos_repo, index_url, index = load_catalog_index(spec["catalog"]["logosRepoUrl"])

    lock = {
        "schemaVersion": 1,
        "version": release_version(spec["version"], release_set_commit)[0],
        "releaseSetCommit": release_set_commit,
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
        "tools": [],
        "modules": [],
        "uiApps": [],
        "tests": [],
    }
    if generated_at:
        lock["generatedAt"] = generated_at

    for kind, group in (("app", "apps"), ("devUtil", "devUtils"), ("tool", "tools")):
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

    if spec.get("tutorial"):
        print(f"  resolving tutorial@{spec['tutorial']['ref']}", file=sys.stderr)
        lock["tutorial"] = resolve_tutorial(spec["tutorial"], lock)

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
    parser.add_argument("--release-set-commit", default=None,
                        help="Git SHA of the checked-out release set; CI appends its "
                             "first seven characters to the release version")
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

    resolved_version, commit_error = release_version(spec["version"],
                                                     args.release_set_commit)
    if commit_error:
        print(f"error: {commit_error}", file=sys.stderr)
        return 1

    try:
        lock = resolve(spec, args.release_set_commit, generated_at=args.generated_at)
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

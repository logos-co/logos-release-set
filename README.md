# logos-release-set

A **release set** is the answer to "which versions of everything go together?" —
one file naming the exact release of every Logos component that is published and
supported as a unit, plus the machinery to prove that set actually works.

```
release-set.json          what a human pins   (5 tags + 11 versions)
        │
        │  scripts/resolve.py
        ▼
release-set.lock.json     what CI resolves    (repo url, commit, tag,
        │                                      download url, checksum, CI run)
        │  doctests/*.test.yaml on 3 platforms
        ▼
GitHub release            what users get      (links, reports, the lock)
```

## The input: `release-set.json`

Four groups, two pinning styles:

| Group | Pinned by | What it is |
|---|---|---|
| `apps` | GitHub release **tag** | Basecamp, logoscore, lgpm, lgpd |
| `devUtils` | GitHub release **tag** | logos-module-builder |
| `modules` | catalog **version** | `core` modules — install to `--modules-dir` |
| `uiApps` | catalog **version** | `ui_qml` plugins — install to `--ui-plugins-dir` |

Plus `version`, the release set's own SemVer identifier:
`X.Y.Z-r.<release-number>+<release-set-commit-short-hash>`. It becomes the
release tag (for example, `v0.2.1-r.1+5ecc750`).

`modules` and `uiApps` carry **no repository** — they are catalog packages
identified by `(name, version)`, and the resolver discovers where they came
from. Note these are *module* names, not repo names: the LEZ module is published
as `lez_core`, and two entries (`lez_indexer_module`,
`lez_explorer_ui`) have no repo in the main workspace at all.

**`main` holds `CHANGE-ME` placeholders and never publishes.** To cut a release
set, branch, fill in the versions, and run the workflow on that branch.

```bash
git switch -c release/0.2.1-r.1+5ecc750
$EDITOR release-set.json
python3 scripts/resolve.py release-set.json --check   # fails while any pin is CHANGE-ME
```

The file is laid out for reading — one entry per line, columns aligned — and
`json.dump` cannot reproduce that: it reflows every entry onto four lines, so a
two-line pin bump becomes a ninety-line diff. Any script that edits this file
should hand it back to the formatter, which sorts entries by name and restores
the layout. CI rejects a file that is not already canonical.

```bash
python3 scripts/format-release-set.py           # rewrite in place
python3 scripts/format-release-set.py --check   # what CI runs
```

## The output: `release-set.lock.json`

`scripts/resolve.py` turns each pin into full provenance. Every versioned
artifact records its repository URL, its **commit (always)**, its tag (where one
applies), its download URL, size and checksum, and the CI run that produced it.

For catalog packages the commit is not guesswork — it is the submodule gitlink
in `logos-modules-release` at the package's release tag:

```
(name, version)
  → publisherRef            "storage_module-v2.0.1"
  → catalog commit          git/ref/tags/<publisherRef>          → 3274937…
  → submodule gitlink sha   contents/submodules?ref=<commit>     → 8feb7cc0…
  → source repo url         .gitmodules at <commit>
```

The module-name → submodule-directory mapping is read from each submodule's own
`metadata.json` at its pinned commit, never inferred from naming conventions —
`lez_core` lives in `logos-execution-zone-module`, and the LEZ
directories drop the `logos-` prefix entirely.

`sha256` is best-effort: the catalog publishes one for every `.lgx`, GitHub
release assets often do not. When it is unavailable the field is omitted rather
than fabricated, and nothing is downloaded just to hash it.

```bash
export GITHUB_TOKEN=...        # the API allowance without one is 60 req/hour
python3 scripts/resolve.py release-set.json -o release-set.lock.json
```

## The proof: `doctests/`

Five executable specs, run on **linux-x86_64, linux-arm64 and macOS arm64**.
They use only released artifacts: tools downloaded from GitHub releases at the
pinned tags, modules installed from the catalog with `lgpd` and `lgpm` at the
pinned versions and checksum-verified on the way in.

| Spec | What it proves |
|---|---|
| `headless-storage-module` | Installs, initializes and drives the pinned storage node; a probe module calls it and catches its events; `/metrics` is scraped |
| `headless-delivery-module` | Same for delivery, subscribing before start so `nodeStarted` is deterministic |
| `headless-blockchain-module` | Same for blockchain, joined to the testnet with the operator guide's peer set |
| `basecamp-appimage-smoke` | The **shipped** Basecamp artifact boots on a user-dir full of pinned modules and stays clean |
| `basecamp-ui` | Basecamp's UI actually works, driven headlessly through the QML inspector |

Initialization follows the
[Logos node operator guide](https://roadmap.logos.co/testnets/logos-node-operator-guide),
with one deliberate deviation: CI runners have no public IP or inbound ports, so
the specs use `"nat": "none"` instead of `extip:<public-ip>` and stop short of
faucet funding and blend-network participation.

Each headless spec also builds a small **probe module** with the pinned
`logos-module-builder`, which calls its target and subscribes to one of its
events from inside the runtime. The probe supplies its own contract via
`dependency_overrides`, so it compiles against the interface without building
the target from source — what it talks to at runtime is the downloaded `.lgx`
and nothing else.

Each headless spec then loads the pinned **`openmetrics`** module and scrapes
`/metrics` over HTTP. `openmetrics` is the module that serves the endpoint: it
runs its own HTTP server and, per scrape, calls a metrics convention method on
each configured module over IPC, merging the results into one document with a
`module="<name>"` label per series. A module opts in by implementing either
`collectMetrics()` (structured — `storage_module`) or `collectOpenMetricsText()`
(already rendered, selected with `"format": "text"` — `delivery_module`).
`blockchain_module` implements neither, so there the probe carries the metric
and publishes the block count it observed.

**Everything is installed before the daemon starts.** The runtime scans its
module directories once, at startup, and there is no rescan command — a package
installed while it is running is invisible, and `load-module` fails with
`MODULE_LOAD_FAILED`.

Everything is the **portable** variant, end to end. A dev build RPATHs into
`/nix/store` and will not load beside portable catalog packages, so probes build
`#lgx-portable` and the Basecamp under test is the portable bundle.

### Two things that look like details and are not

**Fetched tools are launched through an `exec` wrapper, never a symlink.**
`logoscore` finds `logos_host` next to its own executable and its bundled
modules at `../modules`. On macOS that lookup uses `_NSGetExecutablePath`, which
reports the path the process was *invoked* with and does not resolve symlinks —
so behind a symlink the runtime searches `./bin`, reports `logos_host not
found`, and **every module load fails**. `exec` replaces the process image, so
the running program's own path is the real one inside the bundle. The same
applies to launching Basecamp, hence `release-set.sh path`.

**`capability_module` is seeded into the modules directory.** It is the auth
handshake every `load-module` needs, and it ships inside the `logoscore` bundle.
Copying it into the directory passed to `-m` makes the runtime independent of
where it thinks its own bundle is.

### Why two Basecamp specs

The QML inspector is a compile-time feature and is **off in every shipping
artifact**, so the released AppImage cannot be driven by a test harness. Rather
than pretend otherwise, the coverage splits: `basecamp-appimage-smoke` launches
the real shipped artifact and asserts it starts clean, and `basecamp-ui`
rebuilds the *same resolved commit* with the inspector on and drives the UI
properly. Together: the binary users download starts, and the code inside it
works.

### Running them locally

```bash
export RELEASE_SET_DIR="$PWD" GITHUB_TOKEN=...
./doctests/run.sh headless-storage-module     # the most deterministic — start here
./doctests/run.sh                             # everything
```

## The workflow

`.github/workflows/release-set.yml`, **manual only** (`workflow_dispatch`):

1. **resolve** — every pin, or fail fast listing each unresolved `CHANGE-ME`.
2. **doctests** — the three platforms in parallel. If the set pins an artifact
   that a platform does not publish, the affected spec is **skipped, not
   failed** — a release with no linux-arm64 AppImage leaves nothing to
   smoke-test there, which is not a failure of the release set. Skipping is
   decided **up front** by `select-specs.py` reading the lock, not discovered
   mid-run: the doc-test runner treats any non-zero exit as a hard failure, so a
   spec that started can only pass or fail.
3. **publish** — only on green, and never from `main`: a release tagged
   `v<version>` carrying `release-set.lock.json` and the per-platform HTML
   reports, with a description that links every binary, every `.lgx`, every
   source commit, and a table of what was skipped and why.

Skipped platforms are always named in the release description. A release that
does not mention a platform passed there. And "everything succeeded" means at
least one doc-test actually passed — a set whose every spec was skipped
everywhere is validated by nothing, so publishing refuses.

The release **links** to artifacts rather than mirroring them — the binaries
already live in their own repos' releases and the `.lgx` files in the catalog,
and Basecamp's AppImages alone are ~270 MB each.

## Repository layout

```
release-set.json                    the input — placeholders on main
scripts/resolve.py                  pins  -> release-set.lock.json
scripts/select-specs.py             which specs can run on a platform, and why not
scripts/render-release.py           lock + results -> release notes
doctests/release-set.sh             fetch/install helper the specs drive
doctests/*.test.yaml                the five specs
doctests/run.sh                     run them locally
.github/workflows/release-set.yml   resolve -> validate -> publish
```

# logos-release-set

A **release set** is the answer to "which versions of everything go together?" —
one file naming the exact release of every Logos component that is published and
supported as a unit, plus the machinery to prove that set actually works.

```
release-set.json          what a human pins   (6 tags + 15 versions + the tutorial)
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

Five groups, two pinning styles:

| Group | Pinned by | What it is |
|---|---|---|
| `apps` | GitHub release **tag** | Basecamp, logosctl, lgpm |
| `devUtils` | GitHub release **tag** | logos-module-builder |
| `tools` | GitHub release **tag** | logos-module (`lm`), logos-package (`lgx`) |
| `modules` | catalog **version** | `core` modules — install to `--modules-dir` |
| `uiApps` | catalog **version** | `ui_qml` plugins — install to `--ui-plugins-dir` |

Plus `tutorial`, the [logos-tutorial](https://github.com/logos-co/logos-tutorial)
commit (or tag) whose specs run as part of the validation — see
[The tutorial](#the-tutorial).

And `version`, the release set's base SemVer identifier:
`X.Y.Z-r.<release-number>` (for example, `0.2.1-r.1`). CI appends
`+<release-set-commit-short-hash>` from the exact commit it checked out, so the
published release tag becomes, for example, `v0.2.1-r.1+5ecc750`.

`modules` and `uiApps` carry **no repository** — they are catalog packages
identified by `(name, version)`, and the resolver discovers where they came
from. Note these are *module* names, not repo names: the LEZ module is published
as `lez_core`, and two entries (`lez_indexer_module`,
`lez_explorer_ui`) have no repo in the main workspace at all.

**`main` holds `CHANGE-ME` placeholders and never publishes.** To cut a release
set, branch, fill in the versions, and run the workflow on that branch.

```bash
git switch -c release/0.2.1-r.1
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
directories drop the `logos-` prefix entirely. A submodule that holds several
modules lists each subdirectory as a `module = <dir>` line in the catalog's
`.gitmodules`, and each `<dir>/metadata.json` names one: both RLN modules come
from `logos-rln-modules`.

`sha256` is best-effort: the catalog publishes one for every `.lgx`, GitHub
release assets often do not. When it is unavailable the field is omitted rather
than fabricated, and nothing is downloaded just to hash it.

```bash
export GITHUB_TOKEN=...        # the API allowance without one is 60 req/hour
python3 scripts/resolve.py release-set.json -o release-set.lock.json \
  --release-set-commit "$(git rev-parse HEAD)"
```

## The proof: `doctests/`

Five executable specs, run on **linux-x86_64, linux-arm64 and macOS arm64**;
every spec but the blockchain one also runs on **Windows x86_64** (see
[Windows](#windows)). They use only released artifacts: tools downloaded from GitHub releases at the
pinned tags, modules installed from the catalog at the pinned versions and
checksum-verified on the way in.

The three headless specs drive **`logosctl`**, which is the runtime and the
package manager in one — it bundles `package_manager` and `package_downloader`,
so a single artifact resolves, installs, loads and calls. The two Basecamp specs
download with `logosctl` too, then install into Basecamp's user directory with
`lgpm`, since `logosctl` installs only into its own session. That is how the
`lgpm` release stays covered.

| Spec | What it proves |
|---|---|
| `headless-storage-module` | Installs the pinned storage module, waits for the node the package downloader starts and drives it; a probe module calls it and catches its events; `/metrics` is scraped |
| `headless-delivery-module` | Same for delivery, subscribing before start so `nodeStarted` is deterministic |
| `headless-blockchain-module` | Same for blockchain, joined to the testnet with the guide's peer set |
| `basecamp-appimage-smoke` | The **shipped** Basecamp artifact boots on a user-dir full of pinned modules and stays clean; on Windows its installer installs it and its uninstaller removes it |
| `basecamp-ui` | Basecamp's UI actually works, driven headlessly through the QML inspector |

Initialization follows the [Run a Logos node](https://docs.logos.co/run-a-node)
guide for Testnet v0.3, with one deliberate deviation: CI runners have no public
IP or inbound ports, so the specs leave out the guide's `"nat": "extip:<public-ip>"`
and stop short of faucet funding and blend-network participation.

Every daemon the specs start gets a `HOME` inside its working directory.
`logosctl`'s package downloader starts a storage node as soon as the daemon is
up, and its default configuration keeps the node's data in `~/.logos_storage`,
which is where Basecamp's storage node keeps it too. A local run therefore never
touches that directory.

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

**The daemon starts before anything is installed.** `logosctl`'s package
commands are served by the `package_manager` module inside the running daemon,
and the daemon re-scans after each install, restarting only what was already
loaded. Basecamp is the other way round: `lgpm` writes into its user directory
before it launches.

Everything is the **portable** variant, end to end. A dev build RPATHs into
`/nix/store` and will not load beside portable catalog packages, so probes build
`#lgx-portable` and the Basecamp under test is the portable bundle.

### Windows

`headless-storage-module`, `headless-delivery-module`, `basecamp-appimage-smoke`
and `basecamp-ui` also run on Windows.
Nix does not run there, so the `doctests-windows` job calls
[logos-windows-ci](https://github.com/logos-co/logos-windows-ci). It builds this
repo's `flake.nix` on Linux and stages each target as a directory beside the
script it generates from the spec's Windows steps:

| Target | What it holds |
|---|---|
| `logosctl` | The pinned release's `logosctl-x86_64-windows.zip`, unpacked: the artifact users download, not a build of it |
| `lgpm` | The same for `lgpm-x86_64-windows.zip` (the Basecamp specs) |
| `basecamp-setup` | The pinned Basecamp release's installer (`basecamp-appimage-smoke`). Staged as `.bin`: logos-windows-ci gates every staged `.exe` as a 64-bit build output, and an NSIS installer is a released 32-bit stub |
| `probe-storage`, `probe-delivery` | The spec's probe, cross-built with the pinned builder from `doctests/probes/` |
| `release-set` | `release-set.json`, and `doctests/windows.sh` to read it: Windows has no lock |
| `bin-bundle-dir-inspector`, `logos-qt-mcp` | `basecamp-ui` only: Basecamp's inspector bundle and test driver, cross-built from its own flake at the pinned commit (`extra-targets`) |

`scripts/windows-plan.py` pins the flake to the release set with
`--override-input` (the builder tag, and the zips' and installer's URLs from the lock) and
decides which specs get a Windows leg. Basecamp's outputs are not inputs of
this flake: the plan hands them to logos-windows-ci as `extra-targets` refs at
the commit the lock resolved, so they build with Basecamp's own lock. On Windows the helper downloads the
pinned modules with `logosctl` and checks each `.lgx` against the `sha256` the
catalog index publishes, the index the resolver reads. `basecamp-ui` installs
only the packages whose index entry has a `windows-x86_64` variant and names
the rest, then asserts the UI apps and core modules among them.
`basecamp-appimage-smoke` installs the same way, then runs the shipped
installer silently into a folder inside the run, launches the installed copy,
and uninstalls it. A silent install registers an uninstaller and adds
shortcuts for the current user, so a Basecamp already installed there would
lose its own: the helper saves them before the install and restores them after
the uninstall. `headless-blockchain-module` has no Windows half
(`blockchain_module` publishes no Windows build), so it is a skip that says
so.

The probe sources live twice: inline in the specs, which build them on Linux and
macOS, and in `doctests/probes/`, which the flake cross-builds.
`scripts/check-probes.py` fails the resolve job if the two differ.

### Two things that look like details and are not

**Fetched tools are launched through an `exec` wrapper, never a symlink.**
`logosctl` finds `logos_host` next to its own executable and its bundled modules
at `../modules`. On macOS that lookup uses `_NSGetExecutablePath`, which reports
the path the process was *invoked* with and does not resolve symlinks — so
behind a symlink the runtime searches `./bin`, reports `logos_host not found`,
and **every module load fails**. `exec` replaces the process image, so the
running program's own path is the real one inside the bundle. The same applies
to launching Basecamp, hence `release-set.sh path`.

**A fetch names the binary it wants, not just the platform.**
`logos-logoscore-cli` publishes `logosctl-*` and `logoscore-*` side by side, so
"the first asset for this platform" is ambiguous and would silently download the
wrong bundle — `cmd_fetch` then dies looking for an executable that is not in
it. `asset_url` prefers the asset whose filename starts with the requested
binary, which is how every repo here names them.

### Why two Basecamp specs

The QML inspector is a compile-time feature and is **off in every shipping
artifact**, so the released AppImage cannot be driven by a test harness. Rather
than pretend otherwise, the coverage splits: `basecamp-appimage-smoke` launches
the real shipped artifact and asserts it starts clean, and `basecamp-ui`
rebuilds the *same resolved commit* with the inspector on and drives the UI
properly. Together: the binary users download starts, and the code inside it
works.

### The tutorial

`"tutorial": { "repo": "logos-co/logos-tutorial", "ref": "<tag or commit>" }`
adds the tutorial's specs to the validation. Its `tutorial-set.json` names every
repo those specs build against, the specs CI runs, and the platforms it runs
them on; the resolver reads it at the pinned commit and pins each repo:

| The repo is… | It is built at… |
|---|---|
| pinned by this release set (`apps`, `devUtils`, `tools`) | the release set's **tag** |
| the source of a catalog package (`modules`, `uiApps`) | that package's source **commit** |
| pinned by `tutorial-set.json` | the tutorial's own tag or commit |
| left on its default branch by the tutorial | the branch head **at resolve time**, frozen into the lock |

So every platform builds the same commits, and `tutorial.pins` in the lock says
which, and why. The workflow hands the pins to the tutorial as a
`tutorial-pins.json` `{repo: ref}` map, which its own `scripts/tutorial-set.py`
turns into doctest `--release-for` flags — the same path its CI and `run.sh`
take, so a release-set run reproduces from a tutorial checkout:

```bash
python3 scripts/tutorial-plan.py release-set.lock.json --override-out tutorial-pins.json
cd ../logos-tutorial && ./run.sh --pins-override ../logos-release-set/tutorial-pins.json
```

Each tutorial spec is a `tutorial-<name>` row in the validation table. A
platform the tutorial does not list is a skip with that reason, not a failure.
An artifact whose repo the tutorial built against at the release set's version
lists the tutorial's reports in its `validatedBy`.

**Windows.** The tutorial's Windows legs are the `windows` entries of its
`tutorial-set.json`. The `tutorial-windows` job calls
[logos-windows-ci](https://github.com/logos-co/logos-windows-ci) with
`repository`/`ref` set to the pinned tutorial commit: it cross-builds the
tutorial's `flake.nix` on Linux, pinned with this release set's versions as
`--override-input` pairs (computed by the tutorial's own
`tutorial-set.py override-inputs`), and runs each spec's Windows half on
`windows-latest`. `scripts/windows-results.py` reads the execution records of
every Windows leg, the tutorial's and the release set's own, into a
`windows-x86_64` result: a leg with no records failed. Tutorial specs without a
Windows leg are skips.

The tutorial at the pinned commit must work with the pinned versions — it
tracks its dependencies' default branches, so pick a commit from when they
matched.

### Running them locally

```bash
export RELEASE_SET_DIR="$PWD" GITHUB_TOKEN=...
./doctests/run.sh headless-storage-module     # the most deterministic — start here
./doctests/run.sh                             # everything
```

## The workflow

`.github/workflows/release-set.yml`, **manual only** (`workflow_dispatch`):

1. **resolve** — every pin, or fail fast listing each unresolved `CHANGE-ME`.
2. **doctests** and **tutorial** — the three platforms in parallel, plus the
   Windows legs (`doctests-windows`, `tutorial-windows`); the tutorial on the
   platforms its `tutorial-set.json` lists. If the set pins an artifact
   that a platform does not publish, the affected spec is **skipped, not
   failed** — a release with no linux-arm64 AppImage leaves nothing to
   smoke-test there, which is not a failure of the release set. Skipping is
   decided **up front** by `select-specs.py` reading the lock, not discovered
   mid-run: the doc-test runner treats any non-zero exit as a hard failure, so a
   spec that started can only pass or fail.
3. **publish** — only on green, and never from `main`: a release tagged
   `v<version>` carrying `release-set.lock.json` and the per-platform HTML
   reports. Its description lists every artifact first — version, commit and
   platforms, linking each binary and `.lgx` — then the validation table and
   what was skipped and why.

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
flake.nix                           what the Windows legs stage: logosctl, lgpm, probes, pins
scripts/resolve.py                  pins  -> release-set.lock.json
scripts/select-specs.py             which specs can run on a platform, and why not
scripts/render-release.py           lock + results -> release notes
scripts/tutorial-plan.py            lock -> tutorial matrix, pins, skips
scripts/windows-plan.py             lock -> Windows matrix, flake pins, skips
scripts/check-probes.py             the specs' probes == doctests/probes/
scripts/record-result.py            one job's outcome -> results-<platform>-<spec>.json
scripts/windows-results.py          Windows legs' records -> results-windows-x86_64.json
doctests/release-set.sh             fetch/install helper the specs drive
doctests/windows.sh                 its Windows half, reading release-set.json
doctests/probes/                    the probe sources flake.nix cross-builds
doctests/*.test.yaml                the five specs
doctests/run.sh                     run them locally
.github/workflows/release-set.yml   resolve -> validate -> publish
```

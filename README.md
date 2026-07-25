# logos-release-set

A **release set** is the answer to "which versions of everything go together?" —
one file naming the exact release of every Logos component that is published and
supported as a unit, plus the machinery to prove that set actually works.

```
release-set.json          what a human pins   (4 tags + 11 versions)
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

Plus `version`, the release set's own `A.B.C.D` identifier, which becomes the
release tag (`v0.2.1.0`).

`modules` and `uiApps` carry **no repository** — they are catalog packages
identified by `(name, version)`, and the resolver discovers where they came
from. Note these are *module* names, not repo names: the LEZ module is published
as `logos_execution_zone`, and two entries (`lez_indexer_module`,
`lez_explorer_ui`) have no repo in the main workspace at all.

**`main` holds `CHANGE-ME` placeholders and never publishes.** To cut a release
set, branch, fill in the versions, and run the workflow on that branch.

```bash
git switch -c release/0.2.1.0
$EDITOR release-set.json
python3 scripts/resolve.py release-set.json --check   # fails while any pin is CHANGE-ME
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
`logos_execution_zone` lives in `logos-execution-zone-module`, and the LEZ
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
| `headless-storage-module` | Installs, initializes and drives the pinned storage node; a probe module calls it and catches its events |
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

Everything is the **portable** variant, end to end. A dev build RPATHs into
`/nix/store` and will not load beside portable catalog packages, so probes build
`#lgx-portable` and the Basecamp under test is the portable bundle.

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
   smoke-test there, which is not a failure of the release set.
3. **publish** — only on green, and never from `main`: a release tagged
   `v<version>` carrying `release-set.lock.json` and the per-platform HTML
   reports, with a description that links every binary, every `.lgx`, every
   source commit, and a table of what was skipped and why.

Skipped platforms are always named in the release description. A release that
does not mention a platform passed there.

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

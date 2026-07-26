#!/usr/bin/env bash
#
# Helper the release-set doc-tests drive. Everything the specs need to turn a
# pinned release set into a running system lives here, so the specs stay
# readable and every spec fetches artifacts exactly the same way.
#
#   release-set.sh lock                     resolve (or reuse) release-set.lock.json
#   release-set.sh platform                 print this machine's platform id
#   release-set.sh pin <group> <name>       print the pinned tag/version
#   release-set.sh fetch <component> <bin>  download + extract a released binary
#   release-set.sh path <bin>               print the REAL path behind ./bin/<bin>
#   release-set.sh seed-modules <component> copy a tool's bundled modules into MODULES_DIR
#   release-set.sh wait-daemon [secs]       block until the logoscore daemon answers
#   release-set.sh install <pkg> [...]      lgpd download + verify + lgpm install
#
# Exit code 78 means "this artifact is not published for this platform" — the
# caller should SKIP rather than fail. See the release-set workflow.
#
# Environment:
#   RELEASE_SET_DIR   the logos-release-set checkout (required)
#   RELEASE_SET_LOCK  a pre-resolved lock to reuse instead of resolving (optional)
#   GITHUB_TOKEN      needed when resolving (GitHub API rate limits)
set -euo pipefail

SKIP=78
LOCK="release-set.lock.json"
BIN="$PWD/bin"

die() { echo "error: $*" >&2; exit 1; }
skip() { echo "SKIP: $*" >&2; exit $SKIP; }

: "${RELEASE_SET_DIR:?set RELEASE_SET_DIR to the logos-release-set checkout}"

# ---------------------------------------------------------------------------
# platform
# ---------------------------------------------------------------------------

detect_platform() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os/$arch" in
    Linux/x86_64)            echo "linux-x86_64" ;;
    Linux/aarch64|Linux/arm64) echo "linux-arm64" ;;
    Darwin/arm64)            echo "macos-arm64" ;;
    *) die "unsupported platform $os/$arch (release sets validate linux-x86_64, linux-arm64, macos-arm64)" ;;
  esac
}

# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------

cmd_lock() {
  if [ -f "$LOCK" ]; then
    echo "==> reusing $LOCK"
  elif [ -n "${RELEASE_SET_LOCK:-}" ] && [ -f "$RELEASE_SET_LOCK" ]; then
    echo "==> reusing pre-resolved $RELEASE_SET_LOCK"
    cp "$RELEASE_SET_LOCK" "$LOCK"
  else
    echo "==> resolving $RELEASE_SET_DIR/release-set.json"
    python3 "$RELEASE_SET_DIR/scripts/resolve.py" \
      "$RELEASE_SET_DIR/release-set.json" -o "$LOCK"
  fi
  python3 - "$LOCK" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1]))
print(f"release set {lock['version']}  ({lock['catalog']['repo']})")
for group in ("apps", "devUtils"):
    for item in lock[group]:
        print(f"  {item['name']:28} {item['tag']:12} {item['commit'][:10]}")
for group in ("modules", "uiApps"):
    for item in lock[group]:
        print(f"  {item['name']:28} {item['version']:12} {(item['commit'] or '?')[:10]}")
PY
}

require_lock() { [ -f "$LOCK" ] || die "$LOCK not found — run 'release-set.sh lock' first"; }

# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

# pin <group> <name> -> the pinned tag (apps/devUtils) or version (modules/uiApps)
cmd_pin() {
  require_lock
  python3 - "$LOCK" "$1" "$2" <<'PY'
import json, sys
lock, group, name = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
for item in lock.get(group, []):
    if item["name"] == name:
        print(item.get("tag") or item.get("version"))
        break
else:
    sys.exit(f"{group}/{name} is not in the release set")
PY
}

# field <group> <name> <dotted.path>
cmd_field() {
  require_lock
  python3 - "$LOCK" "$1" "$2" "$3" <<'PY'
import json, sys
lock, group, name, path = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3], sys.argv[4]
for item in lock.get(group, []):
    if item["name"] == name:
        value = item
        for part in path.split("."):
            value = (value or {}).get(part)
        print(value if value is not None else "")
        break
else:
    sys.exit(f"{group}/{name} is not in the release set")
PY
}

# asset_url <name> -> download URL of this platform's asset, empty if none
asset_url() {
  require_lock
  python3 - "$LOCK" "$1" "$(detect_platform)" <<'PY'
import json, sys
lock, name, platform = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
for group in ("apps", "devUtils"):
    for item in lock.get(group, []):
        if item["name"] == name:
            for asset in item.get("assets", []):
                if asset.get("platform") == platform:
                    print(asset["url"])
                    sys.exit(0)
            sys.exit(0)
sys.exit(f"{name} is not an app or dev-util in the release set")
PY
}

# ---------------------------------------------------------------------------
# fetch — download a released binary and put it on ./bin
# ---------------------------------------------------------------------------
#
# Linux artifacts are AppImages (sometimes tar-wrapped). CI runners have no
# FUSE, so we always --appimage-extract and run the extracted AppRun.
# macOS artifacts are plain directory bundles (.tar.gz) or a .dmg.

# Expose a fetched binary as ./bin/<name>.
#
# A wrapper that `exec`s the real file, NOT a symlink. These tools locate their
# siblings relative to their own executable: logoscore looks for `logos_host`
# next to itself and for `../modules`. On macOS that lookup uses
# _NSGetExecutablePath, which reports the path the process was INVOKED with and
# does not resolve symlinks — so through a symlink logoscore searches ./bin and
# reports "logos_host not found", and every module load fails. `exec` replaces
# the process image, so the running program's own path is the real one inside
# the bundle and both lookups land where they should, on every platform.
link_binary() {
  local binname="$1" target="$2"
  cat > "$BIN/$binname" <<WRAPPER
#!/usr/bin/env sh
exec "$target" "\$@"
WRAPPER
  chmod +x "$BIN/$binname"
  printf '%s\n' "$target" > "$BIN/.$binname.path"
}

cmd_fetch() {
  local component="${1:?usage: fetch <component> <bin-name>}"
  local binname="${2:?usage: fetch <component> <bin-name>}"
  local platform url file workdir
  platform="$(detect_platform)"
  url="$(asset_url "$component")"

  if [ -z "$url" ]; then
    skip "$component has no $platform artifact in this release set"
  fi

  mkdir -p "$BIN" .fetch
  workdir=".fetch/$component"
  rm -rf "$workdir" && mkdir -p "$workdir"
  file="$workdir/$(basename "$url")"

  echo "==> $component ($platform): $(basename "$url")"
  curl -fsSL "$url" -o "$file"

  case "$file" in
    *.tar.gz|*.tgz) tar -xzf "$file" -C "$workdir" ;;
    *.dmg)
      local mnt; mnt="$(mktemp -d)"
      hdiutil attach -nobrowse -quiet -mountpoint "$mnt" "$file"
      cp -R "$mnt"/*.app "$workdir/" 2>/dev/null || true
      hdiutil detach -quiet "$mnt"
      ;;
  esac

  # An AppImage (bare, or unpacked from the tarball) — extract it, no FUSE needed.
  local appimage
  appimage="$(find "$workdir" -maxdepth 2 -name '*.AppImage' -print -quit)"
  if [ -n "$appimage" ]; then
    chmod +x "$appimage"
    ( cd "$workdir" && "./$(basename "$appimage")" --appimage-extract >/dev/null )
    link_binary "$binname" "$PWD/$workdir/squashfs-root/AppRun"
    echo "    -> $BIN/$binname (AppImage, extracted)"
    return
  fi

  # A directory bundle: find the executable by NAME, either in bin/ or inside a
  # .app's Contents/MacOS/. Matching "*/MacOS/*" instead would take whichever
  # executable the filesystem returned first — Basecamp's .app ships
  # LogosBasecamp, LogosBasecamp.bin, ui-host, logoscore and logos_host side by
  # side, so an unanchored match silently launches the wrong program.
  local exe
  exe="$(find "$workdir" -type f -perm -u+x \
           \( -path "*/bin/$binname" -o -path "*/MacOS/$binname" \) -print -quit)"
  [ -n "$exe" ] || die "no executable named '$binname' in $(basename "$url") — \
found: $(find "$workdir" -type f -perm -u+x \( -path '*/bin/*' -o -path '*/MacOS/*' \) \
-exec basename {} \; | sort -u | tr '\n' ' ')"
  link_binary "$binname" "$PWD/$exe"
  echo "    -> $BIN/$binname (bundle: ${exe#"$workdir"/})"
}

# path — the real file behind ./bin/<name>, recorded when it was fetched.
cmd_path() {
  local binname="${1:?usage: path <bin-name>}"
  [ -f "$BIN/.$binname.path" ] || die "no such fetched binary: $binname"
  cat "$BIN/.$binname.path"
}

# wait-daemon — block until logoscore answers, or give up.
#
# Startup is not instantaneous and not constant: the daemon loads
# capability_module before it writes its client config, which takes a couple of
# seconds on a warm machine and longer on a loaded CI runner. A fixed sleep
# either races or wastes time; polling does neither.
cmd_wait_daemon() {
  local timeout="${1:-60}" i=1
  while [ "$i" -le "$timeout" ]; do
    if "$BIN/logoscore" status >/dev/null 2>&1; then
      echo "==> daemon ready after ${i}s"
      "$BIN/logoscore" status
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "--- daemon log ---" >&2
  tail -30 logs.txt >&2 2>/dev/null || true
  die "logoscore did not become ready within ${timeout}s"
}

# seed-modules — copy a fetched tool's bundled modules into MODULES_DIR.
#
# logoscore ships capability_module inside its own bundle and finds it relative
# to its executable. Because we run it through a symlink in ./bin, that lookup
# lands on our lgpm target directory on macOS (see `path` above) and the module
# is never found — every load-module then stalls on capability negotiation and
# fails. Copying the bundled modules into the directory we pass to `-m` makes
# the tool independent of that resolution, on every platform.
cmd_seed_modules() {
  local component="${1:?usage: seed-modules <component>}"
  local modules_dir="${MODULES_DIR:-./modules}"
  local src
  src="$(find ".fetch/$component" -maxdepth 3 -type d -name modules -print -quit 2>/dev/null || true)"
  [ -n "$src" ] || die "$component has no bundled modules/ directory — was it fetched?"
  mkdir -p "$modules_dir"
  # -L dereferences: nix bundles are full of symlinks into the store.
  cp -RL "$src"/. "$modules_dir"/
  chmod -R u+w "$modules_dir" 2>/dev/null || true
  echo "==> seeded $(ls "$src" | tr '\n' ' ')from $component into $modules_dir"
}

# ---------------------------------------------------------------------------
# install — pull a catalog package at its pinned version and install it
# ---------------------------------------------------------------------------

cmd_install() {
  local pkg="${1:?usage: install <package> [--ui]}"
  local ui="${2:-}"
  local group version variant lgxsha file
  require_lock

  group="modules"
  python3 -c "
import json,sys
lock=json.load(open('$LOCK'))
sys.exit(0 if any(i['name']=='$pkg' for i in lock['modules']) else 1)
" || group="uiApps"

  version="$(cmd_pin "$group" "$pkg")"
  variant="$(cmd_field "$group" "$pkg" "platforms")"
  case "$variant" in
    *"$(detect_platform)"*) : ;;
    *) skip "$pkg@$version has no $(detect_platform) variant" ;;
  esac

  # Where installs land. Basecamp wants them under a --user-dir tree; the
  # headless specs use plain ./modules. Override with MODULES_DIR / PLUGINS_DIR.
  local modules_dir="${MODULES_DIR:-./modules}"
  local plugins_dir="${PLUGINS_DIR:-./plugins}"

  mkdir -p packages "$modules_dir" "$plugins_dir"
  echo "==> lgpd download $pkg --version $version"
  "$BIN/lgpd" download "$pkg" --version "$version" -o packages

  file="$(find packages -name "$pkg-*.lgx" -print -quit)"
  [ -n "$file" ] || die "lgpd produced no .lgx for $pkg"

  # The catalog publishes a sha256 for every .lgx; verify what we just pulled
  # is byte-identical to what the release set pinned.
  lgxsha="$(cmd_field "$group" "$pkg" "lgx.sha256")"
  if [ -n "$lgxsha" ]; then
    python3 - "$file" "$lgxsha" <<'PY'
import hashlib, sys
path, expected = sys.argv[1], sys.argv[2]
digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
if digest != expected:
    sys.exit(f"checksum mismatch for {path}\n  expected {expected}\n  got      {digest}")
print(f"    sha256 OK ({digest[:12]}…)")
PY
  fi

  if [ "$ui" = "--ui" ]; then
    "$BIN/lgpm" --modules-dir "$modules_dir" --ui-plugins-dir "$plugins_dir" \
      install --file "$file"
  else
    "$BIN/lgpm" --modules-dir "$modules_dir" install --file "$file"
  fi
}

# install-all — every module and UI app in the release set, into one tree.
cmd_install_all() {
  require_lock
  local names
  names="$(python3 - "$LOCK" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1]))
for item in lock["modules"] + lock["uiApps"]:
    print(item["name"])
PY
)"
  for name in $names; do
    cmd_install "$name" --ui
  done
}

case "${1:-}" in
  lock)     shift; cmd_lock "$@" ;;
  platform) detect_platform ;;
  pin)      shift; cmd_pin "$@" ;;
  field)    shift; cmd_field "$@" ;;
  fetch)        shift; cmd_fetch "$@" ;;
  path)         shift; cmd_path "$@" ;;
  seed-modules) shift; cmd_seed_modules "$@" ;;
  wait-daemon)  shift; cmd_wait_daemon "$@" ;;
  install)      shift; cmd_install "$@" ;;
  install-all)  shift; cmd_install_all "$@" ;;
  *) die "usage: release-set.sh {lock|platform|pin|field|fetch|path|seed-modules|install|install-all} ..." ;;
esac

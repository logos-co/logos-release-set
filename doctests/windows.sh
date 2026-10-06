#!/usr/bin/env bash
#
# The Windows half of release-set.sh. Nix and the resolver do not run on
# Windows, so CI prepares that leg on Linux (logos-windows-ci) and stages the
# results beside the generated doc-test script, one directory per target:
#
#   release-set/   release-set.json and this script
#   logosctl/      logosctl-x86_64-windows.zip from the pinned release, unpacked
#   lgpm/          lgpm-x86_64-windows.zip from the pinned release, unpacked
#   probe-<name>/  the spec's probe module, cross-built with the pinned builder
#
#   windows.sh pins                 print what release-set.json pins
#   windows.sh link <bin>           expose the staged <bin> as ./bin/<bin>
#   windows.sh ctl-install <pkg>    logosctl download + verify + install
#   windows.sh install-all          download + verify every package with a
#                                   Windows variant, lgpm install into
#                                   $MODULES_DIR / $PLUGINS_DIR
#
# Needs bash, curl, jq and sha256sum, which Git Bash on windows-latest has.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PINS="$HERE/release-set.json"
SESSION="${LOGOSCTL_SESSION:-./session}"

die() { echo "error: $*" >&2; exit 1; }

# jq.exe ends lines with CRLF; strip the CR from every value we compare.
jqr() { jq -r "$@" | tr -d '\r'; }

cmd_pins() {
  jqr '(.apps + .devUtils + (.tools // []))[] | "  \(.name)  \(.releaseTag)"' "$PINS"
  jqr '(.modules + .uiApps)[] | "  \(.name)  \(.version)"' "$PINS"
}

# The same wrapper release-set.sh fetch writes. logosctl finds logos_host.exe
# and its bundled modules relative to its own executable, so it stays in place.
cmd_link() {
  local binname="${1:?usage: link <bin-name>}"
  local exe="$PWD/$binname/bin/$binname.exe"
  [ -f "$exe" ] || die "$binname/bin/$binname.exe is not staged here"
  mkdir -p bin
  printf '#!/usr/bin/env sh\nexec "%s" "$@"\n' "$exe" > "bin/$binname"
  chmod +x "bin/$binname"
  echo "    -> bin/$binname (staged: $binname/bin/$binname.exe)"
}

# The catalog index the resolver reads on the other platforms, fetched once
# into a file: the callers run in subshells, which cannot set a variable.
INDEX_FILE="./catalog-index.json"
catalog_index() {
  if [ ! -s "$INDEX_FILE" ]; then
    curl -fsSL "$(curl -fsSL "$(jqr .catalog.logosRepoUrl "$PINS")" | jqr .indexUrl)" \
      > "$INDEX_FILE"
  fi
  cat "$INDEX_FILE"
}

# <field> of the index entry for <pkg>@<version>.
catalog_field() {
  catalog_index | jqr --arg n "$1" --arg v "$2" \
    ".packages[] | select(.name == \$n) | .versions[]
     | select(.manifest.version == \$v) | $3" | head -n 1
}

pinned_version() {
  jqr --arg n "$1" '(.modules + .uiApps)[] | select(.name == $n) | .version' "$PINS"
}

# Downloads <pkg> at its pinned version, checks it against the sha256 the
# index publishes, and sets FILE to the .lgx.
FILE=""
ctl_download() {
  local pkg="$1" version out want got
  version="$(pinned_version "$pkg")"
  [ -n "$version" ] || die "$pkg is not in the release set"

  mkdir -p packages
  echo "==> logosctl package download $pkg --version $version"
  out="$(./bin/logosctl --config-dir "$SESSION" package download "$pkg" \
           --version "$version" -o packages --json)"
  FILE="$(printf '%s' "$out" | jqr .path)"
  [ -n "$FILE" ] && [ "$FILE" != null ] || die "no .lgx path in: $out"
  FILE="$(cygpath -u "$FILE")"

  want="$(catalog_field "$pkg" "$version" '.sha256 // empty')"
  if [ -n "$want" ]; then
    got="$(sha256sum "$FILE" | cut -d ' ' -f 1)"
    [ "$got" = "$want" ] || die "checksum mismatch for $FILE
  expected $want
  got      $got"
    echo "    sha256 OK (${got:0:12}…)"
  fi
}

cmd_ctl_install() {
  local pkg="${1:?usage: ctl-install <package>}"
  ctl_download "$pkg"
  ./bin/logosctl --config-dir "$SESSION" package install --file "$FILE" -y
}

# Basecamp's --user-dir tree, which logosctl does not write to. A package with
# no Windows variant is skipped and named: the index lists each variant's main.
cmd_install_all() {
  local modules_dir="${MODULES_DIR:-./modules}" plugins_dir="${PLUGINS_DIR:-./plugins}"
  local pkg version
  mkdir -p "$modules_dir" "$plugins_dir"
  for pkg in $(jqr '(.modules + .uiApps)[].name' "$PINS"); do
    version="$(pinned_version "$pkg")"
    if [ -z "$(catalog_field "$pkg" "$version" '.manifest.main["windows-x86_64"] // empty')" ]; then
      echo "==> skip $pkg $version: no windows-x86_64 variant published"
      continue
    fi
    ctl_download "$pkg"
    ./bin/lgpm --modules-dir "$modules_dir" --ui-plugins-dir "$plugins_dir" \
      install --file "$FILE"
  done
}

case "${1:-}" in
  pins)        shift; cmd_pins "$@" ;;
  link)        shift; cmd_link "$@" ;;
  ctl-install) shift; cmd_ctl_install "$@" ;;
  install-all) shift; cmd_install_all "$@" ;;
  *) die "usage: windows.sh {pins|link|ctl-install|install-all} ..." ;;
esac

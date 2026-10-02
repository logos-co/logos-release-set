#!/usr/bin/env bash
#
# The Windows half of release-set.sh. Nix and the resolver do not run on
# Windows, so CI prepares that leg on Linux (logos-windows-ci) and stages the
# results beside the generated doc-test script, one directory per target:
#
#   release-set/   release-set.json and this script
#   logosctl/      logosctl-x86_64-windows.zip from the pinned release, unpacked
#   probe-<name>/  the spec's probe module, cross-built with the pinned builder
#
#   windows.sh pins                 print what release-set.json pins
#   windows.sh link <bin>           expose the staged <bin> as ./bin/<bin>
#   windows.sh ctl-install <pkg>    logosctl download + verify + install
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

# The sha256 the catalog index publishes for <pkg>@<version>: the index the
# resolver reads on the other platforms.
catalog_sha256() {
  local index
  index="$(curl -fsSL "$(jqr .catalog.logosRepoUrl "$PINS")" | jqr .indexUrl)"
  curl -fsSL "$index" | jqr --arg n "$1" --arg v "$2" \
    '.packages[] | select(.name == $n) | .versions[]
     | select(.manifest.version == $v) | .sha256 // empty' | head -n 1
}

cmd_ctl_install() {
  local pkg="${1:?usage: ctl-install <package>}"
  local version out file want got
  version="$(jqr --arg n "$pkg" '(.modules + .uiApps)[] | select(.name == $n) | .version' "$PINS")"
  [ -n "$version" ] || die "$pkg is not in the release set"

  mkdir -p packages
  echo "==> logosctl package download $pkg --version $version"
  out="$(./bin/logosctl --config-dir "$SESSION" package download "$pkg" \
           --version "$version" -o packages --json)"
  file="$(printf '%s' "$out" | jqr .path)"
  [ -n "$file" ] && [ "$file" != null ] || die "no .lgx path in: $out"
  file="$(cygpath -u "$file")"

  want="$(catalog_sha256 "$pkg" "$version")"
  if [ -n "$want" ]; then
    got="$(sha256sum "$file" | cut -d ' ' -f 1)"
    [ "$got" = "$want" ] || die "checksum mismatch for $file
  expected $want
  got      $got"
    echo "    sha256 OK (${got:0:12}…)"
  fi

  ./bin/logosctl --config-dir "$SESSION" package install --file "$file" -y
}

case "${1:-}" in
  pins)        shift; cmd_pins "$@" ;;
  link)        shift; cmd_link "$@" ;;
  ctl-install) shift; cmd_ctl_install "$@" ;;
  *) die "usage: windows.sh {pins|link|ctl-install} ..." ;;
esac

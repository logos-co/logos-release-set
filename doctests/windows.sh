#!/usr/bin/env bash
#
# The Windows half of release-set.sh. Nix and the resolver do not run on
# Windows, so CI prepares that leg on Linux (logos-windows-ci) and stages the
# results beside the generated doc-test script, one directory per target:
#
#   release-set/   release-set.json and this script
#   logosctl/      logosctl-x86_64-windows.zip from the pinned release, unpacked
#   lgpm/          lgpm-x86_64-windows.zip from the pinned release, unpacked
#   basecamp-setup/  the pinned Basecamp release's installer, as a .bin
#   probe-<name>/  the spec's probe module, cross-built with the pinned builder
#
#   windows.sh pins                 print what release-set.json pins
#   windows.sh link <bin>           expose the staged <bin> as ./bin/<bin>
#   windows.sh ctl-install <pkg>    logosctl download + verify + install
#   windows.sh install-all          download + verify every package with a
#                                   Windows variant, lgpm install into
#                                   $MODULES_DIR / $PLUGINS_DIR
#   windows.sh basecamp-install <dir>    run the staged installer silently
#   windows.sh basecamp-uninstall <dir>  run its uninstaller, check what is left
#   windows.sh alive <pid>          is that background launch still running
#   windows.sh stop <pid>           end it and every process it started
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

# What a silent install registers for the current user and the uninstaller
# removes again. On a machine where Basecamp is already installed these point
# at that install, so basecamp-install saves them and basecamp-uninstall puts
# them back.
APP_NAME="Logos Basecamp"
REG_KEYS=("HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\$APP_NAME"
          "HKCU\\Software\\$APP_NAME")
SAVED="./basecamp-registration"

# reg, tasklist and taskkill take /switches, which MSYS would rewrite as paths.
win_() { MSYS2_ARG_CONV_EXCL='*' "$@"; }

shortcuts() {
  local folder
  for folder in Programs DesktopDirectory; do
    printf '%s/%s.lnk\n' "$(cygpath -u "$(powershell -NoProfile -Command \
      "[Environment]::GetFolderPath('$folder')" | tr -d '\r')")" "$APP_NAME"
  done
}

save_registration() {
  local i=0 key lnk
  rm -rf "$SAVED"; mkdir -p "$SAVED"
  for key in "${REG_KEYS[@]}"; do
    i=$((i + 1))
    if win_ reg query "$key" >/dev/null 2>&1; then
      win_ reg export "$key" "$(cygpath -w "$SAVED")\\key$i.reg" /y >/dev/null
      echo "    saved the existing $key"
    fi
  done
  i=0
  while IFS= read -r lnk; do
    i=$((i + 1))
    if [ -f "$lnk" ]; then cp "$lnk" "$SAVED/shortcut$i.lnk"; echo "    saved $lnk"; fi
  done < <(shortcuts)
}

restore_registration() {
  local i=0 f lnk
  [ -d "$SAVED" ] || return 0
  for f in "$SAVED"/key*.reg; do
    [ -f "$f" ] || continue
    win_ reg import "$(cygpath -w "$f")" >/dev/null 2>&1 || die "could not restore $f"
    echo "    restored a saved key ($(basename "$f"))"
  done
  while IFS= read -r lnk; do
    i=$((i + 1))
    if [ -f "$SAVED/shortcut$i.lnk" ]; then cp "$SAVED/shortcut$i.lnk" "$lnk"; echo "    restored $lnk"; fi
  done < <(shortcuts)
  rm -rf "$SAVED"
}

cmd_basecamp_install() {
  local dir="${1:?usage: basecamp-install <dir>}" setup win want got
  # Staged under another name so CI's PE gates skip it (flake.nix says why).
  [ -f basecamp-setup/LogosBasecamp-setup.bin ] || die "no installer staged in basecamp-setup/"
  setup=LogosBasecamp-setup.exe
  cp basecamp-setup/LogosBasecamp-setup.bin "$setup"
  save_registration
  mkdir -p "$dir"
  win="$(cygpath -w "$(cd "$dir" && pwd)")"
  # NSIS reads /D= last and verbatim, so the path cannot be quoted.
  case "$win" in *" "*) die "the install path has a space: $win" ;; esac
  echo "==> $(basename "$setup") /S /D=$win"
  win_ "./$setup" /S "/D=$win" || die "the installer exited $?"
  rm -f "$setup"
  [ -f "$dir/bin/LogosBasecamp.exe" ] || die "no bin/LogosBasecamp.exe after the install"
  want="$(jqr '.apps[] | select(.name == "logos-basecamp") | .releaseTag' "$PINS")"
  got="$(win_ reg query "${REG_KEYS[0]}" /v DisplayVersion | tr -d '\r' \
           | awk '$1 == "DisplayVersion" { print $3 }')"
  [ "$got" = "$want" ] || die "registered $APP_NAME $got, but the release set pins $want"
  echo "    installed $APP_NAME $got, the pinned release, into $win"
}

# Uninstall.exe with _?= runs in place and waits, so it cannot remove itself or
# its folder; everything else has to be gone. The saved registration is put
# back whatever happens.
cmd_basecamp_uninstall() {
  local dir="${1:?usage: basecamp-uninstall <dir>}" win failed=0 left
  if [ -f "$dir/Uninstall.exe" ]; then
    win="$(cygpath -w "$(cd "$dir" && pwd)")"
    echo "==> Uninstall.exe /S _?=$win"
    win_ "$dir/Uninstall.exe" /S "_?=$win" || { echo "the uninstaller exited $?"; failed=1; }
    left="$(ls -A "$dir")"
    if [ "$left" = "Uninstall.exe" ]; then
      echo "    removed the installed tree"
    else
      echo "FAIL: left behind: $(echo "$left" | grep -vx Uninstall.exe | head -n 5 | tr '\n' ' ')"; failed=1
    fi
    if win_ reg query "${REG_KEYS[0]}" >/dev/null 2>&1; then
      echo "FAIL: ${REG_KEYS[0]} is still there"; failed=1
    else
      echo "    removed its registry keys"
    fi
    rm -rf "$dir"
  else
    echo "FAIL: no Uninstall.exe in $dir"; failed=1
  fi
  restore_registration
  return "$failed"
}

# A background launch's Windows PID. The MSYS PID $! names a process that
# exec()s the .exe, and /proc maps it to the program actually running.
winpid() { cat "/proc/$1/winpid" 2>/dev/null || true; }

cmd_alive() {
  local pid
  pid="$(winpid "${1:?usage: alive <pid>}")"
  [ -n "$pid" ] && win_ tasklist /FI "PID eq $pid" /NH | grep -q "$pid"
}

# Git Bash's kill reaches only the one process; taskkill /T takes the tree,
# logos_host.exe children included.
cmd_stop() {
  local pid
  pid="$(winpid "${1:?usage: stop <pid>}")"
  [ -n "$pid" ] || return 0
  win_ taskkill /T /F /PID "$pid" >/dev/null 2>&1 || true
}

case "${1:-}" in
  pins)        shift; cmd_pins "$@" ;;
  link)        shift; cmd_link "$@" ;;
  ctl-install) shift; cmd_ctl_install "$@" ;;
  install-all) shift; cmd_install_all "$@" ;;
  basecamp-install)   shift; cmd_basecamp_install "$@" ;;
  basecamp-uninstall) shift; cmd_basecamp_uninstall "$@" ;;
  alive)       shift; cmd_alive "$@" ;;
  stop)        shift; cmd_stop "$@" ;;
  *) die "usage: windows.sh {pins|link|ctl-install|install-all|basecamp-install|basecamp-uninstall|alive|stop} ..." ;;
esac

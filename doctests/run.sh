#!/usr/bin/env bash
#
# Run the release-set doc-tests locally and regenerate their Markdown.
#
# Each spec validates the release set pinned by ../release-set.json using only
# released artifacts: tools downloaded from GitHub releases, modules installed
# from the catalog with lgpd/lgpm.
#
#   headless-storage-module    the most deterministic — start here
#   headless-delivery-module
#   headless-blockchain-module needs outbound UDP to the testnet
#   basecamp-appimage-smoke    launches the shipped artifact
#   basecamp-ui                builds the pinned commit with the inspector on
#
# Run one spec:            ./run.sh headless-storage-module
# Run everything:          ./run.sh
# Reuse a resolved lock:   RELEASE_SET_LOCK=/path/to/release-set.lock.json ./run.sh
# Local doctest checkout:  DOCTEST="nix run path:../../logos-doctest --" ./run.sh
#
# The specs will not resolve while release-set.json still holds CHANGE-ME
# placeholders — that is main's steady state. Run these from a release branch
# with the versions filled in.
set -euo pipefail

cd "$(dirname "$0")"

read -r -a DOCTEST <<< "${DOCTEST:-nix run github:logos-co/logos-doctest --}"
OUTPUT_DIR="./outputs"

# The specs locate the checkout through this, so they can find release-set.json
# and the helper regardless of where the runner puts its working directory.
export RELEASE_SET_DIR="${RELEASE_SET_DIR:-$(cd .. && pwd)}"
echo "==> RELEASE_SET_DIR=$RELEASE_SET_DIR"

if [ -z "${GITHUB_TOKEN:-}" ] && [ -z "${GH_TOKEN:-}" ]; then
  echo "==> warning: no GITHUB_TOKEN/GH_TOKEN set; the resolver will hit the" >&2
  echo "    unauthenticated GitHub rate limit (60 requests/hour)." >&2
fi

# Fail early and clearly rather than deep inside a spec.
python3 "$RELEASE_SET_DIR/scripts/resolve.py" \
  "$RELEASE_SET_DIR/release-set.json" --check

if [ $# -gt 0 ]; then
  SPECS=()
  for name in "$@"; do SPECS+=("${name%.test.yaml}.test.yaml"); done
else
  SPECS=(*.test.yaml)
fi

echo "==> Clearing previous ${OUTPUT_DIR}/"
# Artifacts copied out of the read-only nix store land read-only, and rm -rf
# cannot delete inside a directory it cannot write to.
[ -e "${OUTPUT_DIR}" ] && chmod -R u+w "${OUTPUT_DIR}" 2>/dev/null
rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

for spec in "${SPECS[@]}"; do
  name="$(basename "${spec%.test.yaml}")"
  echo "==> Running ${spec} into ${OUTPUT_DIR}/"
  "${DOCTEST[@]}" run "${spec}" \
    --verbose \
    --continue-on-fail \
    --output-dir "${OUTPUT_DIR}/"

  echo "==> Generating ${OUTPUT_DIR}/${name}.md"
  "${DOCTEST[@]}" generate "${spec}" -o "${OUTPUT_DIR}/${name}.md"
done

echo "==> Cleaning build artifacts from ${OUTPUT_DIR}/ (keeps .md and images/)"
"${DOCTEST[@]}" clean "${OUTPUT_DIR}" --verbose

echo "==> Done. Rendered docs and screenshots are in ${OUTPUT_DIR}/"

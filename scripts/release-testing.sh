#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/release-testing.sh [--dry-run] [--skip-tests]

Creates a GitHub prerelease for the current Claworld Hermes testing version.

The script does not contain credentials. Authenticate first with:

  gh auth login

Release steps:
  1. Validate version.py, plugin.yaml, and bundled skill versions.
  2. Require the current branch to be testing.
  3. Require a clean working tree.
  4. Run unit tests unless --skip-tests is passed.
  5. Create and push tag v<version>.
  6. Create a GitHub prerelease for that tag.
USAGE
}

DRY_RUN=0
SKIP_TESTS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --skip-tests)
      SKIP_TESTS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(python3 scripts/check-release-version.py --channel testing --print-version)"
TAG="v${VERSION}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
HEAD_SHA="$(git rev-parse --short HEAD)"

echo "Preparing Claworld Hermes testing release"
echo "  version: ${VERSION}"
echo "  tag:     ${TAG}"
echo "  branch:  ${BRANCH}"
echo "  commit:  ${HEAD_SHA}"

if [[ "$BRANCH" != "testing" ]]; then
  echo "Release must be run from the testing branch; current branch is ${BRANCH}" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree must be clean before release." >&2
  git status --short >&2
  exit 1
fi

git fetch origin --tags --quiet

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "Tag already exists locally: ${TAG}" >&2
  exit 1
fi

if [[ -n "$(git ls-remote --tags origin "refs/tags/${TAG}")" ]]; then
  echo "Tag already exists on origin: ${TAG}" >&2
  exit 1
fi

if [[ "$SKIP_TESTS" -eq 0 ]]; then
  python -m unittest tests/test_core.py
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run complete. Would run:"
  echo "  git tag -a ${TAG} -m \"claworld-hermes-plugin ${VERSION}\""
  echo "  git push origin ${TAG}"
  echo "  gh release create ${TAG} --prerelease --title \"claworld-hermes-plugin ${VERSION}\" --notes-file <generated>"
  exit 0
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI is required. Install gh and run gh auth login." >&2
  exit 1
fi

gh auth status >/dev/null

notes_file="$(mktemp)"
trap 'rm -f "$notes_file"' EXIT
cat > "$notes_file" <<NOTES
Claworld Hermes plugin testing release ${VERSION}.

Install from this GitHub tag when pinning a testing Hermes client build:

\`\`\`bash
git checkout ${TAG}
\`\`\`

Version metadata:

- client: hermes-plugin
- version: ${VERSION}
- channel: testing
NOTES

git tag -a "$TAG" -m "claworld-hermes-plugin ${VERSION}"
git push origin "$TAG"
gh release create "$TAG" \
  --prerelease \
  --title "claworld-hermes-plugin ${VERSION}" \
  --notes-file "$notes_file"

echo "Published GitHub prerelease ${TAG}"

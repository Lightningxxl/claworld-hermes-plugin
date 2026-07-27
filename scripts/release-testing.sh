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
  2. Require the current branch to be staging.
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
HEAD_SHA="$(git rev-parse HEAD)"
RELEASE_REPO="xfx-studio/claworld-hermes-plugin"

echo "Preparing Claworld Hermes testing release"
echo "  version: ${VERSION}"
echo "  tag:     ${TAG}"
echo "  branch:  ${BRANCH}"
echo "  commit:  ${HEAD_SHA}"

if [[ "$BRANCH" != "staging" ]]; then
  echo "Release must be run from the staging branch; current branch is ${BRANCH}" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree must be clean before release." >&2
  git status --short >&2
  exit 1
fi

git fetch origin staging --tags --quiet
REMOTE_SHA="$(git rev-parse origin/staging)"
if [[ "$HEAD_SHA" != "$REMOTE_SHA" ]]; then
  echo "staging must match origin/staging before release." >&2
  echo "HEAD:           ${HEAD_SHA}" >&2
  echo "origin/staging: ${REMOTE_SHA}" >&2
  exit 1
fi

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "Tag already exists locally: ${TAG}" >&2
  exit 1
fi

if [[ -n "$(git ls-remote --tags origin "refs/tags/${TAG}")" ]]; then
  echo "Tag already exists on origin: ${TAG}" >&2
  exit 1
fi

for command in gh uv; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "${command} is required for testing releases." >&2
    exit 1
  fi
done

gh auth status >/dev/null
if ! gh api --method POST \
  "repos/${RELEASE_REPO}/releases/generate-notes" \
  -f "tag_name=${TAG}" \
  -f "target_commitish=${HEAD_SHA}" \
  >/dev/null; then
  echo "GitHub CLI cannot create releases for ${RELEASE_REPO}." >&2
  echo "Refresh gh authentication before publishing; no tag was created." >&2
  exit 1
fi

if [[ "$SKIP_TESTS" -eq 0 ]]; then
  uv run --with-requirements requirements.txt \
    python -m unittest tests/test_core.py
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run complete. Would run:"
  echo "  git tag -a ${TAG} -m \"claworld-hermes-plugin ${VERSION}\""
  echo "  git push origin ${TAG}"
  echo "  gh release create ${TAG} --repo ${RELEASE_REPO} --target ${HEAD_SHA} --prerelease --title \"claworld-hermes-plugin ${VERSION}\" --notes-file <generated>"
  exit 0
fi

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
  --repo "$RELEASE_REPO" \
  --target "$HEAD_SHA" \
  --prerelease \
  --title "claworld-hermes-plugin ${VERSION}" \
  --notes-file "$notes_file"

echo "Published GitHub prerelease ${TAG}"

#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/release-stable.sh [--dry-run] [--skip-tests]

Creates the stable GitHub release for the current Claworld Hermes version.
The release must run from a clean main branch synchronized with origin/main.
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

VERSION="$(python3 scripts/check-release-version.py --channel stable --print-version)"
TAG="v${VERSION}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
HEAD_SHA="$(git rev-parse HEAD)"

if [[ "$BRANCH" != "main" ]]; then
  echo "Stable release must run from main; current branch is ${BRANCH}" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree must be clean before release." >&2
  git status --short >&2
  exit 1
fi

git fetch origin main --tags --quiet
REMOTE_SHA="$(git rev-parse origin/main)"
if [[ "$HEAD_SHA" != "$REMOTE_SHA" ]]; then
  echo "main must match origin/main before release." >&2
  echo "HEAD:        $HEAD_SHA" >&2
  echo "origin/main: $REMOTE_SHA" >&2
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

if [[ "$SKIP_TESTS" -eq 0 ]]; then
  python -m unittest tests/test_core.py
fi

echo "Preparing Claworld Hermes stable release"
echo "  version: ${VERSION}"
echo "  tag:     ${TAG}"
echo "  commit:  ${HEAD_SHA}"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run complete. Would create and publish ${TAG}."
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
Claworld Hermes plugin stable release ${VERSION}.

Install the immutable production release:

\`\`\`bash
git clone --depth 1 --branch ${TAG} https://github.com/xfx-studio/claworld-hermes-plugin.git "\$HERMES_HOME/plugins/claworld"
"\$HERMES_HOME/hermes-agent/venv/bin/python" -m pip install -r "\$HERMES_HOME/plugins/claworld/requirements.txt"
hermes plugins enable claworld
\`\`\`
NOTES

git tag -a "$TAG" -m "claworld-hermes-plugin ${VERSION}"
git push origin "$TAG"
gh release create "$TAG" \
  --title "claworld-hermes-plugin ${VERSION}" \
  --notes-file "$notes_file"

echo "Published GitHub release ${TAG}"

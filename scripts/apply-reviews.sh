#!/usr/bin/env bash
# Apply approved manifests from media/completed/tv/_review/.
#
# identify.sh writes a mapping.yaml (with approved: false) into _review/<folder>/
# whenever the cascade can't decide on its own. The user reviews/edits the yaml
# and flips approved to true. This script sweeps _review/, finds every approved
# manifest, and renames the tracks into Jellyfin layout — same destination as
# identify.sh's high-confidence path.
#
# Idempotent. Folders that aren't approved are left in place with a log line.
# After successful apply the source folder is empty and gets rmdir'd, so the
# entry disappears from _review/ on its own.
#
# Run between ./scripts/identify.sh and ./scripts/promote.sh series.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck source=lib/manifest.sh
. "$(dirname "$0")/lib/manifest.sh"

for cmd in yq; do
  if ! command -v "$cmd" >/dev/null; then
    echo "ERROR: '$cmd' not installed on host. Run ./scripts/install.sh." >&2
    exit 1
  fi
done

# Match identify.sh's layout. apply_mapping reads $SERIES_DIR.
SERIES_DIR="./media/completed/tv"
REVIEW_DIR="$SERIES_DIR/_review"

if [[ ! -d "$REVIEW_DIR" ]]; then
  echo "Nothing to apply — $REVIEW_DIR doesn't exist."
  exit 0
fi

shopt -s nullglob
folders=()
for d in "$REVIEW_DIR"/*/; do
  [[ -d "$d" ]] || continue
  folders+=("${d%/}")
done

if (( ${#folders[@]} == 0 )); then
  echo "Nothing to apply — $REVIEW_DIR is empty."
  exit 0
fi

applied=0
skipped=0
for src in "${folders[@]}"; do
  yaml="$src/mapping.yaml"
  if [[ ! -f "$yaml" ]]; then
    echo "==> $src"
    echo "  SKIP: no mapping.yaml present"
    skipped=$((skipped + 1))
    continue
  fi

  approved=$(yq -r '.approved // false' "$yaml" 2>/dev/null || echo "false")
  if [[ "$approved" != "true" ]]; then
    echo "==> $src"
    echo "  SKIP: approved=$approved (edit mapping.yaml and set approved: true)"
    skipped=$((skipped + 1))
    continue
  fi

  if apply_manifest "$src"; then
    applied=$((applied + 1))
  else
    echo "  (left in place)"
    skipped=$((skipped + 1))
  fi
done

echo
echo "Applied: $applied  Skipped: $skipped"
if (( applied > 0 )); then
  echo "Next: ./scripts/promote.sh series"
fi

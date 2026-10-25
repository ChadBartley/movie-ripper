#!/usr/bin/env bash
# Upload completed rips from the local SSD to the SMB share, sorted by mode.
# Usage:
#   ./scripts/promote.sh movie
#   ./scripts/promote.sh series
#
# Source:      ./media/completed/<Title>/  (local SSD)
# Destination: /mnt/jellyfin/<MOVIES|SERIES subdir>/<Title>/  (SMB)
#
# Destination subdirs are configurable via .env:
#   SMB_MOVIES_SUBDIR (default: movies)
#   SMB_SERIES_SUBDIR (default: series)
# Set to empty string to upload to share root.
#
# Uses rsync --info=progress2 inside the container so you see per-file
# progress, throughput, and ETA. Source is removed after successful upload.

set -euo pipefail

if [[ "${1:-}" != "movie" && "${1:-}" != "series" ]]; then
  echo "Usage: $0 {movie|series}" >&2
  exit 1
fi

mode="$1"
cd "$(dirname "$0")/.."

# Pull SMB_*_SUBDIR overrides from .env if present
[[ -f .env ]] && set -a && . ./.env && set +a

case "$mode" in
  movie)  subdir="${SMB_MOVIES_SUBDIR-movies}" ;;
  series) subdir="${SMB_SERIES_SUBDIR-series}" ;;
esac

if ! docker compose ps --status running --services 2>/dev/null | grep -q '^arm$'; then
  echo "ERROR: arm container not running. 'docker compose up -d' first." >&2
  exit 1
fi

if ! docker compose exec -T arm mountpoint -q /mnt/jellyfin; then
  echo "ERROR: /mnt/jellyfin not mounted in container. Is the home overlay" >&2
  echo "enabled (COMPOSE_FILE in .env includes docker-compose.home.yml) and" >&2
  echo "the server reachable? Restart with 'docker compose up -d' after fixing." >&2
  exit 1
fi

# ARM 2.x layout under completed/ is:
#   completed/movies/<Title>/<Title>.mkv
#   completed/tv/<Title>/...                (video_type=series -> tv/, see ARM utils.py:166)
#   completed/unidentified/<DISC_ID>/...    (skipped — needs human review)
case "$mode" in
  movie)  src="/home/arm/media/completed/movies" ;;
  series) src="/home/arm/media/completed/tv" ;;
esac

if [[ -n "$subdir" ]]; then
  dst="/mnt/jellyfin/$subdir"
else
  dst="/mnt/jellyfin"
fi

# If ARM hasn't classified anything in this mode yet, the dir won't exist
if ! docker compose exec -T arm test -d "$src"; then
  echo "Nothing to promote — $src doesn't exist yet."
  exit 0
fi

docker compose exec -T arm mkdir -p "$dst"

if [[ "$mode" == "series" ]]; then
  # Only promote folders identify.sh / apply-reviews.sh have finalized. Skips
  # _review/ quarantines and anything else without an .identified marker
  # (e.g. mid-rip working dirs). Movies don't get this marker — ARM writes
  # them directly into completed/movies/ — so the gate is series-only.
  mapfile -t titles < <(
    docker compose exec -T arm bash -c "
      find '$src' -mindepth 1 -maxdepth 1 -type d |
      while read -r d; do
        [ -f \"\$d/.identified\" ] || continue
        basename \"\$d\"
      done | sort
    "
  )
else
  mapfile -t titles < <(
    docker compose exec -T arm bash -c \
      "find '$src' -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort"
  )
fi

if (( ${#titles[@]} == 0 )); then
  echo "Nothing to promote in $src."
  exit 0
fi

echo "Promoting ${#titles[@]} title(s) as $mode -> $dst/"
printf '  - %s\n' "${titles[@]}"
echo

failed=0
for title in "${titles[@]}"; do
  echo "==> $title"
  size=$(docker compose exec -T arm du -sh "$src/$title" 2>/dev/null | awk '{print $1}')
  echo "    size: $size"

  # -v shows each file as it transfers; --progress shows per-file progress bar.
  # No -T here: rsync uses \r to redraw the progress line, which only flushes
  # incrementally when stdout is a TTY. Other exec calls in this script keep
  # -T because they capture output rather than display it live.
  # --timeout=30: bail if SMB stops responding for 30s rather than wedging
  # in uninterruptible kernel state (CIFS soft-mount can still deadlock).
  if docker compose exec arm rsync -av --progress --timeout=30 --remove-source-files \
        "$src/$title/" "$dst/$title/"; then
    docker compose exec -T arm find "$src/$title" -type d -empty -delete 2>/dev/null || true
    echo "    OK"
  else
    echo "    FAILED — left in place at $src/$title"
    failed=$((failed+1))
  fi
  echo
done

echo
if (( failed > 0 )); then
  echo "$failed title(s) failed. Source folder(s) left intact for retry."
  exit 1
fi
echo "All $mode promotions complete."

# Clean up stale unidentified buckets — these are leftover empty folders from
# ARM's first-pass disc detection (before UDF settles). The successful
# second-pass rip already went to completed/movies/ or /series/.
stale_unidentified=$(docker compose exec -T arm bash -c \
  "find /home/arm/media/completed/unidentified -mindepth 1 -maxdepth 1 -type d -empty 2>/dev/null | wc -l")
if [[ "$stale_unidentified" -gt 0 ]]; then
  echo
  echo "Cleaning $stale_unidentified empty unidentified bucket(s)"
  docker compose exec -T arm find /home/arm/media/completed/unidentified \
    -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true
fi

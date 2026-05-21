# Shared helpers for renaming ripped mkvs into Jellyfin layout.
#
# Callers (identify.sh, apply-reviews.sh) source this file. Both produce the
# same destination layout from different sources of truth — the cascade
# (identify.sh) and an approved mapping.yaml (apply-reviews.sh) — so the
# rename logic, filename sanitization, and runtime probe live here.
#
# Callers must set $SERIES_DIR before invoking apply_mapping.

sanitize_filename() {
  # Strip filesystem-hostile chars; cap length.
  printf '%s' "$1" \
    | tr -d '/\\:*?"<>|' \
    | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//; s/\.\+$//' \
    | cut -c1-200
}

zpad() { printf '%02d' "$1"; }

# Read mkv duration in integer seconds via ffprobe.
mkv_duration_sec() {
  local f="$1" sec
  sec=$(ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null | awk '{printf "%d", $1+0.5}')
  [[ -n "$sec" && "$sec" -gt 0 ]] && printf '%s' "$sec" || printf '0'
}

# Apply a fully-resolved mapping: rename mkvs into Jellyfin layout.
# Args: source_folder show_name year tmdb_id default_season "track1.mkv:season:ep:title" ...
# Per-entry season may be empty to fall back to default_season. default_season may
# itself be empty when every entry carries its own season (multi-season discs).
# Requires: $SERIES_DIR set by caller (the completed/tv root).
apply_mapping() {
  local src="$1" show="$2" year="$3" tmdb_id="$4" default_season="$5"; shift 5
  local series_dir="$SERIES_DIR/$(sanitize_filename "$show ($year) [tmdbid-$tmdb_id]")"

  local entry track season ep title rest src_path dst_name dst_path
  local season_pad ep_pad season_dir
  for entry in "$@"; do
    track="${entry%%:*}"; rest="${entry#*:}"
    season="${rest%%:*}"; rest="${rest#*:}"
    ep="${rest%%:*}";     title="${rest#*:}"
    [[ -z "$season" ]] && season="$default_season"
    [[ -z "$season" ]] && { echo "  SKIP: track $track has no season" >&2; continue; }
    [[ -z "$ep" ]] && { echo "  SKIP: track $track has no episode mapping" >&2; continue; }
    src_path="$src/$track"
    [[ ! -f "$src_path" ]] && { echo "  SKIP: $src_path missing" >&2; continue; }

    season_pad=$(zpad "$season")
    ep_pad=$(zpad "$ep")
    season_dir="$series_dir/Season $season_pad"
    mkdir -p "$season_dir"

    if [[ -n "$title" ]]; then
      dst_name=$(sanitize_filename "$show ($year) - S${season_pad}E${ep_pad} - $title").mkv
    else
      dst_name=$(sanitize_filename "$show ($year) - S${season_pad}E${ep_pad}").mkv
    fi
    dst_path="$season_dir/$dst_name"
    if [[ -e "$dst_path" ]]; then
      echo "  COLLISION: $dst_path exists, leaving $track in place" >&2
      continue
    fi
    mv -n "$src_path" "$dst_path"
    echo "  -> $(basename "$season_dir")/$dst_name"
  done

  # Mark series dir as identified (write or update)
  cat > "$series_dir/.identified" <<EOF
tmdb_id: $tmdb_id
name: $show
year: $year
last_run: $(date -Iseconds)
EOF

  # Drop empty source folder
  rmdir "$src" 2>/dev/null || true
}

# Apply a mapping.yaml from disk: parse, validate, then call apply_mapping.
# Caller is responsible for any approval/policy gating before invoking this.
# Args: src_folder (mapping.yaml must exist directly inside it).
apply_manifest() {
  local src="$1" yaml="$src/mapping.yaml"
  echo "==> $src (manifest)"
  local show year tmdb_id default_season
  show=$(yq -r '.show.name'    "$yaml")
  year=$(yq -r '.show.year'    "$yaml")
  tmdb_id=$(yq -r '.show.tmdb_id' "$yaml")
  default_season=$(yq -r '.season // ""' "$yaml")
  [[ "$default_season" == "null" ]] && default_season=""
  for f in show year tmdb_id; do
    if [[ "${!f}" == "null" || -z "${!f}" ]]; then
      echo "  ABORT: mapping.yaml missing field: $f" >&2
      return 1
    fi
  done

  local entries=() tracks track season ep title
  mapfile -t tracks < <(yq -r '.tracks | keys | .[]' "$yaml")
  for track in "${tracks[@]}"; do
    ep=$(yq -r ".tracks[\"$track\"].episode" "$yaml")
    title=$(yq -r ".tracks[\"$track\"].title // \"\"" "$yaml")
    season=$(yq -r ".tracks[\"$track\"].season // \"\"" "$yaml")
    [[ "$season" == "null" ]] && season=""
    [[ -z "$season" ]] && season="$default_season"
    if [[ "$ep" == "null" || -z "$ep" ]]; then
      echo "  SKIP track $track: episode is empty" >&2
      continue
    fi
    if [[ -z "$season" ]]; then
      echo "  SKIP track $track: no season (set 'season:' on the track or at the top level)" >&2
      continue
    fi
    [[ "$title" == "null" ]] && title=""
    entries+=("$track:$season:$ep:$title")
  done

  if (( ${#entries[@]} == 0 )); then
    echo "  ABORT: no track mappings to apply" >&2
    return 1
  fi

  rm -f "$yaml"
  apply_mapping "$src" "$show" "$year" "$tmdb_id" "$default_season" "${entries[@]}"
}

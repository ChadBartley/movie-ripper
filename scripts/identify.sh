#!/usr/bin/env bash
# Identify ripped TV episodes and rename them into Jellyfin layout.
#
# Reads:   ./media/completed/tv/<Folder>/*.mkv
#          ./media/completed/unidentified/<Folder>/*.mkv  (also scanned)
# Writes:  ./media/completed/tv/<Show> (Year) [tmdbid-N]/Season NN/<Show> (Year) - SNNENN - Title.mkv
# Quarantines low-confidence rips to ./media/completed/tv/_review/<Folder>/ with a mapping.yaml.
#
# Idempotent. Run between rip and ./scripts/promote.sh series.
#
# Cascade per folder:
#   1. Parse disc label (folder name) for show + season + disc number
#   2. TMDB show search
#   3. TMDB season fetch (episode list with runtimes)
#   4. Match track runtimes to expected episodes (by disc number, else by sequence)
#   5. Score; HIGH -> rename in place; lower -> quarantine to _review/ for manual fix
#
# Manual override: if a folder under tv/ contains mapping.yaml, the cascade is
# skipped and the rename happens per the manifest. To apply quarantines from
# _review/, use ./scripts/apply-reviews.sh — it sweeps approved manifests.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck source=lib/manifest.sh
. "$(dirname "$0")/lib/manifest.sh"

# --- Setup ---------------------------------------------------------------

if [[ ! -f .env ]]; then
  echo "ERROR: .env missing. Copy .env.example to .env and fill in." >&2
  exit 1
fi
set -a; . ./.env; set +a

if [[ -z "${TMDB_API_KEY:-}" ]]; then
  echo "ERROR: TMDB_API_KEY not set in .env. See .env.example." >&2
  exit 1
fi

for cmd in curl jq yq ffprobe; do
  if ! command -v "$cmd" >/dev/null; then
    echo "ERROR: '$cmd' not installed on host. Run ./scripts/install.sh." >&2
    exit 1
  fi
done

# ARM maps video_type="series" to completed/tv/ (utils.py:166-167), not series/.
SERIES_DIR="./media/completed/tv"
UNID_DIR="./media/completed/unidentified"
REVIEW_DIR="$SERIES_DIR/_review"
TMDB_CACHE="./media/.tmdb-cache"
mkdir -p "$SERIES_DIR" "$REVIEW_DIR" "$TMDB_CACHE"

TMDB_API="https://api.themoviedb.org/3"
RUNTIME_TOLERANCE_SEC=120   # ±2 min
SCORE_HIGH=8

# --- TMDB ----------------------------------------------------------------

# Cached GET. Args: cache_key, url
tmdb_get() {
  local key="$1" url="$2" cache="$TMDB_CACHE/$1.json"
  if [[ -s "$cache" ]]; then
    cat "$cache"; return
  fi
  local body http
  body=$(curl -fsSL -w '\n%{http_code}' "$url" 2>/dev/null || true)
  http=$(printf '%s' "$body" | tail -n1)
  body=$(printf '%s' "$body" | sed '$d')
  if [[ "$http" != "200" ]]; then
    echo "TMDB $url -> HTTP $http" >&2
    return 1
  fi
  printf '%s' "$body" | tee "$cache"
}

tmdb_search_show() {
  local name="$1" year="${2:-}"
  local query
  query=$(jq -rn --arg s "$name" '$s|@uri')
  local key="search-$(echo -n "$name" | tr '[:upper:] /' '[:lower:]__' | tr -cd 'a-z0-9_-')"
  local url="$TMDB_API/search/tv?api_key=$TMDB_API_KEY&query=$query"
  if [[ -n "$year" ]]; then
    key="${key}-${year}"
    url="${url}&first_air_date_year=${year}"
  fi
  tmdb_get "$key" "$url"
}

tmdb_get_season() {
  local tmdb_id="$1" season="$2"
  tmdb_get "show-${tmdb_id}-s${season}" \
    "$TMDB_API/tv/$tmdb_id/season/$season?api_key=$TMDB_API_KEY"
}

# --- Label parsing -------------------------------------------------------

# Parse a folder name into "show|season|disc" (season/disc may be empty).
# Tries patterns: <show>_S<n>_D<m>, <show>_SEASON_<n>_DISC_<m>, <show>_S<n>.
parse_label() {
  local raw="$1"
  local norm
  # collapse separators to underscores, uppercase for matching, drop trailing junk
  norm=$(printf '%s' "$raw" | tr ' .-' '___' | tr '[:lower:]' '[:upper:]')

  # ARM-style folders often carry a trailing disc serial / UPC barcode
  # (e.g. THE_OFFICE_177750283084). Strip 6+ digit trailing tokens so the
  # TMDB search isn't poisoned. Years (4 digits) and S/D numbers (1-2) survive.
  norm=$(printf '%s' "$norm" | sed -E 's/_[0-9]{6,}$//')

  # Peel off a trailing parenthesized year / year-range hint like (2005) or
  # (2005–2013) (em-dash or hyphen). The year constrains the TMDB search so
  # same-name shows (e.g. the 2024 remake of a 2005 series) don't win on rank.
  local year=""
  if [[ "$norm" =~ \(([0-9]{4})[^\)]*\)$ ]]; then
    year="${BASH_REMATCH[1]}"
  fi
  norm=$(printf '%s' "$norm" | sed -E 's/_?\([0-9]{4}[^)]*\)$//')

  local show season disc

  if [[ "$norm" =~ ^(.+)_S([0-9]{1,2})_D([0-9]{1,2})$ ]]; then
    show="${BASH_REMATCH[1]}" season="${BASH_REMATCH[2]}" disc="${BASH_REMATCH[3]}"
  elif [[ "$norm" =~ ^(.+)_SEASON_([0-9]{1,2})_DISC_([0-9]{1,2})$ ]]; then
    show="${BASH_REMATCH[1]}" season="${BASH_REMATCH[2]}" disc="${BASH_REMATCH[3]}"
  elif [[ "$norm" =~ ^(.+)_S([0-9]{1,2})D([0-9]{1,2})$ ]]; then
    # Glued disc label with no separator between S and D, e.g.
    # ADVENTURE_TIME_S01D01. Without this the whole "S01D01" stays glued to the
    # show name and poisons the TMDB search.
    show="${BASH_REMATCH[1]}" season="${BASH_REMATCH[2]}" disc="${BASH_REMATCH[3]}"
  elif [[ "$norm" =~ ^(.+)_S([0-9]{1,2})$ ]]; then
    show="${BASH_REMATCH[1]}" season="${BASH_REMATCH[2]}" disc=""
  elif [[ "$norm" =~ ^(.+)_SEASON_([0-9]{1,2})$ ]]; then
    show="${BASH_REMATCH[1]}" season="${BASH_REMATCH[2]}" disc=""
  else
    show="$norm" season="" disc=""
  fi

  # Underscores back to spaces, title-case (rough — TMDB will give canonical name)
  show=$(printf '%s' "$show" | tr '_' ' ' | sed 's/  */ /g; s/^ //; s/ $//')

  # Strip leading numeric disc count if 0
  season="${season#0}"
  disc="${disc#0}"

  printf '%s|%s|%s|%s\n' "$show" "$season" "$disc" "$year"
}

# --- Cascade per folder --------------------------------------------------

# (apply_mapping, sanitize_filename, zpad, mkv_duration_sec live in
# scripts/lib/manifest.sh — sourced above. They're shared with apply-reviews.sh.)

# Build a mapping.yaml in the source folder for human triage.
# Args: src show_name year tmdb_id season disc reasons "track1:ep:title:rdisc:rtmdb" ...
write_review_yaml() {
  local src="$1" show="$2" year="$3" tmdb_id="$4" season="$5" disc="$6" reasons="$7"; shift 7
  {
    echo "# identify.sh quarantine — review the mapping below, fix any wrong"
    echo "# episode/title fields (or set 'episode: ~' to skip a track), then set"
    echo "# 'approved: true' at the bottom and run ./scripts/apply-reviews.sh."
    echo "# Cascade reasons:"
    printf '%s\n' "$reasons" | sed 's/^/#   - /'
    echo
    echo "show:"
    echo "  tmdb_id: ${tmdb_id:-~}"
    echo "  name: \"${show//\"/\\\"}\""
    echo "  year: ${year:-~}"
    echo
    echo "season: ${season:-~}"
    echo "disc: ${disc:-~}"
    echo
    echo "tracks:"
    local entry track tseason ep title rdisc rtmdb
    for entry in "$@"; do
      IFS=':' read -r track tseason ep title rdisc rtmdb <<<"$entry"
      echo "  $track:"
      echo "    episode: ${ep:-~}"
      # Per-track season only emitted when it differs from the disc default;
      # apply_manifest falls back to the top-level 'season:' otherwise.
      if [[ -n "$tseason" && "$tseason" != "$season" ]]; then
        echo "    season: $tseason"
      fi
      if [[ -n "$title" ]]; then
        echo "    title: \"${title//\"/\\\"}\""
      else
        echo "    title: ~"
      fi
      echo "    runtime_disc: ${rdisc:-~}"
      echo "    runtime_tmdb: ${rtmdb:-~}"
    done
    echo
    echo "# Flip to true once the mapping above is correct."
    echo "approved: false"
  } > "$src/mapping.yaml"
}

# --- Dialogue-based identification ---------------------------------------

# Build a JSON job and pipe it to dialogue_identify.py.
# Args: src show year tmdb_id reasons_so_far track1.mkv track2.mkv ...
dialogue_identify() {
  local src="$1" show="$2" year="$3" tmdb_id="$4" prior_reasons="$5"; shift 5
  local tracks=("$@")
  local n=${#tracks[@]}

  echo "  dialogue: building job for $n tracks..."

  local job_tracks='[]' t
  for t in "${tracks[@]}"; do
    job_tracks=$(jq --arg n "$t" --arg p "$src/$t" \
      '. + [{name: $n, path: $p}]' <<<"$job_tracks")
  done
  local job
  job=$(jq -n \
    --arg show "$show" --arg year "$year" --arg id "$tmdb_id" \
    --argjson tracks "$job_tracks" \
    '{show: $show,
      year:    ($year | tonumber? // null),
      tmdb_id: ($id   | tonumber? // null),
      tracks:  $tracks}')

  local result rc
  result=$(printf '%s' "$job" | "$(dirname "$0")/dialogue_identify.py")
  rc=$?
  if (( rc == 3 )); then
    echo "  dialogue: cache missing for tmdb_id=$tmdb_id; pre-warm with:" >&2
    echo "    ./scripts/fetch_transcripts.py --tmdb-id $tmdb_id" >&2
    quarantine "$src" "$show" "$year" "$tmdb_id" "" "" \
      "$prior_reasons"$'\nTranscript cache missing — run fetch_transcripts.py' \
      "${tracks[@]}"
    return 1
  fi
  if (( rc != 0 )); then
    quarantine "$src" "$show" "$year" "$tmdb_id" "" "" \
      "$prior_reasons"$'\ndialogue_identify failed (rc='"$rc"')' "${tracks[@]}"
    return 1
  fi

  # Validate per-track results against TMDB runtime as a sanity check.
  # Multi-season discs are fine — apply_mapping carries season per-entry.
  local entries=() rev_entries=() reasons="$prior_reasons"
  local all_pass=1
  local t s e title conf reason rdisc_sec rdisc_h tmdb_runtime exp_sec season_json rtmdb_h
  for t in "${tracks[@]}"; do
    s=$(jq -r --arg t "$t" '.tracks[$t].season    // empty' <<<"$result")
    e=$(jq -r --arg t "$t" '.tracks[$t].episode   // empty' <<<"$result")
    title=$(jq -r --arg t "$t" '.tracks[$t].title // empty' <<<"$result")
    conf=$(jq -r  --arg t "$t" '.tracks[$t].confidence // "low"' <<<"$result")
    reason=$(jq -r --arg t "$t" '.tracks[$t].reasoning // empty' <<<"$result")
    rdisc_sec=$(mkv_duration_sec "$src/$t")
    rdisc_h=$(printf '%dm%02ds' $((rdisc_sec/60)) $((rdisc_sec%60)))

    tmdb_runtime=0
    if [[ -n "$s" && -n "$e" ]] && season_json=$(tmdb_get_season "$tmdb_id" "$s" 2>/dev/null); then
      tmdb_runtime=$(jq -r --argjson e "$e" \
        '.episodes[] | select(.episode_number == $e) | .runtime // 0' <<<"$season_json")
      [[ -z "$tmdb_runtime" || "$tmdb_runtime" == "null" ]] && tmdb_runtime=0
    fi
    exp_sec=$(( tmdb_runtime * 60 ))
    rtmdb_h=""
    (( tmdb_runtime > 0 )) && rtmdb_h=$(printf '%dm00s' "$tmdb_runtime")

    local runtime_ok=0
    (( exp_sec > 0 \
       && rdisc_sec >= exp_sec - RUNTIME_TOLERANCE_SEC \
       && rdisc_sec <= exp_sec + RUNTIME_TOLERANCE_SEC )) && runtime_ok=1

    echo "  dialogue: $t -> S${s:-?}E${e:-?} '$title' [conf=$conf, runtime $rdisc_h vs $rtmdb_h, $reason]"

    if [[ "$conf" == "high" && "$runtime_ok" == "1" && -n "$s" && -n "$e" ]]; then
      entries+=("$t:$s:$e:$title")
      rev_entries+=("$t:$s:$e:$title:$rdisc_h:$rtmdb_h")
    else
      all_pass=0
      rev_entries+=("$t:${s:-}:${e:-}:${title}:$rdisc_h:$rtmdb_h")
      reasons+=$'\nDialogue: '"$t S${s:-?}E${e:-?} conf=$conf — $reason"
    fi
  done

  if (( all_pass )) && (( ${#entries[@]} == n )); then
    echo "  dialogue: all $n tracks high-confidence + runtime-validated"
    apply_mapping "$src" "$show" "$year" "$tmdb_id" "" "${entries[@]}"
    return 0
  fi

  quarantine "$src" "$show" "$year" "$tmdb_id" "" "" \
    "$reasons" "${rev_entries[@]}"
}

# Main per-folder cascade.
process_folder() {
  local src="$1"
  local base; base=$(basename "$src")

  # Skip already-identified Jellyfin-shaped dirs
  [[ "$base" =~ \[tmdbid-[0-9]+\] ]] && return 0
  # Skip _review directory itself
  [[ "$base" == "_review" ]] && return 0

  echo "==> $src"

  # Manual override path
  if [[ -f "$src/mapping.yaml" ]]; then
    apply_manifest "$src" || echo "  (left in place)"
    return
  fi

  # Collect mkv tracks (sorted by name; preserves MakeMKV scan order)
  local tracks=()
  mapfile -t tracks < <(find "$src" -maxdepth 1 -type f -name '*.mkv' -printf '%f\n' | sort -V)
  if (( ${#tracks[@]} == 0 )); then
    echo "  no mkvs, skipping"
    return
  fi

  # Tier 1 — label parse
  local parsed show_guess season disc label_year
  parsed=$(parse_label "$base")
  IFS='|' read -r show_guess season disc label_year <<<"$parsed"
  echo "  label: show='$show_guess' season='${season:-?}' disc='${disc:-?}' year='${label_year:-?}'"

  local reasons=""
  local score=0

  # Tier 2 — TMDB show search
  local search_json show_name year tmdb_id show_count
  if ! search_json=$(tmdb_search_show "$show_guess" "$label_year"); then
    reasons+=$'\nTMDB show search failed (network or auth)'
    quarantine "$src" "$show_guess" "" "" "$season" "$disc" "$reasons" "${tracks[@]}"
    return
  fi
  show_count=$(echo "$search_json" | jq '.results | length')
  if [[ "$show_count" -eq 0 ]]; then
    reasons+=$'\nNo TMDB show match for label'
    quarantine "$src" "$show_guess" "" "" "$season" "$disc" "$reasons" "${tracks[@]}"
    return
  fi
  show_name=$(echo "$search_json" | jq -r '.results[0].name')
  year=$(echo "$search_json"      | jq -r '.results[0].first_air_date | (. // "")[0:4]')
  tmdb_id=$(echo "$search_json"   | jq -r '.results[0].id')
  echo "  TMDB: $show_name ($year) [id=$tmdb_id]"
  score=$((score + 3))
  if [[ "$show_count" -gt 1 ]]; then
    local pop1 pop2
    pop1=$(echo "$search_json" | jq -r '.results[0].popularity')
    pop2=$(echo "$search_json" | jq -r '.results[1].popularity // 0')
    awk "BEGIN{exit !(${pop1:-0} > ${pop2:-0}*2)}" || {
      score=$((score - 1))
      reasons+=$'\nMultiple plausible TMDB matches; chose top by popularity'
    }
  fi

  # No season parsed from the label — dialogue identifier handles unknown season.
  if [[ -z "$season" ]]; then
    reasons+=$'\nSeason not parsed from label; using dialogue identifier'
    dialogue_identify "$src" "$show_name" "$year" "$tmdb_id" "$reasons" "${tracks[@]}"
    return
  fi
  score=$((score + 2))
  [[ -n "$disc" ]] && score=$((score + 1))

  # Tier 3 — season fetch
  local season_json
  if ! season_json=$(tmdb_get_season "$tmdb_id" "$season"); then
    reasons+=$'\nTMDB season fetch failed'
    quarantine "$src" "$show_name" "$year" "$tmdb_id" "$season" "$disc" "$reasons" "${tracks[@]}"
    return
  fi
  local total_eps; total_eps=$(echo "$season_json" | jq '.episodes | length')
  if [[ "$total_eps" -eq 0 ]]; then
    reasons+=$'\nTMDB returned no episodes for that season'
    quarantine "$src" "$show_name" "$year" "$tmdb_id" "$season" "$disc" "$reasons" "${tracks[@]}"
    return
  fi
  echo "  season $season has $total_eps episodes on TMDB"

  # Tier 4 — match runtimes
  local n=${#tracks[@]}
  local first_ep=1
  if [[ -n "$disc" ]]; then
    # Estimate episodes-per-disc from track count
    first_ep=$(( (disc - 1) * n + 1 ))
  fi

  local matches=0 entries=() rev_entries=()
  local i=0 t track ep_idx ep_num ep_title ep_runtime rdisc_sec
  for t in "${tracks[@]}"; do
    rdisc_sec=$(mkv_duration_sec "$src/$t")
    ep_idx=$((first_ep - 1 + i))
    ep_num=$(echo "$season_json" | jq -r ".episodes[$ep_idx].episode_number // empty")
    ep_title=$(echo "$season_json" | jq -r ".episodes[$ep_idx].name // empty")
    ep_runtime=$(echo "$season_json" | jq -r ".episodes[$ep_idx].runtime // 0")
    local exp_sec=$(( ep_runtime * 60 ))
    local rdisc_h=$(printf '%dm%02ds' $((rdisc_sec/60)) $((rdisc_sec%60)))
    local rtmdb_h=""
    [[ "$ep_runtime" -gt 0 ]] && rtmdb_h=$(printf '%dm00s' "$ep_runtime")

    if [[ -n "$ep_num" ]] && (( exp_sec > 0 )) \
       && (( rdisc_sec >= exp_sec - RUNTIME_TOLERANCE_SEC )) \
       && (( rdisc_sec <= exp_sec + RUNTIME_TOLERANCE_SEC )); then
      matches=$((matches + 1))
      entries+=("$t:$season:$ep_num:$ep_title")
      rev_entries+=("$t:$season:$ep_num:$ep_title:$rdisc_h:$rtmdb_h")
    else
      rev_entries+=("$t:$season::${ep_title:-}:$rdisc_h:$rtmdb_h")
    fi
    i=$((i + 1))
  done

  echo "  runtime match: $matches/$n tracks within ±${RUNTIME_TOLERANCE_SEC}s"
  if [[ "$matches" -eq "$n" ]]; then
    score=$((score + 3))
    if [[ -n "$disc" ]] && (( total_eps % n == 0 )); then
      score=$((score + 1))
    fi
  fi

  echo "  score: $score (HIGH >= $SCORE_HIGH)"

  if (( score >= SCORE_HIGH )) && (( matches == n )); then
    apply_mapping "$src" "$show_name" "$year" "$tmdb_id" "$season" "${entries[@]}"
  else
    reasons+=$'\nConfidence below HIGH threshold ('"$score"'/'"$SCORE_HIGH"'); '"$matches"'/'"$n"' tracks matched'
    quarantine "$src" "$show_name" "$year" "$tmdb_id" "$season" "$disc" "$reasons" "${rev_entries[@]}"
  fi
}

quarantine() {
  local src="$1" show="$2" year="$3" tmdb_id="$4" season="$5" disc="$6" reasons="$7"; shift 7
  local entries=("$@")
  # Normalize entries to the 6-field review format: track:season:ep:title:rdisc:rtmdb.
  # Callers may pass plain track names or full review lines.
  local norm=() e
  for e in "${entries[@]}"; do
    case "$e" in
      *:*:*:*:*:*) norm+=("$e") ;;
      *.mkv)       norm+=("$e::::$( mkv_duration_sec "$src/$e" 2>/dev/null | awk '{printf "%dm%02ds", $1/60, $1%60}'):") ;;
      *)           norm+=("$e:::::") ;;
    esac
  done

  local base; base=$(basename "$src")
  local target="$REVIEW_DIR/$base"
  if [[ "$src" != "$target" ]]; then
    if [[ -e "$target" ]]; then
      target="$REVIEW_DIR/${base}.$(date +%s)"
    fi
    mkdir -p "$REVIEW_DIR"
    mv "$src" "$target"
  fi
  write_review_yaml "$target" "$show" "$year" "$tmdb_id" "$season" "$disc" "$reasons" "${norm[@]}"
  echo "  -> _review/$(basename "$target")/mapping.yaml"
}

# --- Main walk -----------------------------------------------------------

shopt -s nullglob
folders=()
for d in "$SERIES_DIR"/*/ "$UNID_DIR"/*/; do
  [[ -d "$d" ]] || continue
  folders+=("${d%/}")
done

if (( ${#folders[@]} == 0 )); then
  echo "Nothing to identify."
  exit 0
fi

for f in "${folders[@]}"; do
  process_folder "$f"
done

echo
echo "Done."
if compgen -G "$REVIEW_DIR/*/mapping.yaml" > /dev/null; then
  echo "Quarantines remain in $REVIEW_DIR/. Edit each mapping.yaml, set"
  echo "approved: true, then run ./scripts/apply-reviews.sh."
fi
echo "Then: ./scripts/promote.sh series."

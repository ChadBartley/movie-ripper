# movie-ripper

Tracker repo for [Automatic Ripping Machine](https://github.com/automatic-ripping-machine/automatic-ripping-machine) deployments. Edit/tweak here, `git pull` on the machine that has the optical drive.

Target: Manjaro hosts with Nvidia GPUs (NVENC), shipping rips to an SMB share at `//192.168.1.10/data` (example — set yours in `.env`).

## Status

Personal hobby project. Currently identifying TV episodes at 185/186 across
_The Office_ (US) using the audio-fingerprint + transcript-match pipeline
described below. The sole miss just didn't have a transcript I could grab —
trivial to handle by hand at that rate. Movies pass through ARM unchanged
with the usual `MINLENGTH` / `MAXLENGTH` tuning.

## Disclaimer (please read)

This is a personal hobby setup published as a reference. **It is not a
product, not a service, and not advice.** A few things worth being clear
about:

- **Ripping.** Actual disc decryption and ripping is performed by
  [Automatic Ripping Machine](https://github.com/automatic-ripping-machine/automatic-ripping-machine)
  and the underlying tools it bundles (MakeMKV, HandBrake, libdvdcss, etc.).
  This repo does not implement any of that; it just configures and orchestrates ARM.
  Use ARM only with discs you legally own, in jurisdictions where format
  shifting for personal backup is permitted. Check your local law.
- **Transcript scraping.** `scripts/fetch_transcripts.py` fetches plain
  dialogue text from public transcript sites
  ([subslikescript.com](https://subslikescript.com),
  [Springfield! Springfield!](https://www.springfieldspringfield.co.uk),
  and per-show [Fandom](https://www.fandom.com/) wikis via their MediaWiki
  API) and caches it locally on your own machine. Cached transcripts are
  never redistributed by this repo. Whether you scrape, how often, and at
  what rate is **your responsibility** under each site's Terms of Service
  and applicable law.
- **No warranty.** See [LICENSE](./LICENSE).

I am not a lawyer and nothing here is legal advice. If you are unsure
whether something is allowed where you live, don't do it.

## What's in here

```
docker-compose.yml         # the deploy unit
Dockerfile                 # thin layer over upstream ARM image (adds rsync)
config/
  arm.yaml.example         # committed template for ARM main config
  arm.yaml                 # gitignored — your real config (OMDB key etc)
  apprise.yaml             # notifications (off)
  abcde.conf               # CD ripping
scripts/
  install.sh               # first-time Manjaro setup; bootstraps arm.yaml
  update.sh                # git pull && docker compose up -d
  identify.sh              # match series rips to TMDB and rename for Jellyfin
  apply-reviews.sh         # sweep approved manifests in completed/tv/_review/
  fetch_transcripts.py     # pre-warm transcript cache for a show
  promote.sh movie|series  # upload completed rips from SSD -> SMB share
  noop-init.sh             # no-op shim that disables syslog-ng (Manjaro fix)
  run_tests.sh             # ruff + pytest
.env.example               # copy to .env (gitignored)
```

## How a rip flows

```
disc -> MakeMKV   -> ./media/raw/        (local SSD, deleted after transcode)
     -> HandBrake -> ./media/transcode/  (local SSD)
     -> ARM       -> ./media/completed/<Title>/<Title>.mkv  (local SSD, final)

         ----- decoupled from the server -----

     -> promote.sh -> /mnt/jellyfin/{movies,series}/<Title>/  (SMB upload)
```

ARM never writes to the server during the rip pipeline — it stays on the local SSD until you run `promote.sh`. Server down or laptop offline: keep ripping; promote later.

## Host dependencies (Manjaro)

Everything ripping-related (MakeMKV, HandBrake, abcde, ffmpeg, the ARM web UI) lives **inside the container** — you don't install those on the host. The host only needs the bits that let the container run, talk to the GPU, and mount the SMB share:

```bash
sudo pacman -S --needed docker docker-compose cifs-utils lsscsi nvidia-container-toolkit yq
sudo systemctl enable --now docker
sudo usermod -aG docker $USER          # then log out + back in
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

| Package                     | Why                                                                      |
| --------------------------- | ------------------------------------------------------------------------ |
| `docker` + `docker-compose` | The runtime.                                                             |
| `cifs-utils`                | Kernel-side SMB mount; the docker volume driver calls into `mount.cifs`. |
| `lsscsi`                    | Used to identify which `/dev/sr*` is which drive.                        |
| `nvidia-container-toolkit`  | Exposes the Nvidia GPU to the container for NVENC.                       |

`./scripts/install.sh` runs all of the above for you and is idempotent — safe either way.

## First-time setup on a rip machine

```bash
git clone <this repo> && cd movie-ripper

# 1. Install host deps (one of these — both work):
sudo pacman -S --needed docker docker-compose cifs-utils lsscsi nvidia-container-toolkit
# OR:
./scripts/install.sh

# 2. Plug in the optical drive(s), then identify them:
lsscsi -g

# 3. Configure .env (gitignored)
cp .env.example .env
$EDITOR .env                  # fill in SMB creds, OMDB key, UID/GID, drive list

# 4. Configure arm.yaml (gitignored). install.sh creates it; if you skipped that:
cp config/arm.yaml.example config/arm.yaml
$EDITOR config/arm.yaml       # paste OMDB_API_KEY into the OMDB_API_KEY: "" line

# 5. Log out + back in (so the docker group takes effect), then:
docker compose up -d
```

Web UI at `http://<host>:8090` — default login `admin` / `password`.

### Sanity checks before inserting a disc

```bash
docker compose exec arm nvidia-smi              # GPU visible inside container?
docker compose exec arm ls /mnt/jellyfin        # SMB mount working?
docker compose exec arm grep OMDB_API_KEY /etc/arm/config/arm.yaml
                                                 # key set? (not just "")
```

### Day-to-day rip workflow

```bash
# Insert disc. Watch progress:
docker compose logs -f arm | grep -v check_hw_transcode_support
# (or open the web UI)

# When ARM finishes, the file is at ./media/completed/<Title>/.
# When you're ready to upload to the server, batch-promote:
./scripts/promote.sh movie       # or: ./scripts/promote.sh series

# For TV, run identify.sh between rip and promote to rename episodes
# into Jellyfin's S01E01 layout via TMDB lookup:
./scripts/identify.sh

# Anything identify.sh couldn't auto-resolve gets quarantined to
# media/completed/tv/_review/. Edit mapping.yaml, set approved: true, then:
./scripts/apply-reviews.sh

# Anything ARM couldn't identify at all lands in ./media/completed/unidentified/.
# Rename the folder to "Title (Year)" before promoting.
```

### Series identification (`identify.sh` + `fetch_transcripts.py`)

ARM is unreliable at mapping MakeMKV tracks to specific TV episodes. The pipeline:

1. **Pre-warm transcripts** for the show (one-time per show):

   ```bash
   ./scripts/fetch_transcripts.py "The Office" --year 2005
   ```

   Walks a fallback ladder of subslikescript.com → Springfield! Springfield!
   → per-show Fandom wikis (e.g. `spongebob.fandom.com`, via the MediaWiki
   API). Cached at `./media/.subs-cache/<tmdb_id>/`.

   To limit fetching to a subset of seasons (useful when you only own discs
   for some of them — e.g. *Supernatural* S1–S2), pass `--seasons`:

   ```bash
   ./scripts/fetch_transcripts.py --tmdb-id 1622 --seasons 1-2
   ./scripts/fetch_transcripts.py --tmdb-id 1622 --seasons 1,3,5-7
   ```

   **Pick the right source per show with `--sources`.** The flag is an
   ordered allowlist: order = precedence, omitted names are skipped. Default
   is `subslikescript,springfield,fandom` — Fandom is last because it
   auto-discovers the wiki host (e.g. `spongebob.fandom.com`) and verifies
   it by probing 5 sample episode titles, which costs an extra HTTP round
   trip. For animated/cult shows where Fandom has the best coverage, put it
   first:

   ```bash
   ./scripts/fetch_transcripts.py "SpongeBob SquarePants" --sources fandom
   ./scripts/fetch_transcripts.py --tmdb-id 387 --sources fandom,subslikescript
   ```

   **Watch for transcript/track desync on SubsLikeScript.** SLS's slugs
   sometimes carry the wrong transcript under the right URL (observed on
   *The Office* US S4 — the `episode-9-Dinner_Party` page actually serves
   Local Ad's text). Pass a Springfield index URL as the positional argument
   to seed-prime that source's resolver and skip discovery for that run:

   ```bash
   ./scripts/fetch_transcripts.py \
     "https://www.springfieldspringfield.co.uk/episode_scripts.php?tv-show=the-office-us" \
     --tmdb-id 2316
   ```

   After fetching, spot-check a few cached files (`head -c 200
   media/.subs-cache/<tmdb_id>/sNN/eNN-*.txt`) against the episode's actual
   cold open before trusting the cache for identification.

2. **Rip discs** as usual.

3. **Identify** — `./scripts/identify.sh` runs a cascade per ripped folder:
   - Parse the disc label for show / season / disc when present.
   - Look up the show on TMDB.
   - For each MKV track: extract 3 min of audio, transcribe with faster-whisper,
     and fuzzy-match against the cached transcripts via rapidfuzz `partial_ratio`.
   - Solve track→episode assignment with a contiguity bonus on adjacent tracks.
   - Score the result. **HIGH confidence** → rename in place to
     `Show (Year) [tmdbid-N]/Season NN/Show (Year) - SNNENN - Title.mkv`.
     Anything lower → quarantine to `media/completed/tv/_review/<Folder>/` with a
     `mapping.yaml` for manual triage.

To fix a quarantined rip:

1. Edit `media/completed/tv/_review/<Folder>/mapping.yaml` — fill in any `~`
   fields (or set `episode: ~` to skip a track). For a disc that spans
   multiple seasons, set `season:` per track under the track entry; the
   top-level `season:` is the default for tracks that don't specify one.
2. Set `approved: true` at the top of the file.
3. Run `./scripts/apply-reviews.sh` — sweeps `_review/` for every approved
   manifest and renames the tracks into the same Jellyfin layout the
   high-confidence path uses. Source folders empty out and disappear from
   `_review/` on success.

Then `./scripts/promote.sh series` as usual.

#### Known limitation: special features

Bonus features, deleted scenes, gag reels, webisodes, and other DVD/Blu-ray
extras **are not identified**. The pipeline matches against TMDB's episode
list — anything not in TMDB has no target to match to, and the public
transcript sites don't carry extras either. In practice on *The Office*
(US) this accounted for 1 unmatched track out of 186; it lands in
`_review/` like any other miss. Either rename + promote it manually, or
delete it.

Same caveat applies to any episode TMDB happens to be missing data for
(rare, but it happens — e.g. some specials, region-only episodes, or
shows where TMDB's S/E numbering disagrees with the disc layout).

**Manual transcript override**: if a show isn't on the public sources, drop a
plain-text transcript at
`./media/.subs-cache/<tmdb_id>/sNN/eNN-Episode_Title.txt`. The identifier picks
it up on the next run.

The script also scans `media/completed/unidentified/`, so series ARM
mis-classified are picked up automatically.

### .env credentials

The `.env` file is **gitignored**. You'll fill it in fresh on each machine. Required fields:

| Var                         | What                                                                         |
| --------------------------- | ---------------------------------------------------------------------------- |
| `ARM_UID` / `ARM_GID`       | `id -u` / `id -g` of the host user                                           |
| `TZ`                        | e.g. `America/Los_Angeles`                                                   |
| `ARM_DRIVES`                | Comma list of drives. Find with `lsscsi -g`.                                 |
| `OMDB_API_KEY`              | Free key from omdbapi.com                                                    |
| `TMDB_API_KEY`              | Free key from themoviedb.org (for `identify.sh`)                             |
| `SMB_USER` / `SMB_PASSWORD` | Credentials for the SMB share defined by `SMB_HOST`/`SMB_SHARE`              |
| `SCRAPER_CONTACT`           | Optional. URL/email appended to outbound User-Agent for transcript scraping. |

### Adding a second optical drive

`docker-compose.yml` currently passes through `/dev/sr0` only. To add more drives, edit the `devices:` block:

```yaml
devices:
  - /dev/sr0:/dev/sr0
  - /dev/sr1:/dev/sr1
```

(Compose can't expand a comma-list env var into multiple device entries, so this stays manual.)

## Updating

```bash
./scripts/update.sh
```

Pulls the latest config from the repo, rebuilds the local image against the latest upstream ARM base, and restarts the container.

## Tuning notes

- **Mode: movies (current default).** `MINLENGTH=900` / `MAXLENGTH=99999` in `config/arm.yaml` — drops bonus shorts, lets full movies through. To rip TV, flip `MAXLENGTH` to `2700` (45 min) and set `ALLOW_DUPLICATES: true`.
- **NVENC.** HandBrake presets are `H.265 NVENC 1080p` / `H.265 NVENC 2160p 4K`. List all available presets with `docker exec arm HandBrakeCLI -z`.
- **Local config (gitignored).** `config/arm.yaml.example` is the committed template. On first `./scripts/install.sh` it's copied to `config/arm.yaml` (gitignored) — paste your `OMDB_API_KEY` there, never in the example.
- **Promotion flow (decoupled from server).** ARM writes the final transcoded folder to the local SSD at `./media/completed/<Title>/`. ARM keeps working even if the file server is offline. When the server is reachable, run:
  ```bash
  ./scripts/promote.sh movie     # uploads completed/* -> SMB movies/
  ./scripts/promote.sh series    # uploads completed/* -> SMB series/
  ```
  This does the actual SSD→SMB upload (one cross-fs `mv` per title, via the container's SMB mount). `completed/unidentified/` is skipped — fix the title in the ARM web UI or rename the folder before promoting.
- **Notifications off.** `apprise.yaml` is empty. To enable: set `APPRISE: "/etc/arm/config/apprise.yaml"` in `arm.yaml` and add a service URL.

## License

[MIT](./LICENSE).

## Acknowledgements

This project is mostly glue. The hard parts are done by other people's
software and other people's data:

**Ripping pipeline**

- [Automatic Ripping Machine](https://github.com/automatic-ripping-machine/automatic-ripping-machine) — the orchestrator this repo deploys.
- [MakeMKV](https://www.makemkv.com/) — disc decryption + track extraction.
- [HandBrake](https://handbrake.fr/) — transcoding (NVENC presets).
- [libdvdcss](https://www.videolan.org/developers/libdvdcss.html) — CSS decryption for DVDs.
- [abcde](https://abcde.einval.com/) — audio CD ripping.
- [ffmpeg](https://ffmpeg.org/) — audio extraction for the identifier.

**Episode identification**

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — local audio transcription (CTranslate2 backend).
- [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) — string matching (`partial_ratio`).
- [The Movie Database (TMDB)](https://www.themoviedb.org/) — show / season / episode metadata.
- [OMDb](https://www.omdbapi.com/) — fallback metadata for ARM's identification step.

**Transcript sources** (public, scraped per their ToS — see disclaimer above)

- [subslikescript.com](https://subslikescript.com)
- [Springfield! Springfield!](https://www.springfieldspringfield.co.uk)
- [Fandom](https://www.fandom.com/) — per-show wikis, accessed via the public MediaWiki API.

**Tooling**

- [uv](https://github.com/astral-sh/uv) — Python script runner with PEP 723 inline metadata.
- [ruff](https://github.com/astral-sh/ruff), [pytest](https://pytest.org/), [shellcheck](https://www.shellcheck.net/) — lint and tests.

> This product uses the TMDB API but is not endorsed or certified by TMDB.

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "tqdm>=4.66",
#   "rapidfuzz>=3.6",
#   "faster-whisper>=1.0.3",
# ]
# ///
"""Dialogue-based episode identifier.

Reads a JSON job from stdin, runs ffmpeg + faster-whisper + rapidfuzz,
prints a JSON result to stdout. Same contract as identify_episode.py.

Requires that scripts/fetch_transcripts.py has already populated the cache
for the given tmdb_id. Errors out (exit 3) if the cache is missing.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tqdm import tqdm

# Put scripts/ on sys.path so the sibling ``transcripts`` package resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Submodule-direct imports (per Task 11b note: no package-level re-export). This
# keeps ``transcripts.sources`` (and therefore requests + bs4) out of this CLI's
# import graph, so the PEP 723 deps above stay minimal.
from transcripts.cache import Cache, Episode, normalize_text  # noqa: E402
from transcripts.matching import assign_episodes, score_track  # noqa: E402

logger = logging.getLogger("dialogue_identify")

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "media" / ".subs-cache"

# Audio window: skip cold open + theme; sit in act 1 of a 22- or 42-min episode.
AUDIO_START_SEC = 180
AUDIO_DUR_SEC = 180
# Below this size we treat the WAV as effectively empty and retry from start=0.
# Three seconds of 16kHz mono PCM is ~96 KB; pick a generous floor.
MIN_USABLE_WAV_BYTES = 100_000

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")


def extract_audio(mkv_path: Path, out_wav: Path) -> None:
    """Extract a 3-min mono 16kHz PCM window for ASR. Skips cold-open + theme.

    Falls back to start=0 if the post-extract WAV is empty/tiny — the track
    may be a short special, an extra, or have an intro longer than the
    default ``AUDIO_START_SEC``. ``MIN_USABLE_WAV_BYTES`` is sized so a few
    seconds of 16 kHz mono PCM clears it (44-byte WAV header + samples).
    """

    def run(start_sec: int) -> None:
        cmd = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(start_sec),
            "-t",
            str(AUDIO_DUR_SEC),
            "-i",
            str(mkv_path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ]
        subprocess.run(cmd, check=True)

    run(AUDIO_START_SEC)
    if out_wav.stat().st_size < MIN_USABLE_WAV_BYTES:
        logger.info(
            "track shorter than %ds; retrying from start=0 for %s", AUDIO_START_SEC, mkv_path.name
        )
        run(0)


def transcribe(wav_path: Path, model) -> str:
    segments, _ = model.transcribe(str(wav_path), beam_size=1, vad_filter=True)
    return " ".join(seg.text for seg in segments)


def _configure_logging() -> None:
    """Stderr-only logger; identify.sh captures stdout for the JSON result.

    Third-party loggers (httpx, huggingface_hub, faster_whisper) are pinned to
    WARNING so their per-request chatter doesn't interleave with tqdm progress
    bars — tqdm uses \\r on stderr to redraw, and any other line breaks it.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    for noisy in ("httpx", "httpcore", "huggingface_hub", "faster_whisper", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    _configure_logging()
    try:
        job = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        logger.error("bad stdin JSON: %s", e)
        return 2

    tmdb_id = job.get("tmdb_id")
    show = job.get("show")
    tracks = job.get("tracks", [])
    if not tmdb_id or not tracks:
        logger.error("missing tmdb_id or tracks in job: tmdb_id=%r tracks=%d", tmdb_id, len(tracks))
        return 2

    cache = Cache(CACHE_DIR)
    meta = cache.read_show_meta(tmdb_id)
    if meta is None:
        logger.error(
            "no transcripts cached for tmdb_id=%s. Run: "
            "./scripts/fetch_transcripts.py --tmdb-id %s",
            tmdb_id,
            tmdb_id,
        )
        return 3

    corpus: list[tuple[Episode, str]] = list(cache.iter_all_episodes(tmdb_id))
    if not corpus:
        logger.error(
            "cache for %s (%s) has no episode texts. Re-run fetch_transcripts.py.", show, tmdb_id
        )
        return 3
    logger.info("scoring against %d cached episodes", len(corpus))

    # Lazy import — faster_whisper imports torch and probes CUDA (~2s). All
    # the fast-fail validation above must run first; otherwise a missing-cache
    # error pays the import cost. Per .claude/rules/no-inline-imports.md the
    # justification stays attached to the import.
    from faster_whisper import WhisperModel

    logger.info(
        "loading whisper model=%s device=%s compute=%s",
        WHISPER_MODEL,
        WHISPER_DEVICE,
        WHISPER_COMPUTE,
    )
    model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)

    track_transcripts: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="dialogue-id-") as tmp:
        for tr in tqdm(tracks, unit="track", desc="Transcribing", file=sys.stderr):
            name = tr["name"]
            path = Path(tr["path"])
            wav = Path(tmp) / f"{path.stem}.wav"
            try:
                extract_audio(path, wav)
            except subprocess.CalledProcessError as e:
                # Per no-buried-errors: log with structured context, then skip
                # this track. The track shows up as "no candidate scored" in
                # the output JSON, which surfaces in identify.sh's quarantine.
                logger.warning("ffmpeg failed for track=%s path=%s: %s", name, path, e)
                continue
            text = transcribe(wav, model)
            track_transcripts[name] = normalize_text(text)

    # Build the (track, episode) → score table. With ~200 cached episodes and
    # ~8 tracks per disc the inner work is ~1500 fuzzy-matches; show a bar so
    # the user doesn't think the run hung between transcribing and JSON emit.
    # Postfix tracks the running best match per track for live visibility.
    episodes = [ep for ep, _ in corpus]
    ep_text = {ep: text for ep, text in corpus}
    scores: dict[tuple[str, Episode], float] = {}
    for name, t_text in tqdm(
        track_transcripts.items(),
        total=len(track_transcripts),
        unit="track",
        desc="Scoring",
        file=sys.stderr,
        position=0,
    ):
        best_score = -1.0
        best_ep: Episode | None = None
        inner = tqdm(
            episodes,
            unit="ep",
            desc=f"  {name}",
            file=sys.stderr,
            position=1,
            leave=False,
        )
        for ep in inner:
            s = score_track(t_text, ep_text[ep])
            scores[(name, ep)] = s
            if s > best_score:
                best_score = s
                best_ep = ep
                inner.set_postfix_str(
                    f"best=S{ep.season:02d}E{ep.number:02d} {s:.0f}", refresh=False
                )
        inner.close()
        if best_ep is not None:
            tqdm.write(
                f"  {name}: top S{best_ep.season:02d}E{best_ep.number:02d} "
                f"'{best_ep.title}' score={best_score:.0f}",
                file=sys.stderr,
            )

    track_order = [tr["name"] for tr in tracks if tr["name"] in track_transcripts]
    assignments = assign_episodes(track_order, episodes, scores)

    # Emit the same JSON shape identify_episode.py produced
    out = {"tracks": {}}
    for name in (tr["name"] for tr in tracks):
        a = assignments.get(name)
        if a is None:
            out["tracks"][name] = {
                "season": None,
                "episode": None,
                "title": None,
                "confidence": "low",
                "reasoning": "transcription failed; no candidate scored",
            }
            continue
        out["tracks"][name] = {
            "season": a.episode.season,
            "episode": a.episode.number,
            "title": a.episode.title,
            "confidence": a.confidence,
            "reasoning": f"score={a.score:.0f} margin={a.margin:.0f}",
        }

    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

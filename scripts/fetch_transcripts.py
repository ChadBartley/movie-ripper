#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "typer>=0.12",
#   "tqdm>=4.66",
#   "requests>=2.31",
#   "beautifulsoup4>=4.12",
#   "rapidfuzz>=3.6",
# ]
# ///
"""Pre-warm the transcript cache for a TV show.

Usage:
    fetch_transcripts.py "The Office"                       # by name
    fetch_transcripts.py "The Office" --year 2005           # disambiguate
    fetch_transcripts.py --tmdb-id 2316                     # by TMDB id
    fetch_transcripts.py https://subslikescript.com/...     # seed URL
    fetch_transcripts.py --tmdb-id 1622 --seasons 1,2       # subset of seasons
    fetch_transcripts.py --tmdb-id 1622 --seasons 1-2,5,8-9 # ranges + lists
    fetch_transcripts.py "SpongeBob SquarePants" \
        --sources fandom,subslikescript                     # custom ladder

The ``--sources`` flag is an ordered allowlist. Names omitted from it are
skipped entirely; the order defines fallback precedence. Default is
``subslikescript,springfield,fandom`` — Fandom is last because
auto-discovery costs an extra HTTP round trip.

Reads TMDB_API_KEY from .env.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
from pathlib import Path

import typer
from tqdm import tqdm

# Put scripts/ on sys.path so the sibling ``transcripts`` package resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Submodule-direct imports (per Task 11b note: no package-level re-export).
from transcripts.cache import Cache, Episode, normalize_text  # noqa: E402
from transcripts.sources import (  # noqa: E402
    DEFAULT_SOURCES,
    SOURCE_REGISTRY,
    ShowRef,
    SpringfieldSource,
    SubsLikeScriptSource,
    fetch_episode_with_fallback,
)
from transcripts.tmdb import resolve_show_via_tmdb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "media" / ".subs-cache"

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _parse_seasons_spec(spec: str) -> set[int]:
    """Parse a `--seasons` value like ``"1,2"`` or ``"1-2,5,8-9"`` into a set.

    Whitespace tolerated. Raises ``typer.BadParameter`` on malformed input or
    non-positive season numbers — better to fail loudly than silently fetch
    nothing.
    """
    seasons: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                raise typer.BadParameter(f"invalid season range: {token!r}") from None
            if lo < 1 or hi < lo:
                raise typer.BadParameter(f"invalid season range: {token!r}")
            seasons.update(range(lo, hi + 1))
        else:
            try:
                n = int(token)
            except ValueError:
                raise typer.BadParameter(f"invalid season: {token!r}") from None
            if n < 1:
                raise typer.BadParameter(f"season must be >= 1: {token!r}")
            seasons.add(n)
    if not seasons:
        raise typer.BadParameter("no seasons parsed from --seasons")
    return seasons


def _detect_seed_url_source(url: str) -> tuple[ShowRef, object] | None:
    """Map a pasted URL to (ShowRef, source instance)."""
    parsed = urllib.parse.urlparse(url)
    if "subslikescript.com" in parsed.netloc:
        return ShowRef("subslikescript", url), SubsLikeScriptSource()
    if "springfieldspringfield.co.uk" in parsed.netloc:
        return ShowRef("springfield", url), SpringfieldSource()
    return None


def parse_sources_arg(spec: str | None) -> list[str]:
    """Parse the ``--sources`` value into an ordered list of source names.

    ``None`` or empty returns the default ladder. Whitespace is tolerated.
    Order is preserved as precedence; duplicates are removed (first wins).
    Unknown names raise ``typer.BadParameter`` so a typo doesn't silently
    produce an empty ladder.
    """
    if spec is None or not spec.strip():
        return list(DEFAULT_SOURCES)
    names: list[str] = []
    seen: set[str] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        names.append(token)
    if not names:
        raise typer.BadParameter("no source names parsed from --sources")
    unknown = [n for n in names if n not in SOURCE_REGISTRY]
    if unknown:
        known = ", ".join(sorted(SOURCE_REGISTRY))
        raise typer.BadParameter(f"unknown source name(s): {', '.join(unknown)}. Known: {known}")
    return names


@app.command()
def main(
    target: str = typer.Argument(
        None,
        help="Show name OR seed URL. Omit when using --tmdb-id.",
    ),
    tmdb_id: int = typer.Option(None, "--tmdb-id", help="TMDB series id."),
    year: int = typer.Option(None, "--year", help="Disambiguate by air year."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-fetch episodes already in cache."),
    seasons: str = typer.Option(
        None,
        "--seasons",
        help="Comma-separated seasons or ranges to fetch, e.g. '1,2' or '1-2,5'. Default: all.",
    ),
    sources_spec: str = typer.Option(
        None,
        "--sources",
        help=(
            "Comma-separated, ordered allowlist of sources to use. "
            "Order = fallback precedence; omitted names are skipped. "
            f"Default: {','.join(DEFAULT_SOURCES)}."
        ),
    ),
):
    _load_dotenv()
    tmdb_api_key = os.environ.get("TMDB_API_KEY")
    if not tmdb_api_key:
        typer.echo("ERROR: TMDB_API_KEY not set in .env", err=True)
        raise typer.Exit(1)

    cache = Cache(CACHE_DIR)

    # Parse --sources up-front so a typo fails before any HTTP work.
    source_names = parse_sources_arg(sources_spec)

    # Decide input mode
    seed: tuple[ShowRef, object] | None = None
    show_name: str | None = None
    if target and target.startswith(("http://", "https://")):
        seed = _detect_seed_url_source(target)
        if not seed:
            typer.echo(f"ERROR: unrecognized seed URL host: {target}", err=True)
            raise typer.Exit(1)
        seed_source_name = seed[1].name
        if seed_source_name not in source_names:
            typer.echo(
                f"ERROR: seed URL points to '{seed_source_name}' but --sources excludes it. "
                f"Active sources: {','.join(source_names)}",
                err=True,
            )
            raise typer.Exit(1)
    elif target:
        show_name = target
    elif tmdb_id is None:
        typer.echo("ERROR: must pass a show name, seed URL, or --tmdb-id", err=True)
        raise typer.Exit(1)

    # Resolve TMDB metadata (canonical episode list)
    typer.echo("Resolving show via TMDB...")
    tmdb_id, show_name, year_resolved, episodes = resolve_show_via_tmdb(
        tmdb_api_key,
        show_name,
        year,
        tmdb_id,
    )
    typer.echo(f"  → {show_name} ({year_resolved}) [tmdb-{tmdb_id}], {len(episodes)} episodes")
    cache.write_show_meta(tmdb_id, show_name, year_resolved, episodes)

    if seasons is not None:
        wanted = _parse_seasons_spec(seasons)
        available = {ep.season for ep in episodes}
        unknown = wanted - available
        if unknown:
            typer.echo(
                f"ERROR: --seasons references seasons not in TMDB: {sorted(unknown)} "
                f"(available: {sorted(available)})",
                err=True,
            )
            raise typer.Exit(1)
        episodes = [ep for ep in episodes if ep.season in wanted]
        typer.echo(f"  filtered to seasons {sorted(wanted)}: {len(episodes)} episodes")

    # Build source ladder from --sources. Order is precedence; absent names skipped.
    # Seed URL primes the corresponding source's ShowRef cache without changing
    # the order — ladder shape is the user's call via --sources.
    sources = [SOURCE_REGISTRY[name]() for name in source_names]
    typer.echo(f"  sources: {','.join(source_names)}")
    show_ref_cache: dict[str, ShowRef | None] = {}
    if seed:
        seed_ref, seed_src = seed
        # Replace the registry-built instance with the seed-URL-bound one so
        # both share state (relevant for sources that cache an index per show).
        sources = [seed_src if s.name == seed_src.name else s for s in sources]
        show_ref_cache[seed_src.name] = seed_ref

    # Fetch each episode through the ladder
    fetched_by_source: dict[str, int] = {}
    missing: list[Episode] = []
    pbar = tqdm(episodes, unit="ep", desc="Fetching")
    for ep in pbar:
        pbar.set_postfix_str(f"S{ep.season:02d}E{ep.number:02d} {ep.title[:30]}")
        if not refresh and cache.has_episode(tmdb_id, ep.season, ep.number, ep.title):
            fetched_by_source["cache"] = fetched_by_source.get("cache", 0) + 1
            continue
        text, src_name = fetch_episode_with_fallback(
            sources,
            show_name,
            year_resolved,
            tmdb_id,
            ep.season,
            ep.number,
            ep.title,
            expected_episodes=episodes,
            show_ref_cache=show_ref_cache,
        )
        if text is None:
            missing.append(ep)
            continue
        cache.write_episode(tmdb_id, ep.season, ep.number, ep.title, normalize_text(text))
        fetched_by_source[src_name] = fetched_by_source.get(src_name, 0) + 1

    # Report
    typer.echo("")
    typer.echo("Coverage report:")
    total = len(episodes)
    got = total - len(missing)
    typer.echo(f"  {got}/{total} episodes cached")
    for src, n in sorted(fetched_by_source.items()):
        typer.echo(f"    {src}: {n}")
    if missing:
        typer.echo(f"  Missing ({len(missing)}):")
        for ep in missing:
            typer.echo(f"    S{ep.season:02d}E{ep.number:02d} {ep.title}")
        typer.echo("")
        typer.echo("  Drop manual transcripts at:")
        typer.echo(f"    {CACHE_DIR}/{tmdb_id}/sNN/eNN-Title.txt")
        raise typer.Exit(2)
    typer.echo("Done.")


if __name__ == "__main__":
    app()

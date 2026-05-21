"""Score ASR transcripts against cached episode text and solve the
track→episode assignment.

score_track is a thin rapidfuzz boundary; assign_episodes (Task 11) is
the real logic — greedy with collision-by-score and a contiguity bonus
on disc-adjacent tracks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz

from transcripts.cache import Episode

logger = logging.getLogger(__name__)

Confidence = Literal["high", "medium", "low"]


def score_track(transcript: str, episode_text: str) -> float:
    """Score how well an ASR transcript matches an episode's dialogue.

    Both inputs must already be normalized (see ``normalize_text``).
    Returns 0..100 — higher is a better match. Returns 0.0 on empty
    input rather than asking rapidfuzz to handle the corner case.
    """
    if not transcript or not episode_text:
        return 0.0
    return float(fuzz.partial_ratio(transcript, episode_text))


@dataclass(frozen=True)
class Assignment:
    """Result of assigning one disc track to an episode.

    ``confidence`` is the typed driver for the apply-vs-quarantine decision
    in identify.sh. The string form serializes 1:1 to the existing JSON
    contract that quarantine/apply already consume.
    """

    episode: Episode
    score: float
    margin: float  # gap to runner-up score for this track
    confidence: Confidence


CONFIDENCE_HIGH_SCORE = 70.0
CONFIDENCE_HIGH_MARGIN = 15.0
CONFIDENCE_MEDIUM_SCORE = 60.0
CONFIDENCE_MEDIUM_MARGIN = 8.0
CONTIGUITY_BONUS = 5.0


def _classify_confidence(score: float, margin: float) -> Confidence:
    if score >= CONFIDENCE_HIGH_SCORE and margin >= CONFIDENCE_HIGH_MARGIN:
        return "high"
    if score >= CONFIDENCE_MEDIUM_SCORE and margin >= CONFIDENCE_MEDIUM_MARGIN:
        return "medium"
    return "low"


def _contiguity_bonus(track_to_ep: dict[str, Episode], track_order: list[str]) -> float:
    bonus = 0.0
    for a, b in zip(track_order, track_order[1:], strict=False):
        ea = track_to_ep.get(a)
        eb = track_to_ep.get(b)
        if not ea or not eb:
            continue
        if ea.season == eb.season and eb.number == ea.number + 1:
            bonus += CONTIGUITY_BONUS
    return bonus


def assign_episodes(
    tracks: list[str], episodes: list[Episode], scores: dict[tuple[str, Episode], float]
) -> dict[str, Assignment]:
    """Assign each track to an episode greedily with contiguity tie-break.

    Tracks should be passed in their physical order on the disc (e.g. C01,
    C02, ...) so the contiguity bonus can fire between adjacent tracks.
    """
    ranked: dict[str, list[tuple[Episode, float]]] = {
        t: sorted(
            ((e, scores.get((t, e), 0.0)) for e in episodes), key=lambda pair: pair[1], reverse=True
        )
        for t in tracks
    }

    # Phase 1: greedy by score, break collisions by score; loser falls to next.
    used: dict[Episode, str] = {}
    track_to_ep: dict[str, Episode] = {}
    queue = list(tracks)
    cursor: dict[str, int] = {t: 0 for t in tracks}
    while queue:
        progressed = False
        next_queue: list[str] = []
        for t in queue:
            i = cursor[t]
            if i >= len(ranked[t]):
                continue
            ep, sc = ranked[t][i]
            holder = used.get(ep)
            if holder is None:
                used[ep] = t
                track_to_ep[t] = ep
                progressed = True
            else:
                holder_sc = next(s for e, s in ranked[holder] if e == ep)
                if sc > holder_sc:
                    cursor[holder] += 1
                    track_to_ep.pop(holder, None)
                    used[ep] = t
                    track_to_ep[t] = ep
                    next_queue.append(holder)
                    progressed = True
                else:
                    cursor[t] += 1
                    next_queue.append(t)
        queue = next_queue
        if not progressed:
            break

    # Phase 2: contiguity refinement.
    base_total = sum(
        scores.get((t, track_to_ep[t]), 0.0) for t in tracks if t in track_to_ep
    ) + _contiguity_bonus(track_to_ep, tracks)
    improved = True
    while improved:
        improved = False
        for t in tracks:
            current = track_to_ep.get(t)
            if not current:
                continue
            for ep, _sc in ranked[t]:
                if ep == current:
                    continue
                holder = used.get(ep)
                if holder is None:
                    new = dict(track_to_ep)
                    new[t] = ep
                    new_total = sum(
                        scores.get((tt, new[tt]), 0.0) for tt in tracks if tt in new
                    ) + _contiguity_bonus(new, tracks)
                    if new_total > base_total + 0.001:
                        used.pop(current, None)
                        used[ep] = t
                        track_to_ep[t] = ep
                        base_total = new_total
                        improved = True
                        break
                elif holder != t:
                    new = dict(track_to_ep)
                    new[t] = ep
                    new[holder] = current
                    new_total = sum(
                        scores.get((tt, new[tt]), 0.0) for tt in tracks if tt in new
                    ) + _contiguity_bonus(new, tracks)
                    if new_total > base_total + 0.001:
                        used[ep] = t
                        used[current] = holder
                        track_to_ep[t] = ep
                        track_to_ep[holder] = current
                        base_total = new_total
                        improved = True
                        break

    out: dict[str, Assignment] = {}
    for t in tracks:
        ep = track_to_ep.get(t)
        if not ep:
            continue
        sc = scores.get((t, ep), 0.0)
        runner_up = max((s for e, s in ranked[t] if e != ep), default=0.0)
        out[t] = Assignment(
            episode=ep,
            score=sc,
            margin=sc - runner_up,
            confidence=_classify_confidence(sc, sc - runner_up),
        )
    return out

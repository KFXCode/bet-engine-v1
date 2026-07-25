"""
data/lineups.py
================
Confirmed starting lineups from the MLB Stats API boxscore endpoint.

Unlike data/rosters.py (the full 26-man active roster), this returns ONLY
the 9 batters actually posted in today's batting order for a game -- which
is what the HR prop workflow should use, so it never surfaces a benched or
inactive player (the "Francisco Alvarez wasn't even in the lineup" bug).

Lineups post ~3-4 hours before first pitch. Before that, the boxscore has
no battingOrder set, so get_confirmed_lineup() returns [] and the caller
(run_daily.py) falls back to the active roster with a visible
data_quality="roster" flag on those picks, rather than pretending a
morning guess is a confirmed starter.
"""

import logging

import requests

logger = logging.getLogger(__name__)

BOXSCORE_API = "https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"


def _game_pk(game_id):
    """MLB game_ids are the raw statsapi gamePk (schedule_provider stores it
    as-is). Guard anyway so a differently-shaped id can't crash the run."""
    return str(game_id).strip()


def get_confirmed_lineup(game_id, side):
    """side: 'home' | 'away'. Returns list[str] of batter full names in the
    posted batting order, or [] if the lineup hasn't posted yet / any error."""
    try:
        resp = requests.get(BOXSCORE_API.format(game_pk=_game_pk(game_id)), timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.debug("Lineup fetch failed for game %s (%s): %s", game_id, side, exc)
        return []

    team_block = payload.get("teams", {}).get(side, {})
    players = team_block.get("players", {})

    ordered = []
    for player in players.values():
        batting_order = player.get("battingOrder")  # "100","200"... only set once posted
        if not batting_order:
            continue
        name = player.get("person", {}).get("fullName")
        if name:
            ordered.append((int(batting_order), name))

    ordered.sort(key=lambda t: t[0])
    # battingOrder like 101/102 = starter vs substitute in a slot; the sort
    # keeps the starter (x00) first, and we only want the 9 posted starters.
    seen_slots = set()
    starters = []
    for order, name in ordered:
        slot = order // 100
        if slot in seen_slots:
            continue
        seen_slots.add(slot)
        starters.append(name)
    return starters


def get_confirmed_pitcher(game_id, side):
    """side: 'home' | 'away'. Returns the CONFIRMED starting pitcher's full
    name for that side, or None if not confirmed yet / any error.

    Reads two places in the live boxscore feed, most-authoritative first:
      1. teams.<side>.pitchers[0] -- once the game is underway this is the
         actual pitcher who started, so a late scratch (e.g. an injured
         probable pulled morning-of) is already corrected here.
      2. the game's probablePitchers block -- MLB's own confirmed probable,
         which updates when a starter changes, unlike the schedule feed's
         cached probablePitcher that can go stale.
    Anything this can't confirm returns None so the caller can fall back to
    the schedule probable AND flag the pick as unconfirmed."""
    try:
        resp = requests.get(BOXSCORE_API.format(game_pk=_game_pk(game_id)), timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.debug("Confirmed-pitcher fetch failed for game %s (%s): %s", game_id, side, exc)
        return None

    team_block = payload.get("teams", {}).get(side, {})
    players = team_block.get("players", {})

    # 1. Actual game starter (populated once first pitch is thrown / lineups locked)
    pitcher_ids = team_block.get("pitchers", [])
    if pitcher_ids:
        starter = players.get(f"ID{pitcher_ids[0]}", {})
        name = starter.get("person", {}).get("fullName")
        if name:
            return name

    # 2. MLB's confirmed probable for this specific game
    info = payload.get("info", [])
    probable = team_block.get("probablePitcher") or {}
    name = probable.get("fullName")
    if name:
        return name
    return None


def get_hr_settled_players(game_id):
    """For grading: returns set of batter full names who hit >=1 HR in this
    game, or None if the game isn't final / boxscore unavailable. Reads each
    player's batting.homeRuns from the final boxscore."""
    try:
        resp = requests.get(BOXSCORE_API.format(game_pk=_game_pk(game_id)), timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.debug("HR settlement fetch failed for game %s: %s", game_id, exc)
        return None

    homered = set()
    found_any_stats = False
    for side in ("home", "away"):
        players = payload.get("teams", {}).get(side, {}).get("players", {})
        for player in players.values():
            batting = player.get("stats", {}).get("batting", {})
            if not batting:
                continue
            found_any_stats = True
            if batting.get("homeRuns", 0):
                name = player.get("person", {}).get("fullName")
                if name:
                    homered.add(name)
    if not found_any_stats:
        return None  # game not started / no box score yet -- leave pending
    return homered

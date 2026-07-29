"""
data/schedule_provider.py
==========================
Today's MLB schedule from the MLB Stats API -- free, public, no API key.
This is the one external data source in the whole project you never need to
configure: it's what tells the rest of the pipeline which games exist today.
"""

import logging
import requests

from engine.models import Game, ProbablePitcher
from data.teams import normalize_team

logger = logging.getLogger(__name__)

MLB_STATS_API = "https://statsapi.mlb.com/api/v1/schedule"

# gameType codes: R=regular season, F/D/L/W=postseason rounds -- these are
# real, bettable MLB games. Excluded: A=All-Star Game, S=Spring Training,
# E=Exhibition.
REAL_GAME_TYPES = {"R", "F", "D", "L", "W"}


def get_todays_games(date_str):
    """date_str: 'YYYY-MM-DD'. Returns a list of engine.models.Game.
    Never raises -- on any network/parsing problem it logs and returns []."""
    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "probablePitcher,team,linescore",
    }
    try:
        resp = requests.get(MLB_STATS_API, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch MLB schedule for %s: %s", date_str, exc)
        return []

    games = []
    skipped_non_bettable = 0
    for date_block in payload.get("dates", []):
        for g in date_block.get("games", []):
            if g.get("gameType") not in REAL_GAME_TYPES:
                skipped_non_bettable += 1
                continue
            try:
                games.append(_parse_game(g, date_str))
            except Exception as exc:
                logger.warning("Skipping one game we couldn't parse: %s", exc)
    if skipped_non_bettable:
        logger.info("Excluded %d non-bettable MLB game(s) today (All-Star/Spring Training/Exhibition).",
                    skipped_non_bettable)

    # --- Doubleheader tagging -------------------------------------------
    # When the SAME pairing appears twice today it's a doubleheader. MLB's
    # feed carries gameNumber (1/2); we stamp each Game with it and flag the
    # pair, so every downstream pick/parlay leg can say WHICH game it means
    # (the Cincinnati bug: two CIN games, picks couldn't tell them apart).
    by_pair = {}
    for game in games:
        by_pair.setdefault((game.away_team, game.home_team), []).append(game)
    for pair_games in by_pair.values():
        if len(pair_games) > 1:
            pair_games.sort(key=lambda g: g.game_time_utc or "")
            for i, game in enumerate(pair_games, start=1):
                if game.game_number == 1 and i != 1:
                    game.game_number = i
                game.doubleheader = True

    return games


def _parse_game(g, date_str):
    teams = g["teams"]
    home = teams["home"]["team"]["name"]
    away = teams["away"]["team"]["name"]

    home_pitcher = _parse_pitcher(teams["home"].get("probablePitcher"))
    away_pitcher = _parse_pitcher(teams["away"].get("probablePitcher"))

    return Game(
        game_id=str(g["gamePk"]),
        date=date_str,
        home_team=normalize_team(home),
        away_team=normalize_team(away),
        game_time_utc=g.get("gameDate"),
        home_pitcher=home_pitcher,
        away_pitcher=away_pitcher,
        game_number=g.get("gameNumber", 1),
    )


def _parse_pitcher(p):
    if not p:
        return None
    return ProbablePitcher(name=p.get("fullName", "TBD"), player_id=p.get("id"))

"""
data/standings.py
===================
Team records (wins/losses, run differential, games back, current streak)
from the MLB Stats API standings endpoint. Powers the "talent gap" and
"motivation" grading factors.

ALSO the home of the once-per-season history seed (see seed_history_for_all
below). That lives here because this module is the one records call that runs
UNCONDITIONALLY on every run. The seed was originally triggered from
standings_scores.get_records(), which is only called for sports that have
games TODAY -- so NCAAF, with no games in August, never seeded at all and the
totals model still had nothing to project from. Seeding must not depend on
today's schedule; it has to happen for every sport whose season is in range.
"""

import logging
from datetime import datetime

import requests

from data.situational import TEAM_IDS

logger = logging.getLogger(__name__)

STANDINGS_API = "https://statsapi.mlb.com/api/v1/standings"

# Sports whose prior-season finals we backfill so records/totals have a real
# baseline from day one. MLB is excluded: its records come live from the
# statsapi standings endpoint above, which is complete on its own.
SEED_SPORTS = ["NCAAF", "NFL", "NCAAB", "NBA", "NHL", "WNBA"]


def seed_history_for_all(season=None, sports=None):
    """Backfill prior-season finals for every listed sport that hasn't been
    seeded yet. Idempotent (each sport/season self-marks once done) and safe to
    call every run. Independent of today's schedule by design."""
    season = season or datetime.now().year
    from data.seed_history import ensure_season_seeded  # local import: avoids a cycle

    total = 0
    for sport in (sports or SEED_SPORTS):
        try:
            total += ensure_season_seeded(sport, season)
        except Exception as exc:
            logger.warning("History seed for %s skipped: %s", sport, exc)
    if total:
        logger.info("History seed: %d prior-season game(s) stored across %d sport(s).",
                    total, len(sports or SEED_SPORTS))
    return total


def get_all_team_records(season=None):
    """Returns dict team_abbr -> {wins, losses, runs_scored, runs_allowed,
    games_back, streak, division_rivals}. Never raises; returns {} on
    failure so callers degrade gracefully (talent/motivation factors just
    go neutral for the day).

    Seeds prior-season history for the non-MLB sports first -- this is the
    unconditional hook that guarantees it happens regardless of which leagues
    are playing today."""
    season = season or datetime.now().year

    try:
        seed_history_for_all(season)
    except Exception as exc:
        logger.warning("History seed pass failed (continuing): %s", exc)

    try:
        resp = requests.get(
            STANDINGS_API,
            params={"leagueId": "103,104", "season": season, "standingsTypes": "regularSeason"},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("Standings fetch failed: %s", exc)
        return {}

    id_to_abbr = {v: k for k, v in TEAM_IDS.items()}
    records = {}
    for record_block in payload.get("records", []):
        division_teams = []
        for team_record in record_block.get("teamRecords", []):
            team_id = team_record.get("team", {}).get("id")
            abbr = id_to_abbr.get(team_id)
            if not abbr:
                continue
            division_teams.append(abbr)
            streak_block = team_record.get("streak", {}) or {}
            streak_number = streak_block.get("streakNumber", 0) or 0
            streak = streak_number if streak_block.get("streakType") == "wins" else -streak_number
            records[abbr] = {
                "wins": team_record.get("wins"),
                "losses": team_record.get("losses"),
                "runs_scored": team_record.get("runsScored"),
                "runs_allowed": team_record.get("runsAllowed"),
                "games_back": _parse_games_back(team_record.get("gamesBack")),
                "streak": streak,
            }
        for abbr in division_teams:
            if abbr in records:
                records[abbr]["division_rivals"] = [t for t in division_teams if t != abbr]
    return records


def _parse_games_back(gb):
    if gb in (None, "-", ""):
        return 0.0
    try:
        return float(gb)
    except ValueError:
        return 0.0

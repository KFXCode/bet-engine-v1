"""
data/schedule_provider_ncaaf.py
================================
Today's college football schedule, via the shared multi-host ESPN fetcher.

MULTI-HOST (Aug 27, 2026): this hit ONE ESPN host directly. GitHub Actions'
IPs get blocked there, so on the runner it returned nothing and fell through
to The Odds API -- and with the odds quota exhausted there was no schedule at
all, so the league silently vanished from the report. data/espn_fetch tries
three independent ESPN hosts and costs zero Odds API credits, so schedules
keep working even with no quota.

PLACEHOLDER GUARD (Sep 4, 2026): ESPN publishes future rounds whose opponent
isn't decided yet, sending the team through as "TBD". Those were being turned
into real Game rows, so the engine graded them, priced them, and actually
RECOMMENDED bets on a team named TBD -- picks that can never be settled and
sat pending forever, dragging the NCAAF record with them. Any matchup missing
a real opponent on either side is now dropped before it becomes a Game.
"""

import logging

from engine.models import Game
from data.teams_college import normalize_college_team
from data.espn_fetch import fetch_scoreboard_events

logger = logging.getLogger(__name__)

LEAGUE_PATH = "football/college-football"

# Names ESPN uses when a slot has no decided opponent yet.
PLACEHOLDER_NAMES = {"TBD", "TBA", "TO BE DETERMINED", "TO BE ANNOUNCED", ""}


def _is_placeholder(name):
    return (name or "").strip().upper() in PLACEHOLDER_NAMES


def get_todays_ncaaf_games(date_str):
    """date_str: 'YYYY-MM-DD'. Never raises."""
    events = fetch_scoreboard_events(
        LEAGUE_PATH, date_str,
        season_types=(None, 2, 3),
        referer="https://www.espn.com/college-football/scoreboard",
    )
    if not events:
        logger.info("NCAAF schedule %s: no events from any ESPN host.", date_str)
        return []

    games = []
    skipped_placeholder = 0
    for event in events:
        try:
            parsed, placeholder = _parse_event(event, date_str)
            if placeholder:
                skipped_placeholder += 1
            elif parsed:
                games.append(parsed)
        except Exception as exc:
            logger.warning("Skipping one NCAAF game we couldn't parse: %s", exc)

    if skipped_placeholder:
        logger.info("NCAAF schedule %s: skipped %d game(s) with an undecided (TBD) opponent -- "
                    "those can never be graded, so they're kept off the slate.",
                    date_str, skipped_placeholder)
    logger.info("NCAAF schedule %s: %d game(s).", date_str, len(games))
    return games


def _parse_event(event, date_str):
    """Returns (Game|None, is_placeholder)."""
    competitions = event.get("competitions", [])
    if not competitions:
        return None, False
    competitors = competitions[0].get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None, False

    home_name = (home.get("team", {}).get("displayName")
                 or home.get("team", {}).get("shortDisplayName") or "")
    away_name = (away.get("team", {}).get("displayName")
                 or away.get("team", {}).get("shortDisplayName") or "")

    if _is_placeholder(home_name) or _is_placeholder(away_name):
        return None, True
    if not home_name or not away_name:
        return None, False

    home_norm = normalize_college_team(home_name)
    away_norm = normalize_college_team(away_name)
    # Guard again post-normalization, in case the mapper itself yields a
    # placeholder for an unrecognized name.
    if _is_placeholder(home_norm) or _is_placeholder(away_norm):
        return None, True

    return Game(
        game_id=f"ncaaf-{event['id']}",
        date=date_str,
        home_team=home_norm,
        away_team=away_norm,
        game_time_utc=event.get("date"),
        sport="NCAAF",
    ), False

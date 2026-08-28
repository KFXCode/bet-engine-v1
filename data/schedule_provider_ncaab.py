"""
data/schedule_provider_ncaab.py
================================
Today's men's college basketball schedule, via the shared multi-host ESPN
fetcher.

WHY (Aug 27, 2026): this hit ONE ESPN host directly. GitHub Actions' IPs get
blocked there, so on the runner it returned nothing and fell through to The
Odds API -- and with the odds quota exhausted there was no schedule at all, so
the league silently vanished from the report. data/espn_fetch tries three
independent ESPN hosts and costs zero Odds API credits, so schedules keep
working even with no quota.

Off-season this returns [] so NCAAB stays dormant until November with no code
change needed.
"""

import logging

from engine.models import Game
from data.teams_college import normalize_college_team
from data.espn_fetch import fetch_scoreboard_events

logger = logging.getLogger(__name__)

LEAGUE_PATH = "basketball/mens-college-basketball"


def get_todays_ncaab_games(date_str):
    """date_str: 'YYYY-MM-DD'. Never raises."""
    events = fetch_scoreboard_events(
        LEAGUE_PATH, date_str,
        season_types=(None, 2, 3),
        referer="https://www.espn.com/mens-college-basketball/scoreboard",
    )
    if not events:
        logger.info("NCAAB schedule %s: no events from any ESPN host.", date_str)
        return []

    games = []
    for event in events:
        try:
            parsed = _parse_event(event, date_str)
            if parsed:
                games.append(parsed)
        except Exception as exc:
            logger.warning("Skipping one NCAAB game we couldn't parse: %s", exc)

    logger.info("NCAAB schedule %s: %d game(s).", date_str, len(games))
    return games


def _parse_event(event, date_str):
    competitions = event.get("competitions", [])
    if not competitions:
        return None
    competitors = competitions[0].get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None

    home_name = (home.get("team", {}).get("displayName")
                 or home.get("team", {}).get("shortDisplayName") or "")
    away_name = (away.get("team", {}).get("displayName")
                 or away.get("team", {}).get("shortDisplayName") or "")
    if not home_name or not away_name:
        return None

    return Game(
        game_id=f"ncaab-{event['id']}",
        date=date_str,
        home_team=normalize_college_team(home_name),
        away_team=normalize_college_team(away_name),
        game_time_utc=event.get("date"),
        sport="NCAAB",
    )

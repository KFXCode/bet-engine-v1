"""
data/schedule_provider_ncaaf.py
================================
Today's college football schedule, via the shared multi-host ESPN fetcher.

WHY (Aug 27, 2026): this hit ONE ESPN host directly. GitHub Actions' IPs get
blocked there, so on the runner it returned nothing and fell through to The
Odds API -- and with the odds quota exhausted there was no schedule at all, so
the league silently vanished from the report. Exactly what hid a 4-game NFL
night. data/espn_fetch tries three independent ESPN hosts and costs zero Odds
API credits, so schedules keep working even with no quota.

Season types are swept (1 = preseason/early, 2 = regular, 3 = postseason/bowls)
so bowl season and Week 0 both show up.
"""

import logging

from engine.models import Game
from data.teams_college import normalize_college_team
from data.espn_fetch import fetch_scoreboard_events

logger = logging.getLogger(__name__)

LEAGUE_PATH = "football/college-football"


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
    for event in events:
        try:
            parsed = _parse_event(event, date_str)
            if parsed:
                games.append(parsed)
        except Exception as exc:
            logger.warning("Skipping one NCAAF game we couldn't parse: %s", exc)

    logger.info("NCAAF schedule %s: %d game(s).", date_str, len(games))
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
        game_id=f"ncaaf-{event['id']}",
        date=date_str,
        home_team=normalize_college_team(home_name),
        away_team=normalize_college_team(away_name),
        game_time_utc=event.get("date"),
        sport="NCAAF",
    )

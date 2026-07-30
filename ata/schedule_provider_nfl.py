"""
data/schedule_provider_nfl.py
==============================
Today's NFL schedule from ESPN's public scoreboard endpoint -- free, no key,
same "never raises" contract as the MLB/WNBA providers: any network/parse
problem logs and returns [] so the daily run still completes.

Off-season this simply returns [] (no games), so the sport stays dormant on
the report until the schedule opens -- no code change needed when the season
starts. Regular season is Weeks 1-18 (Sept-Jan) plus playoffs; preseason
(seasontype=1) is included so August tune-up games show too.
"""

import logging
import requests

from engine.models import Game
from data.teams_nfl import normalize_nfl_team

logger = logging.getLogger(__name__)

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


def get_todays_nfl_games(date_str):
    """date_str: 'YYYY-MM-DD'."""
    try:
        resp = requests.get(ESPN_SCOREBOARD, params={"dates": date_str.replace("-", "")}, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch NFL schedule for %s: %s", date_str, exc)
        return []

    games = []
    for event in payload.get("events", []):
        try:
            parsed = _parse_event(event, date_str)
            if parsed:
                games.append(parsed)
        except Exception as exc:
            logger.warning("Skipping one NFL game we couldn't parse: %s", exc)
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

    return Game(
        game_id=f"nfl-{event['id']}",
        date=date_str,
        home_team=normalize_nfl_team(home.get("team", {}).get("displayName", "")),
        away_team=normalize_nfl_team(away.get("team", {}).get("displayName", "")),
        game_time_utc=event.get("date"),
        sport="NFL",
    )

"""
data/schedule_provider_ncaaf.py
================================
Today's NCAA Football schedule from ESPN's public scoreboard endpoint --
free, no key, same "never raises" contract as the other providers. Off-season
returns [] so the sport stays dormant until the schedule opens.

groups=80 is ESPN's code for all FBS games. Saturday slates can be 40-60
games; the daily 5-pick cap in the strategy engine keeps output selective.
"""

import logging
import requests

from engine.models import Game
from data.teams_college import normalize_college_team

logger = logging.getLogger(__name__)

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"


def get_todays_ncaaf_games(date_str):
    """date_str: 'YYYY-MM-DD'."""
    try:
        resp = requests.get(ESPN_SCOREBOARD,
                            params={"dates": date_str.replace("-", ""), "groups": "80", "limit": "200"},
                            timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch NCAAF schedule for %s: %s", date_str, exc)
        return []

    games = []
    for event in payload.get("events", []):
        try:
            parsed = _parse_event(event, date_str)
            if parsed:
                games.append(parsed)
        except Exception as exc:
            logger.warning("Skipping one NCAAF game we couldn't parse: %s", exc)
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
        game_id=f"ncaaf-{event['id']}",
        date=date_str,
        home_team=normalize_college_team(home.get("team", {}).get("displayName", "")),
        away_team=normalize_college_team(away.get("team", {}).get("displayName", "")),
        game_time_utc=event.get("date"),
        sport="NCAAF",
    )

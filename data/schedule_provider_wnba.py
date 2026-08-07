"""
data/schedule_provider_wnba.py
================================
Today's WNBA schedule from ESPN's public scoreboard endpoint -- free, no key,
same "never raises" contract as data/schedule_provider.py (MLB).

IMPORTANT: ESPN's site.api endpoints reject requests that don't look like a
browser -- a default python-requests User-Agent often gets an empty/blocked
response from datacenter IPs (like GitHub Actions), which is why WNBA games
silently never showed up. We send a browser User-Agent (BROWSER_HEADERS) so
the runner gets the same data your phone does. Every ESPN-based provider
(NFL/NCAAF/NCAAB/NHL/NBA) must do the same.

WNBA games have no probable-pitcher equivalent, so Game.home_pitcher/
away_pitcher stay None -- pitching-dependent factors degrade to neutral.
"""

import logging

import requests

from engine.models import Game
from data.teams_wnba import normalize_wnba_team

logger = logging.getLogger(__name__)

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.espn.com/wnba/scoreboard",
}


def get_todays_wnba_games(date_str):
    """date_str: 'YYYY-MM-DD'."""
    try:
        resp = requests.get(ESPN_SCOREBOARD, params={"dates": date_str.replace("-", "")},
                            headers=BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch WNBA schedule for %s: %s", date_str, exc)
        return []

    events = payload.get("events", [])
    logger.info("WNBA schedule: ESPN returned %d event(s) for %s.", len(events), date_str)
    games = []
    for event in events:
        try:
            parsed = _parse_event(event, date_str)
            if parsed:
                games.append(parsed)
        except Exception as exc:
            logger.warning("Skipping one WNBA game we couldn't parse: %s", exc)
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
        game_id=f"wnba-{event['id']}",
        date=date_str,
        home_team=normalize_wnba_team(home.get("team", {}).get("displayName", "")),
        away_team=normalize_wnba_team(away.get("team", {}).get("displayName", "")),
        game_time_utc=event.get("date"),
        sport="WNBA",
    )

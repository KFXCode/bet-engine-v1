"""
data/schedule_provider_nfl.py
==============================
Today's NFL schedule, via the shared multi-host ESPN fetcher.

WHY THIS CHANGED (Aug 27, 2026): this provider was still hitting ONE ESPN host
directly with plain requests. GitHub Actions' datacenter IPs get blocked on
that host, so on the runner it returned nothing and the code fell through to
The Odds API -- and with the odds quota exhausted there was no schedule at
all, so NFL games silently vanished from the report even on a 4-game night.

data/espn_fetch.fetch_scoreboard_events tries three independent ESPN hosts
(site.api, site.web.api, and the CDN core endpoint) and returns the first that
answers, so a block on one can't hide the slate. It costs zero Odds API
credits, which also keeps schedules working when the quota is gone.

It also sweeps season types: NFL PRESEASON lives under seasontype=1 while the
regular season is 2 and playoffs are 3. Querying only the default missed
preseason games entirely.

Off-season this returns [] (no games), so NFL stays dormant on the report with
no code change needed when the season opens.
"""

import logging

from engine.models import Game
from data.teams_nfl import normalize_nfl_team
from data.espn_fetch import fetch_scoreboard_events

logger = logging.getLogger(__name__)

LEAGUE_PATH = "football/nfl"


def get_todays_nfl_games(date_str):
    """date_str: 'YYYY-MM-DD'. Never raises."""
    events = fetch_scoreboard_events(
        LEAGUE_PATH, date_str,
        season_types=(None, 1, 2, 3),   # default, preseason, regular, postseason
        referer="https://www.espn.com/nfl/scoreboard",
    )
    if not events:
        logger.info("NFL schedule %s: no events from any ESPN host.", date_str)
        return []

    games = []
    for event in events:
        try:
            parsed = _parse_event(event, date_str)
            if parsed:
                games.append(parsed)
        except Exception as exc:
            logger.warning("Skipping one NFL game we couldn't parse: %s", exc)

    logger.info("NFL schedule %s: %d game(s).", date_str, len(games))
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
        game_id=f"nfl-{event['id']}",
        date=date_str,
        home_team=normalize_nfl_team(home_name),
        away_team=normalize_nfl_team(away_name),
        game_time_utc=event.get("date"),
        sport="NFL",
    )

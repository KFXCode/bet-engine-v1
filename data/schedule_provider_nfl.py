"""
data/schedule_provider_nfl.py
==============================
Today's NFL schedule, via the shared multi-host ESPN fetcher.

TWO THINGS THIS FILE OWNS:

1. RESILIENT FETCH. This used to hit ONE ESPN host with plain requests.
   GitHub Actions' datacenter IPs get blocked there, so on the runner it
   returned nothing and fell through to The Odds API -- and with the odds
   quota exhausted there was no schedule at all, so NFL silently vanished from
   the report even on a 4-game night. data/espn_fetch tries three independent
   ESPN hosts and costs zero Odds API credits.

2. PRESEASON DETECTION. Each game is tagged is_preseason from ESPN's
   seasontype (1 = preseason). engine/td_props.py uses that flag to skip TD
   props for those games: FanDuel posts no anytime-TD market in preseason, and
   season TD rates can't predict who scores when starters play two series.
   Moneyline still runs normally on preseason games -- only player props are
   suppressed.

ESPN reports seasontype in two places depending on host/shape, so both are
checked: the event's own `season.type`, and the query we asked for.
"""

import logging

from engine.models import Game
from data.teams_nfl import normalize_nfl_team
from data.espn_fetch import fetch_scoreboard_events

logger = logging.getLogger(__name__)

LEAGUE_PATH = "football/nfl"
PRESEASON_TYPE = 1


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

    pre = sum(1 for g in games if g.is_preseason)
    logger.info("NFL schedule %s: %d game(s)%s.", date_str, len(games),
                f" ({pre} preseason -- TD props suppressed for those)" if pre else "")
    return games


def _is_preseason(event):
    season = event.get("season") or {}
    try:
        if int(season.get("type")) == PRESEASON_TYPE:
            return True
    except (TypeError, ValueError):
        pass
    # Some host shapes put it on the competition instead.
    for comp in event.get("competitions", []):
        cs = (comp.get("season") or {})
        try:
            if int(cs.get("type")) == PRESEASON_TYPE:
                return True
        except (TypeError, ValueError):
            continue
    return False


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
        is_preseason=_is_preseason(event),
    )

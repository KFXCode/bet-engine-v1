"""
data/odds_api_schedule.py
==========================
Generic schedule builder from The Odds API, for any sport. This is the
runner-proof fallback for every ESPN-based schedule provider: ESPN blocks
GitHub Actions' datacenter IPs, but The Odds API (the same paid feed that
supplies our odds) works reliably from the runner.

run_daily._fetch_schedule() calls schedule_from_odds_api(sport, date_str)
whenever a sport's ESPN provider returns nothing, so NFL/NCAAF/NCAAB/NHL/NBA
all light up automatically the moment their seasons open -- no per-sport ESPN
dependency. Games are bucketed to the local day (config.TIMEZONE). Never raises.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import config
from engine.models import Game
from data.teams import normalize_team as normalize_mlb_team
from data.teams_wnba import normalize_wnba_team
from data.teams_nfl import normalize_nfl_team
from data.teams_college import normalize_college_team
from data.teams_nhl import normalize_nhl_team
from data.teams_nba import normalize_nba_team

logger = logging.getLogger(__name__)

# The Odds API sport keys (mirrors data/odds_providers.ODDS_API_SPORT_KEYS).
ODDS_API_SPORT_KEYS = {
    "MLB": "baseball_mlb",
    "WNBA": "basketball_wnba",
    "NFL": "americanfootball_nfl",
    "NCAAF": "americanfootball_ncaaf",
    "NCAAB": "basketball_ncaab",
    "NHL": "icehockey_nhl",
    "NBA": "basketball_nba",
}


def _normalizer(sport):
    if sport == "WNBA":
        return normalize_wnba_team
    if sport == "NFL":
        return normalize_nfl_team
    if sport in ("NCAAF", "NCAAB"):
        return normalize_college_team
    if sport == "NHL":
        return normalize_nhl_team
    if sport == "NBA":
        return normalize_nba_team
    return normalize_mlb_team


def schedule_from_odds_api(sport, date_str):
    """Returns list[Game] for `sport` on the local day `date_str`, derived from
    The Odds API h2h feed. [] on any problem (never raises)."""
    sport_key = ODDS_API_SPORT_KEYS.get(sport)
    if not sport_key:
        return []
    if not config.ODDS_API_KEY:
        logger.warning("%s schedule fallback: no ODDS_API_KEY -- cannot derive schedule.", sport)
        return []

    url = f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/odds"
    try:
        resp = requests.get(url, params={
            "apiKey": config.ODDS_API_KEY, "regions": "us", "markets": "h2h",
            "oddsFormat": "american",
        }, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except Exception as exc:
        logger.error("%s schedule fallback (Odds API) failed for %s: %s", sport, date_str, exc)
        return []

    try:
        tz = ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = ZoneInfo("America/New_York")

    normalize = _normalizer(sport)
    games = []
    seen = set()
    for ev in events if isinstance(events, list) else []:
        ct = ev.get("commence_time")
        if not ct:
            continue
        try:
            local_day = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(tz).strftime("%Y-%m-%d")
        except Exception:
            continue
        if local_day != date_str:
            continue
        home = normalize(ev.get("home_team", ""))
        away = normalize(ev.get("away_team", ""))
        key = (home, away)
        if key in seen:
            continue
        seen.add(key)
        games.append(Game(
            game_id=f"{sport.lower()}-{ev.get('id')}",
            date=date_str,
            home_team=home,
            away_team=away,
            game_time_utc=ct,
            sport=sport,
        ))
    logger.info("%s schedule (Odds API fallback): %d game(s) for %s.", sport, len(games), date_str)
    return games

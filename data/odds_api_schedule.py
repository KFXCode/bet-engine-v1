"""
data/odds_api_schedule.py
==========================
Generic schedule builder from The Odds API, for any sport. This is the
runner-proof fallback for every ESPN-based schedule provider: ESPN blocks
GitHub Actions' datacenter IPs, but The Odds API (the same paid feed that
supplies our odds) works reliably from the runner.

run_daily._fetch_schedule() calls schedule_from_odds_api(sport, date_str)
whenever a sport's ESPN provider returns nothing. Games are bucketed to the
local day (config.TIMEZONE). Never raises.

PRESEASON FIX (Aug 22, 2026): The Odds API keeps NFL PRESEASON under its own
sport key, `americanfootball_nfl_preseason` -- the regular-season key
`americanfootball_nfl` returns nothing in August. That is why no NFL tab
appeared during preseason even though a dozen games were on. Each sport now
maps to a LIST of candidate keys and we merge every one that returns events,
so preseason and regular season both light up (and the changeover in early
September needs no code change).
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

# One or MORE Odds API sport keys per sport. Order doesn't matter -- results
# from every key that returns events are merged.
ODDS_API_SPORT_KEYS = {
    "MLB": ["baseball_mlb"],
    "WNBA": ["basketball_wnba"],
    "NFL": ["americanfootball_nfl", "americanfootball_nfl_preseason"],
    "NCAAF": ["americanfootball_ncaaf"],
    "NCAAB": ["basketball_ncaab"],
    "NHL": ["icehockey_nhl", "icehockey_nhl_preseason"],
    "NBA": ["basketball_nba", "basketball_nba_preseason"],
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


def _fetch_events(sport_key):
    """Events for one sport key. [] on any problem (a preseason key that isn't
    live yet returns 404/422 -- that's expected, not an error worth shouting
    about, so it logs at debug)."""
    url = f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/odds"
    try:
        resp = requests.get(url, params={
            "apiKey": config.ODDS_API_KEY, "regions": "us", "markets": "h2h",
            "oddsFormat": "american",
        }, timeout=15)
        if resp.status_code in (404, 422):
            logger.debug("Odds API key %s not currently active (%s).", sport_key, resp.status_code)
            return []
        resp.raise_for_status()
        events = resp.json()
        return events if isinstance(events, list) else []
    except Exception as exc:
        logger.debug("Odds API schedule fetch failed for %s: %s", sport_key, exc)
        return []


def schedule_from_odds_api(sport, date_str):
    """Returns list[Game] for `sport` on the local day `date_str`, merged
    across every candidate sport key (regular season + preseason)."""
    sport_keys = ODDS_API_SPORT_KEYS.get(sport)
    if not sport_keys:
        return []
    if not config.ODDS_API_KEY:
        logger.warning("%s schedule fallback: no ODDS_API_KEY -- cannot derive schedule.", sport)
        return []

    events = []
    for key in sport_keys:
        found = _fetch_events(key)
        if found:
            logger.info("%s schedule: %d event(s) from key %s.", sport, len(found), key)
        events.extend(found)

    if not events:
        logger.info("%s schedule (Odds API): no events from any key %s.", sport, sport_keys)
        return []

    try:
        tz = ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = ZoneInfo("America/New_York")

    normalize = _normalizer(sport)
    games = []
    seen = set()
    for ev in events:
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

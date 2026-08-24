"""
data/schedule_provider_wnba.py
================================
Today's WNBA schedule.

PRIMARY source is ESPN, fetched through data/espn_fetch.py, which tries
several ESPN hosts (site.api, site.web.api, and the cdn.espn.com core feed).
That multi-host retry is the whole point: a single blocked host used to make
ESPN look empty from GitHub Actions, which pushed us onto the paid Odds API --
and when those credits ran out there was no schedule left at all, so the WNBA
tab disappeared from the report entirely.

The Odds API is now a LAST-RESORT fallback only, so a normal day costs zero
odds credits just to learn which games exist.

Never raises -- any failure logs and returns [] so the daily run still
produces a report.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import config
from engine.models import Game
from data.teams_wnba import normalize_wnba_team
from data.espn_fetch import fetch_scoreboard_events

logger = logging.getLogger(__name__)

ESPN_LEAGUE_PATH = "basketball/wnba"
ODDS_API_WNBA = f"{config.ODDS_API_BASE_URL}/sports/basketball_wnba/odds"


def get_todays_wnba_games(date_str):
    """date_str: 'YYYY-MM-DD'. ESPN (multi-host) first, Odds API last resort."""
    games = _from_espn(date_str)
    if games:
        return games
    logger.info("WNBA schedule: no ESPN events from any host -- falling back to The Odds API "
                "(this costs credits; ESPN normally covers it for free).")
    return _from_odds_api(date_str)


def _from_espn(date_str):
    events = fetch_scoreboard_events(
        ESPN_LEAGUE_PATH, date_str,
        season_types=(None, 1, 2, 3),
        referer="https://www.espn.com/wnba/scoreboard",
    )
    games = []
    for event in events:
        try:
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            competitors = competitions[0].get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            home_team = home.get("team", {}) or {}
            away_team = away.get("team", {}) or {}
            home_name = (home_team.get("displayName") or home_team.get("name")
                         or home_team.get("abbreviation") or "")
            away_name = (away_team.get("displayName") or away_team.get("name")
                         or away_team.get("abbreviation") or "")
            if not home_name or not away_name:
                continue
            games.append(Game(
                game_id=f"wnba-{event['id']}",
                date=date_str,
                home_team=normalize_wnba_team(home_name),
                away_team=normalize_wnba_team(away_name),
                game_time_utc=event.get("date") or competitions[0].get("date"),
                sport="WNBA",
            ))
        except Exception as exc:
            logger.warning("Skipping one WNBA (ESPN) game we couldn't parse: %s", exc)
    if games:
        logger.info("WNBA schedule (ESPN): %d game(s) for %s -- %s", len(games), date_str,
                    ", ".join(f"{g.away_team}@{g.home_team}" for g in games))
    return games


def _from_odds_api(date_str):
    if not config.ODDS_API_KEY:
        logger.warning("WNBA fallback: no ODDS_API_KEY -- cannot derive schedule.")
        return []
    try:
        resp = requests.get(ODDS_API_WNBA, params={
            "apiKey": config.ODDS_API_KEY, "regions": "us", "markets": "h2h",
            "oddsFormat": "american",
        }, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except Exception as exc:
        logger.error("WNBA fallback (Odds API) fetch failed for %s: %s", date_str, exc)
        return []

    try:
        tz = ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = ZoneInfo("America/New_York")

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
        home = normalize_wnba_team(ev.get("home_team", ""))
        away = normalize_wnba_team(ev.get("away_team", ""))
        key = (home, away)
        if key in seen:
            continue
        seen.add(key)
        games.append(Game(
            game_id=f"wnba-{ev.get('id')}",
            date=date_str,
            home_team=home,
            away_team=away,
            game_time_utc=ct,
            sport="WNBA",
        ))
    logger.info("WNBA schedule (Odds API fallback): %d game(s) for %s.", len(games), date_str)
    return games

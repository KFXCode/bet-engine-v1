"""
data/schedule_provider_wnba.py
================================
Today's WNBA schedule. PRIMARY source is ESPN's public scoreboard, but ESPN
frequently blocks GitHub Actions' datacenter IPs outright (empty response,
regardless of User-Agent) -- which is why WNBA silently never showed up.

So the real workhorse is the FALLBACK: when ESPN returns nothing, we derive
the schedule from The Odds API WNBA feed (basketball_wnba), which is proven to
work from the runner (it's the same paid feed that supplies WNBA odds). Games
are bucketed to the local day (config.TIMEZONE) so a late tip that lands after
midnight UTC still counts as today.

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

logger = logging.getLogger(__name__)

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
ODDS_API_WNBA = f"{config.ODDS_API_BASE_URL}/sports/basketball_wnba/odds"

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.espn.com/wnba/scoreboard",
}


def get_todays_wnba_games(date_str):
    """date_str: 'YYYY-MM-DD'. Tries ESPN, falls back to The Odds API."""
    games = _from_espn(date_str)
    if games:
        return games
    logger.info("WNBA schedule: ESPN returned nothing (likely IP-blocked on the runner) -- "
                "falling back to The Odds API feed.")
    return _from_odds_api(date_str)


def _from_espn(date_str):
    try:
        resp = requests.get(ESPN_SCOREBOARD, params={"dates": date_str.replace("-", "")},
                            headers=BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("WNBA ESPN fetch failed for %s: %s", date_str, exc)
        return []

    events = payload.get("events", [])
    logger.info("WNBA schedule (ESPN): %d event(s) for %s.", len(events), date_str)
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
            games.append(Game(
                game_id=f"wnba-{event['id']}",
                date=date_str,
                home_team=normalize_wnba_team(home.get("team", {}).get("displayName", "")),
                away_team=normalize_wnba_team(away.get("team", {}).get("displayName", "")),
                game_time_utc=event.get("date"),
                sport="WNBA",
            ))
        except Exception as exc:
            logger.warning("Skipping one WNBA (ESPN) game we couldn't parse: %s", exc)
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

"""
data/odds_providers.py
=======================
Moneyline/spread/total odds. Two implementations behind one interface:

- MockOddsProvider   : deterministic-per-day synthetic odds, zero setup.
- TheOddsApiProvider : real odds from https://the-odds-api.com, FanDuel book.

config.ODDS_MODE picks which one get_odds_provider() hands back. Every
network path falls back to mock data on failure so a bad API response never
crashes the daily run.

PRESEASON KEYS (Aug 22, 2026): The Odds API keeps NFL preseason under its own
sport key (`americanfootball_nfl_preseason`); the regular-season key is empty
in August. Each sport maps to a LIST of keys and events from all of them are
merged, so preseason games get REAL FanDuel prices.

RESPONSE CACHE (Aug 23, 2026): the API quota was burned out mid-month, which
knocked WNBA and NFL off the report entirely -- with no credits their
schedules returned nothing, so there were no games and therefore no tabs.
The biggest waste was RE-RUNS: every manual run re-billed the full slate,
even when re-running minutes apart just to refresh lineups or republish.
Raw responses are now cached on disk for config.ODDS_CACHE_MINUTES and reused,
so repeat runs cost ZERO credits. A quota/auth failure (401) is also detected
explicitly and logged loudly instead of silently degrading to fake numbers.
"""

import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import config
from engine.models import MoneylineOdds
from data.teams import normalize_team as normalize_mlb_team
from data.teams_wnba import normalize_wnba_team
from data.teams_nfl import normalize_nfl_team
from data.teams_college import normalize_college_team
from data.teams_nhl import normalize_nhl_team
from data.teams_nba import normalize_nba_team

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(config.DATA_STORE_DIR) / "odds_cache"


def _cache_path(sport_key):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sport_key)
    return _CACHE_DIR / f"{safe}.json"


def _cache_read(sport_key):
    """Cached events for this key, or None if missing/stale/unreadable."""
    p = _cache_path(sport_key)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text())
        age_min = (time.time() - blob.get("cached_at", 0)) / 60.0
        if age_min > config.ODDS_CACHE_MINUTES:
            return None
        logger.info("Odds cache HIT for %s (%.0f min old, %d events) -- no API credits used.",
                    sport_key, age_min, len(blob.get("events", [])))
        return blob.get("events", [])
    except Exception:
        return None


def _cache_write(sport_key, events):
    try:
        _cache_path(sport_key).write_text(json.dumps({"cached_at": time.time(), "events": events}))
    except Exception as exc:
        logger.debug("Could not write odds cache for %s: %s", sport_key, exc)


def _normalize_for_sport(raw, sport):
    """Each sport spells team names differently and its abbreviation table
    isn't interchangeable with another's -- dispatch to the right normalizer
    or a game silently fails to match its real odds event and falls back to
    simulated numbers."""
    if sport == "WNBA":
        return normalize_wnba_team(raw)
    if sport == "NFL":
        return normalize_nfl_team(raw)
    if sport in ("NCAAF", "NCAAB"):
        return normalize_college_team(raw)
    if sport == "NHL":
        return normalize_nhl_team(raw)
    if sport == "NBA":
        return normalize_nba_team(raw)
    return normalize_mlb_team(raw)


class OddsProvider:
    def get_odds(self, games):
        """games: list[Game]. Returns dict game_id -> MoneylineOdds (latest)."""
        raise NotImplementedError


ODDS_API_SPORT_KEYS = {
    "MLB": ["baseball_mlb"],
    "WNBA": ["basketball_wnba"],
    "NFL": ["americanfootball_nfl", "americanfootball_nfl_preseason"],
    "NCAAF": ["americanfootball_ncaaf"],
    "NCAAB": ["basketball_ncaab"],
    "NHL": ["icehockey_nhl", "icehockey_nhl_preseason"],
    "NBA": ["basketball_nba", "basketball_nba_preseason"],
}


class MockOddsProvider(OddsProvider):
    """Deterministic-per-game-per-day synthetic odds so the whole pipeline is
    runnable with zero setup."""

    def get_odds(self, games):
        out = {}
        now = datetime.now(timezone.utc)
        for game in games:
            seed = f"{game.game_id}-{now:%Y-%m-%d}"
            rng = random.Random(seed)
            favorite_strength = rng.uniform(0.08, 0.35)
            home_is_favorite = rng.random() > 0.45
            drift = (now.hour - 12) * rng.uniform(-1.5, 1.5)

            if home_is_favorite:
                home_ml = -_prob_to_american(0.5 + favorite_strength) + drift
                away_ml = _prob_to_american(0.5 - favorite_strength) + drift
            else:
                away_ml = -_prob_to_american(0.5 + favorite_strength) + drift
                home_ml = _prob_to_american(0.5 - favorite_strength) + drift

            out[game.game_id] = MoneylineOdds(
                book="mock",
                home_ml=int(home_ml),
                away_ml=int(away_ml),
                captured_at=now.isoformat(),
                total=round(rng.uniform(7.5, 9.5) * 2) / 2,
            )
        return out


class TheOddsApiProvider(OddsProvider):
    """Real odds via The Odds API, FanDuel bookmaker, moneyline+spread+total."""

    def __init__(self, api_key=None, bookmaker=None, sport="MLB"):
        self.api_key = api_key or config.ODDS_API_KEY
        self.bookmaker = bookmaker or config.ODDS_API_BOOKMAKER
        self.sport = sport

    def _fetch_events(self, sport_key):
        """Events for ONE sport key, cache-first. [] on any problem."""
        cached = _cache_read(sport_key)
        if cached is not None:
            return cached

        url = f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": "us",
            "markets": "h2h,spreads,totals",
            "bookmakers": self.bookmaker,
            "oddsFormat": "american",
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 401:
                # Out of credits, or a bad key. This is the failure that took
                # WNBA/NFL off the report -- make it impossible to miss.
                logger.error("ODDS API 401 for %s -- OUT OF CREDITS or invalid key. "
                             "Check https://the-odds-api.com/account/. Falling back to "
                             "simulated odds; live prices will be wrong until this clears.",
                             sport_key)
                return []
            if resp.status_code == 429:
                logger.error("ODDS API rate-limited (429) on %s -- backing off this run.", sport_key)
                return []
            if resp.status_code in (404, 422):
                logger.debug("Odds API key %s not currently active (%s).", sport_key, resp.status_code)
                return []
            resp.raise_for_status()
            payload = resp.json()
            events = payload if isinstance(payload, list) else []
            remaining = resp.headers.get("x-requests-remaining")
            used = resp.headers.get("x-requests-used")
            if remaining is not None:
                logger.info("Odds API %s: %d events | credits remaining %s (used %s).",
                            sport_key, len(events), remaining, used)
                try:
                    if int(remaining) < 100:
                        logger.error("LOW ODDS API CREDITS: only %s left. Cut manual re-runs "
                                     "or the report will lose live odds and non-MLB sports.", remaining)
                except (TypeError, ValueError):
                    pass
            if events:
                _cache_write(sport_key, events)
            return events
        except Exception as exc:
            logger.warning("The Odds API request failed for %s (%s).", sport_key, exc)
            return []

    def get_odds(self, games):
        if not self.api_key:
            logger.warning("ODDS_API_KEY missing -- falling back to mock odds for this run.")
            return MockOddsProvider().get_odds(games)

        sport_keys = ODDS_API_SPORT_KEYS.get(self.sport)
        if not sport_keys:
            logger.warning("No Odds API sport key mapped for %s -- falling back to mock odds.", self.sport)
            return MockOddsProvider().get_odds(games)

        payload = []
        for key in sport_keys:
            found = self._fetch_events(key)
            if found:
                logger.info("%s odds: %d event(s) from key %s.", self.sport, len(found), key)
            payload.extend(found)

        if not payload:
            logger.error("No %s events from any Odds API key %s -- falling back to mock odds.",
                         self.sport, sport_keys)
            return MockOddsProvider().get_odds(games)

        by_teams = {}
        for event in payload:
            home = _normalize_for_sport(event.get("home_team", ""), self.sport)
            away = _normalize_for_sport(event.get("away_team", ""), self.sport)
            by_teams.setdefault((home, away), []).append(event)

        out = {}
        now = datetime.now(timezone.utc).isoformat()
        for game in games:
            event = _closest_event(by_teams.get((game.home_team, game.away_team), []), game)
            if not event:
                logger.warning("No live %s odds found for %s @ %s (checked %d events) -- "
                               "using simulated odds for this game only.",
                               self.sport, game.away_team, game.home_team, len(payload))
                out[game.game_id] = MockOddsProvider().get_odds([game])[game.game_id]
                continue
            out[game.game_id] = _parse_odds_event(event, self.bookmaker, game, now, self.sport)
        return out


def _closest_event(events, game):
    """From all feed events for this matchup, pick the one whose commence_time
    is nearest the game's scheduled start -- so today's game never grabs
    tomorrow's line."""
    if not events:
        return None
    if len(events) == 1 or not game.game_time_utc:
        return events[0]
    try:
        target = _parse_iso(game.game_time_utc)
    except Exception:
        return events[0]

    def _distance(ev):
        ct = ev.get("commence_time")
        if not ct:
            return float("inf")
        try:
            return abs((_parse_iso(ct) - target).total_seconds())
        except Exception:
            return float("inf")

    return min(events, key=_distance)


def _parse_iso(s):
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _parse_odds_event(event, bookmaker, game, captured_at, sport):
    home_ml = away_ml = None
    home_spread = away_spread = None
    total = None
    for bm in event.get("bookmakers", []):
        if bm.get("key") != bookmaker:
            continue
        for market in bm.get("markets", []):
            if market["key"] == "h2h":
                for outcome in market["outcomes"]:
                    if _normalize_for_sport(outcome["name"], sport) == game.home_team:
                        home_ml = outcome["price"]
                    elif _normalize_for_sport(outcome["name"], sport) == game.away_team:
                        away_ml = outcome["price"]
            elif market["key"] == "spreads":
                for outcome in market["outcomes"]:
                    if _normalize_for_sport(outcome["name"], sport) == game.home_team:
                        home_spread = outcome["point"]
                    elif _normalize_for_sport(outcome["name"], sport) == game.away_team:
                        away_spread = outcome["point"]
            elif market["key"] == "totals":
                if market["outcomes"]:
                    total = market["outcomes"][0].get("point")
    return MoneylineOdds(
        book=bookmaker, home_ml=home_ml, away_ml=away_ml, captured_at=captured_at,
        home_spread=home_spread, away_spread=away_spread, total=total,
    )


def _prob_to_american(p):
    if p >= 0.5:
        return round(100 * p / (1 - p))
    return round(100 * (1 - p) / p)


def get_odds_provider(sport="MLB"):
    if config.ODDS_MODE == "api":
        return TheOddsApiProvider(sport=sport)
    return MockOddsProvider()

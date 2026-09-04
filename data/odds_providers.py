"""
data/odds_providers.py
=======================
Moneyline/total odds. Two implementations behind one interface:

- MockOddsProvider   : deterministic synthetic odds. LOCAL DEV ONLY.
- TheOddsApiProvider : real odds from https://the-odds-api.com, FanDuel book.

NO FABRICATED PRICES IN API MODE (Sep 4, 2026). This used to substitute
MockOddsProvider for any game the live feed didn't return -- inventing a price
and then grading a real pick against it. That is the worst possible failure
mode: it produced a board of +563/+421/+321 "opportunities" that never existed,
and three NCAAF totals were "won" off fabricated baseball-shaped lines. It also
made the report warn that credits were exhausted when they weren't (287 of
20,000 used) -- because the warning inferred the cause instead of reporting it.

The order is now: LIVE price -> last REAL price we stored for that game -> no
pick at all. A game with no real price is simply left out of the slate. Fewer
picks on a thin day is strictly better than confident picks at invented odds.

The stored-price fallback matters mid-day: once a game starts it drops out of
the live feed, and without it every in-progress game would vanish from the
board along with the picks already published on it.

CREDIT DISCIPLINE:
1. MARKETS: The Odds API bills PER MARKET. We ask only for what each sport's
   model actually consumes (MARKETS_BY_SPORT) -- asking for h2h,spreads,totals
   everywhere was paying 3 credits for 1 credit of use.
2. CACHE: responses are cached 4h, so re-runs inside a window cost nothing.
3. FLOOR: paid calls stop while a reserve remains, and remaining credits are
   recorded so the report can state the true number instead of guessing.
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
from data.teams_nfl import normalize_nfl_team
from data.teams_college import normalize_college_team
from data.teams_nhl import normalize_nhl_team
from data.teams_nba import normalize_nba_team

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(config.DATA_STORE_DIR) / "odds_cache"

# Only markets the engine actually consumes. Spreads are never modeled; totals
# are projected for NCAAF only (engine/totals.TOTALS_SPORTS).
MARKETS_BY_SPORT = {
    "MLB": "h2h",
    "NFL": "h2h",
    "NCAAF": "h2h,totals",
    "NCAAB": "h2h",
    "NHL": "h2h",
    "NBA": "h2h",
}

CACHE_MINUTES = int(getattr(config, "ODDS_CACHE_MINUTES", 240) or 240)
if CACHE_MINUTES < 240:
    CACHE_MINUTES = 240

CREDIT_RESERVE = int(getattr(config, "ODDS_CREDIT_RESERVE", 250) or 250)

# Set once per process when the quota is confirmed dead.
_QUOTA_EXHAUSTED = False

# Last credit figures the API reported, so the report can state facts rather
# than infer a cause. None means we never got a successful response this run.
LAST_CREDITS = {"remaining": None, "used": None}


def credits_status():
    """(remaining, used) as last reported by the API, or (None, None)."""
    return LAST_CREDITS["remaining"], LAST_CREDITS["used"]


def quota_exhausted():
    return _QUOTA_EXHAUSTED


def _cache_path(sport_key):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sport_key)
    return _CACHE_DIR / f"{safe}.json"


def _cache_read(sport_key, allow_stale=False):
    p = _cache_path(sport_key)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text())
        age_min = (time.time() - blob.get("cached_at", 0)) / 60.0
        events = blob.get("events", [])
        if age_min > CACHE_MINUTES and not allow_stale:
            return None
        if allow_stale and age_min > CACHE_MINUTES:
            logger.warning("Using STALE cached odds for %s (%.0f min old) -- the API is "
                           "unavailable. Verify prices before betting.", sport_key, age_min)
        else:
            logger.info("Odds cache HIT for %s (%.0f min old, %d events) -- 0 credits used.",
                        sport_key, age_min, len(events))
        return events
    except Exception:
        return None


def _cache_write(sport_key, events):
    try:
        _cache_path(sport_key).write_text(json.dumps({"cached_at": time.time(), "events": events}))
    except Exception as exc:
        logger.debug("Could not write odds cache for %s: %s", sport_key, exc)


def _normalize_for_sport(raw, sport):
    """Each sport spells team names differently and its abbreviation table
    isn't interchangeable -- dispatch to the right normalizer or a game
    silently fails to match its odds event."""
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
        """games: list[Game]. Returns dict game_id -> MoneylineOdds. A game
        with no real price is OMITTED, never faked."""
        raise NotImplementedError


ODDS_API_SPORT_KEYS = {
    "MLB": ["baseball_mlb"],
    "NFL": ["americanfootball_nfl", "americanfootball_nfl_preseason"],
    "NCAAF": ["americanfootball_ncaaf"],
    "NCAAB": ["basketball_ncaab"],
    "NHL": ["icehockey_nhl", "icehockey_nhl_preseason"],
    "NBA": ["basketball_nba", "basketball_nba_preseason"],
}


class MockOddsProvider(OddsProvider):
    """Deterministic synthetic odds so the pipeline is runnable with zero
    setup. LOCAL DEVELOPMENT ONLY -- never reached when ODDS_MODE=api."""

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
    """Real odds via The Odds API, FanDuel bookmaker."""

    def __init__(self, api_key=None, bookmaker=None, sport="MLB"):
        self.api_key = api_key or config.ODDS_API_KEY
        self.bookmaker = bookmaker or config.ODDS_API_BOOKMAKER
        self.sport = sport

    def _markets(self):
        return MARKETS_BY_SPORT.get(self.sport, "h2h")

    def _fetch_events(self, sport_key):
        global _QUOTA_EXHAUSTED

        cached = _cache_read(sport_key)
        if cached is not None:
            return cached

        if _QUOTA_EXHAUSTED:
            stale = _cache_read(sport_key, allow_stale=True)
            return stale if stale is not None else []

        markets = self._markets()
        url = f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": "us",
            "markets": markets,
            "bookmakers": self.bookmaker,
            "oddsFormat": "american",
        }
        try:
            resp = requests.get(url, params=params, timeout=15)

            if resp.status_code == 401:
                _QUOTA_EXHAUSTED = True
                logger.error("ODDS API 401 on %s -- key rejected or quota gone. No further paid "
                             "calls this run. Check https://the-odds-api.com/account/",
                             sport_key)
                stale = _cache_read(sport_key, allow_stale=True)
                return stale if stale is not None else []
            if resp.status_code == 429:
                logger.error("ODDS API rate-limited (429) on %s -- backing off this run.", sport_key)
                stale = _cache_read(sport_key, allow_stale=True)
                return stale if stale is not None else []
            if resp.status_code in (404, 422):
                logger.debug("Odds API key %s not currently active (%s).", sport_key, resp.status_code)
                return []
            resp.raise_for_status()

            payload = resp.json()
            events = payload if isinstance(payload, list) else []

            remaining = resp.headers.get("x-requests-remaining")
            used = resp.headers.get("x-requests-used")
            last_cost = resp.headers.get("x-requests-last")
            try:
                if remaining is not None:
                    LAST_CREDITS["remaining"] = int(remaining)
                if used is not None:
                    LAST_CREDITS["used"] = int(used)
            except (TypeError, ValueError):
                pass
            logger.info("Odds API %s [markets=%s]: %d events | cost %s | remaining %s (used %s).",
                        sport_key, markets, len(events), last_cost or "?", remaining or "?", used or "?")

            if LAST_CREDITS["remaining"] is not None and LAST_CREDITS["remaining"] <= CREDIT_RESERVE:
                _QUOTA_EXHAUSTED = True
                logger.error("ODDS API CREDIT FLOOR: only %s credits left (reserve %d). Pausing "
                             "paid calls so the balance is saved for pre-game runs.",
                             LAST_CREDITS["remaining"], CREDIT_RESERVE)

            if events:
                _cache_write(sport_key, events)
            return events
        except Exception as exc:
            logger.warning("The Odds API request failed for %s (%s).", sport_key, exc)
            stale = _cache_read(sport_key, allow_stale=True)
            return stale if stale is not None else []

    def get_odds(self, games):
        if not self.api_key:
            logger.error("ODDS_API_KEY missing -- cannot price any game. No picks will be made.")
            return {}

        sport_keys = ODDS_API_SPORT_KEYS.get(self.sport)
        if not sport_keys:
            logger.warning("No Odds API sport key mapped for %s -- skipping the sport.", self.sport)
            return {}

        payload = []
        for key in sport_keys:
            found = self._fetch_events(key)
            if found:
                logger.info("%s odds: %d event(s) from key %s.", self.sport, len(found), key)
            payload.extend(found)

        by_teams = {}
        for event in payload:
            home = _normalize_for_sport(event.get("home_team", ""), self.sport)
            away = _normalize_for_sport(event.get("away_team", ""), self.sport)
            by_teams.setdefault((home, away), []).append(event)

        out = {}
        unpriced = []
        restored = 0
        now = datetime.now(timezone.utc).isoformat()
        for game in games:
            event = _closest_event(by_teams.get((game.home_team, game.away_team), []), game)
            if event:
                out[game.game_id] = _parse_odds_event(event, self.bookmaker, game, now, self.sport)
                continue

            # Not in the live feed -- almost always because the game already
            # started. Fall back to the last REAL price we stored for it, so
            # picks already published on that game keep their true number.
            stored = _last_real_line(game.game_id)
            if stored:
                out[game.game_id] = stored
                restored += 1
                continue

            # No live price and none ever stored: leave it out of the slate
            # entirely rather than invent one.
            unpriced.append(f"{game.away_team}@{game.home_team}")

        if restored:
            logger.info("%s odds: restored the last real stored line for %d game(s) already "
                        "underway or absent from the live feed.", self.sport, restored)
        if unpriced:
            logger.warning("%s odds: NO real price for %d game(s) -- excluded from the slate "
                           "(never priced with fake odds): %s",
                           self.sport, len(unpriced), ", ".join(unpriced[:8])
                           + (f" +{len(unpriced) - 8} more" if len(unpriced) > 8 else ""))
        return out


def _last_real_line(game_id):
    """The most recent non-mock odds snapshot stored for this game, or None."""
    try:
        from data.db import Database
        db = Database()
        row = db.get_last_real_line(game_id)
        if not row:
            return None
        return MoneylineOdds(
            book=row["book"], home_ml=row["home_ml"], away_ml=row["away_ml"],
            captured_at=row["captured_at"], home_spread=row["home_spread"],
            away_spread=row["away_spread"], total=row["total"],
        )
    except Exception as exc:
        logger.debug("Stored-line lookup failed for %s: %s", game_id, exc)
        return None


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

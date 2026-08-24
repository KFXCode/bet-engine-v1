"""
data/td_odds.py
================
Anytime-touchdown-scorer prices from The Odds API (paid player-props tier),
market key `player_anytime_td`.

Player props are per-EVENT on The Odds API, not per-sport, so each game needs
its own request:
    /sports/{sport_key}/events/{event_id}/odds?markets=player_anytime_td

Two things this has to get right:

1. EVENT IDS. Our NFL game_ids look like "nfl-<oddsapi event id>" (built in
   data/odds_api_schedule.py), so the id is recoverable by stripping the
   prefix. If a game_id doesn't carry a usable id we skip it rather than
   burning a credit on a guess.

2. SPORT KEY. Preseason lives under `americanfootball_nfl_preseason` and the
   regular season under `americanfootball_nfl` -- the same split that hid the
   NFL tab entirely in August. We try both and use whichever answers, so the
   September changeover needs no code edit.

Missing odds are NEVER fatal: the prop still shows with its model score and an
"odds n/a" note, exactly like HR props do.
"""

import logging
import re
import unicodedata

import requests

import config

logger = logging.getLogger(__name__)

MARKET = "player_anytime_td"
SPORT_KEYS = ["americanfootball_nfl", "americanfootball_nfl_preseason"]


def _norm(name):
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _event_id(game_id):
    gid = str(game_id or "")
    if gid.startswith("nfl-"):
        candidate = gid[4:]
        # Odds API ids are long hex-ish strings; ESPN ids are short digits.
        if len(candidate) >= 16:
            return candidate
    return None


def _fetch_event_props(sport_key, event_id):
    url = f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/events/{event_id}/odds"
    try:
        resp = requests.get(url, params={
            "apiKey": config.ODDS_API_KEY,
            "regions": "us",
            "markets": MARKET,
            "oddsFormat": "american",
            "bookmakers": config.ODDS_API_BOOKMAKER,
        }, timeout=20)
        if resp.status_code in (404, 422):
            return None
        if resp.status_code == 401:
            logger.warning("TD odds: 401 from The Odds API -- player props need the paid "
                           "props add-on. Props will show without prices.")
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.debug("TD odds fetch failed (%s / %s): %s", sport_key, event_id, exc)
        return None


def fetch_td_odds(candidates, games):
    """candidates: list of dicts with 'game_id' and 'player_name'.
    Returns {(game_id, normalized_name): {"odds": int, "book": str}}."""
    if not config.ODDS_API_KEY:
        logger.info("TD odds: no ODDS_API_KEY -- props will show without prices.")
        return {}

    wanted_games = {c.get("game_id") for c in candidates if c.get("game_id")}
    if not wanted_games:
        return {}

    out = {}
    priced_games = 0
    for game_id in wanted_games:
        event_id = _event_id(game_id)
        if not event_id:
            logger.debug("TD odds: no Odds API event id recoverable from %s -- skipping.", game_id)
            continue

        payload = None
        for sport_key in SPORT_KEYS:
            payload = _fetch_event_props(sport_key, event_id)
            if payload:
                break
        if not payload:
            continue

        found_here = 0
        for book in payload.get("bookmakers", []):
            book_key = book.get("key")
            for market in book.get("markets", []):
                if market.get("key") != MARKET:
                    continue
                for oc in market.get("outcomes", []):
                    player = oc.get("description") or oc.get("name")
                    price = oc.get("price")
                    if not player or price is None:
                        continue
                    key = (game_id, _norm(player))
                    if key not in out:
                        out[key] = {"odds": int(price), "book": book_key}
                        found_here += 1
        if found_here:
            priced_games += 1

    logger.info("TD odds: priced %d player(s) across %d/%d game(s).",
                len(out), priced_games, len(wanted_games))
    return out

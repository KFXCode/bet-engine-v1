"""
data/td_odds.py
================
Anytime-touchdown-scorer prices from The Odds API (paid player-props tier),
market key `player_anytime_td`.

Player props are per-EVENT on The Odds API, so each game needs its own
request:
    /sports/{sport_key}/events/{event_id}/odds?markets=player_anytime_td

EVENT ID RESOLUTION (Sep 14, 2026) -- the bug that made every TD prop show
"odds n/a" while the paid credits sat unused.

This module used to recover the Odds API event id by string-slicing our own
game_id:

    candidate = game_id[4:]          # "nfl-<something>"
    if len(candidate) >= 16:         # Odds API ids are long hex
        return candidate
    return None

That worked only while NFL schedules came from the Odds API fallback. Once the
ESPN schedule providers were made multi-host and started succeeding, game_ids
became ESPN ids instead -- "nfl-401872927", nine digits. The length check
failed on every game, `_event_id` returned None for all of them, and the
function skipped the entire slate WITHOUT MAKING A SINGLE REQUEST. No error,
no 401, no credits spent: ten TD props published priceless every week and the
report blamed the paid props tier for a problem that was ours.

A fix in one file caused a silent failure in an unrelated one, which is
exactly the pattern behind most of this system's bugs.

Event ids are now resolved the way data/prop_odds.py already did it (which is
why PLAYER props kept getting prices all along while TD props got none): pull
the Odds API /events list and match on normalized TEAM ABBREVIATIONS. That
works regardless of which provider supplied the schedule, so it can't break
again the next time a schedule source changes.

CREDIT DISCIPLINE: /events and each per-event response are cached on disk, and
a 401 stops all further paid calls for the run.

Missing odds are NEVER fatal -- the prop still shows with its model score and
an "odds n/a" note.
"""

import json
import logging
import re
import time
import unicodedata
from pathlib import Path

import requests

import config
from data.teams_nfl import normalize_nfl_team

logger = logging.getLogger(__name__)

MARKET = "player_anytime_td"
SPORT_KEYS = ["americanfootball_nfl", "americanfootball_nfl_preseason"]

CACHE_MINUTES = max(240, int(getattr(config, "ODDS_CACHE_MINUTES", 240) or 240))
_CACHE_DIR = Path(config.DATA_STORE_DIR) / "td_odds_cache"

# Set once per process when the quota is confirmed dead.
_QUOTA_EXHAUSTED = False

BOOK_LABELS = {
    "fanduel": "FanDuel", "draftkings": "DraftKings", "betmgm": "BetMGM",
    "williamhill_us": "Caesars", "betrivers": "BetRivers", "espnbet": "ESPN BET",
    "fanatics": "Fanatics",
}
# Prefer FanDuel, but take any US book rather than showing nothing -- FanDuel
# often posts TD markets late, and a real price from another book beats "n/a".
BOOK_PREFERENCE = ["fanduel", "draftkings", "betmgm", "williamhill_us",
                   "betrivers", "espnbet", "fanatics"]


def _norm(name):
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _cache_path(name):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))
    return _CACHE_DIR / f"{safe}.json"


def _cache_read(name, allow_stale=False):
    p = _cache_path(name)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text())
        age = (time.time() - blob.get("cached_at", 0)) / 60.0
        if age > CACHE_MINUTES and not allow_stale:
            return None
        if age > CACHE_MINUTES:
            logger.warning("TD odds: using STALE cache for %s (%.0f min) -- quota exhausted.",
                           name, age)
        return blob.get("data")
    except Exception:
        return None


def _cache_write(name, data):
    try:
        _cache_path(name).write_text(json.dumps({"cached_at": time.time(), "data": data}))
    except Exception as exc:
        logger.debug("TD odds cache write failed for %s: %s", name, exc)


def _event_id_map():
    """{(home_abbr, away_abbr): [event_id, ...]} from the Odds API /events
    list, across regular season and preseason keys.

    Matching on TEAMS rather than parsing our own game_id is the whole point --
    see the module docstring. It costs one cheap request and is immune to
    which provider supplied today's schedule."""
    global _QUOTA_EXHAUSTED

    cached = _cache_read("nfl_events")
    if cached is not None:
        return {tuple(k.split("|")): v for k, v in cached.items()}
    if _QUOTA_EXHAUSTED:
        stale = _cache_read("nfl_events", allow_stale=True)
        return {tuple(k.split("|")): v for k, v in stale.items()} if stale else {}

    out = {}
    for sport_key in SPORT_KEYS:
        url = f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/events"
        try:
            resp = requests.get(url, params={"apiKey": config.ODDS_API_KEY}, timeout=15)
            if resp.status_code == 401:
                _QUOTA_EXHAUSTED = True
                logger.error("TD odds: 401 OUT OF CREDITS (or key lacks the props add-on) -- "
                             "no further paid calls this run.")
                stale = _cache_read("nfl_events", allow_stale=True)
                return {tuple(k.split("|")): v for k, v in stale.items()} if stale else {}
            if resp.status_code in (404, 422):
                continue
            resp.raise_for_status()
            for ev in resp.json() or []:
                home = normalize_nfl_team(ev.get("home_team", ""))
                away = normalize_nfl_team(ev.get("away_team", ""))
                eid = ev.get("id")
                if home and away and eid:
                    out.setdefault((home, away), []).append(eid)
        except Exception as exc:
            logger.debug("TD odds: /events failed for %s: %s", sport_key, exc)

    if out:
        _cache_write("nfl_events", {"|".join(k): v for k, v in out.items()})
        logger.info("TD odds: resolved %d NFL matchup(s) from the Odds API events list.", len(out))
    else:
        logger.warning("TD odds: /events returned no NFL matchups -- cannot price TD props today.")
    return out


def _fetch_event_props(event_id):
    """Cached per event. Tries both sport keys."""
    global _QUOTA_EXHAUSTED

    cache_key = f"td_{event_id}"
    cached = _cache_read(cache_key)
    if cached is not None:
        logger.info("TD odds: cache HIT for event %s -- 0 credits used.", event_id)
        return cached
    if _QUOTA_EXHAUSTED:
        stale = _cache_read(cache_key, allow_stale=True)
        return stale if stale is not None else {}

    def pref_rank(bk):
        return BOOK_PREFERENCE.index(bk) if bk in BOOK_PREFERENCE else len(BOOK_PREFERENCE)

    prices = {}
    for sport_key in SPORT_KEYS:
        url = f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/events/{event_id}/odds"
        try:
            resp = requests.get(url, params={
                "apiKey": config.ODDS_API_KEY,
                "regions": "us",
                "markets": MARKET,
                "oddsFormat": "american",
            }, timeout=20)
            if resp.status_code == 401:
                _QUOTA_EXHAUSTED = True
                logger.error("TD odds: 401 on event %s -- stopping paid prop calls.", event_id)
                stale = _cache_read(cache_key, allow_stale=True)
                return stale if stale is not None else {}
            if resp.status_code in (404, 422):
                continue
            resp.raise_for_status()
            payload = resp.json() or {}
            remaining = resp.headers.get("x-requests-remaining")
            if remaining is not None:
                logger.info("TD odds: event %s | credits remaining %s.", event_id, remaining)

            for book in payload.get("bookmakers", []):
                bk = book.get("key")
                rank = pref_rank(bk)
                label = BOOK_LABELS.get(bk, bk)
                for market in book.get("markets", []):
                    if market.get("key") != MARKET:
                        continue
                    for oc in market.get("outcomes", []):
                        # Anytime-TD is a yes/no market: only take the YES side.
                        side = str(oc.get("name", "")).strip().lower()
                        if side in ("no", "under"):
                            continue
                        player = oc.get("description") or oc.get("participant")
                        price = oc.get("price")
                        if not player or price is None:
                            continue
                        key = _norm(player)
                        cur = prices.get(key)
                        if cur is None or rank < cur["_rank"]:
                            prices[key] = {"odds": int(price), "book": label, "_rank": rank}
            if prices:
                break
        except Exception as exc:
            logger.debug("TD odds fetch failed (%s / %s): %s", sport_key, event_id, exc)

    result = {k: {"odds": v["odds"], "book": v["book"]} for k, v in prices.items()}
    if result:
        _cache_write(cache_key, result)
    return result


def fetch_td_odds(candidates, games):
    """candidates: list of dicts with 'game_id' and 'player_name'.
    games:      list[Game] -- needed to map a game_id to its teams.
    Returns {(game_id, normalized_name): {"odds": int, "book": str}}."""
    if not config.ODDS_API_KEY:
        logger.info("TD odds: no ODDS_API_KEY -- props will show without prices.")
        return {}

    wanted = {c.get("game_id") for c in candidates if c.get("game_id")}
    if not wanted:
        return {}

    game_by_id = {g.game_id: g for g in games}
    event_map = _event_id_map()
    if not event_map:
        return {}

    out = {}
    priced_games = 0
    unmatched = []

    for game_id in wanted:
        game = game_by_id.get(game_id)
        if not game:
            continue
        event_ids = event_map.get((game.home_team, game.away_team), [])
        if not event_ids:
            unmatched.append(f"{game.away_team}@{game.home_team}")
            continue

        prices = {}
        for eid in event_ids:
            prices = _fetch_event_props(eid)
            if prices:
                break
        if not prices:
            continue

        priced_games += 1
        for key, val in prices.items():
            out[(game_id, key)] = val

    logger.info("TD odds: priced %d player(s) across %d/%d game(s).",
                len(out), priced_games, len(wanted))
    if unmatched:
        logger.warning("TD odds: no Odds API event matched %d game(s) -- team-abbr mismatch: %s",
                       len(unmatched), ", ".join(unmatched[:6]))
    return out

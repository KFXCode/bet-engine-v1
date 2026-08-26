"""
data/hr_odds.py
================
"To hit a home run" (Over 0.5 HR) odds for the day's chosen HR props, via The
Odds API player-props endpoint.

Book policy: PREFER FanDuel, but FALL BACK to any other US book when FanDuel
hasn't posted the market yet. FanDuel often posts HR props late, and
FanDuel-only code returned "odds n/a" on everything in that window even though
real prices existed elsewhere. We show the best available price and label the
book; when it isn't FanDuel, confirm on your app.

CREDIT CACHE (Aug 26, 2026): player props are the most expensive call in the
system -- The Odds API bills PER EVENT for them, so a 3-pick board cost 3+
credits on EVERY run, including re-runs minutes apart. Combined with the
moneyline over-fetch this drained all 20,000 monthly credits and silently
replaced real prices with simulated ones. Responses are now cached on disk per
event for CACHE_MINUTES, and a 401 stops all further paid calls for the run.

Details (confirmed against live API data):
  - Correct line is point == 0.5 ("to hit a HR"); never 1.5 (2+) / 2.5 (3+).
  - The Odds API can return the SAME game as TWO events (one empty). We keep
    ALL event ids per matchup and try each until one returns real prices.
  - Junk/placeholder prices (e.g. BetRivers +19900) are filtered.

Returns {(game_id, norm_name): {"odds": int, "book": str}}. Never raises.
"""

import json
import logging
import re
import time
import unicodedata
from pathlib import Path

import requests

import config
from data.teams import normalize_team

logger = logging.getLogger(__name__)

BOOK_PREFERENCE = ["fanduel", "williamhill_us", "draftkings", "betmgm",
                   "betrivers", "espnbet", "fanatics"]
BOOK_LABELS = {
    "fanduel": "FanDuel", "williamhill_us": "Caesars", "draftkings": "DraftKings",
    "betmgm": "BetMGM", "betrivers": "BetRivers", "espnbet": "ESPN BET",
    "fanatics": "Fanatics",
}
HR_ODDS_MAX_PRICE = 2000

CACHE_MINUTES = int(getattr(config, "ODDS_CACHE_MINUTES", 240) or 240)
if CACHE_MINUTES < 240:
    CACHE_MINUTES = 240

_CACHE_DIR = Path(config.DATA_STORE_DIR) / "props_cache"

_QUOTA_EXHAUSTED = False


def _norm_name(name):
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
            logger.warning("Using STALE cached HR props for %s (%.0f min) -- quota exhausted; "
                           "verify the price before betting.", name, age)
        return blob.get("data")
    except Exception:
        return None


def _cache_write(name, data):
    try:
        _cache_path(name).write_text(json.dumps({"cached_at": time.time(), "data": data}))
    except Exception as exc:
        logger.debug("Could not write props cache for %s: %s", name, exc)


def fetch_hr_odds(hr_props, games):
    """Returns {(game_id, norm_name): {"odds": int, "book": str}}."""
    if not (config.HR_ODDS_ENABLED and config.ODDS_MODE == "api" and config.ODDS_API_KEY and hr_props):
        logger.info("HR odds: skipped (enabled=%s mode=%s key=%s props=%d)",
                    config.HR_ODDS_ENABLED, config.ODDS_MODE, bool(config.ODDS_API_KEY),
                    len(hr_props or []))
        return {}

    game_by_id = {g.game_id: g for g in games}
    needed_game_ids = {p.get("game_id") for p in hr_props if p.get("game_id") in game_by_id}
    logger.info("HR odds: %d pick(s) across %d game(s) need odds.", len(hr_props), len(needed_game_ids))
    if not needed_game_ids:
        return {}

    event_map = _event_id_map()
    logger.info("HR odds: /events returned %d matchup(s).", len(event_map))
    if not event_map:
        return {}

    out = {}
    for game_id in needed_game_ids:
        game = game_by_id[game_id]
        event_ids = event_map.get((game.home_team, game.away_team), [])
        if not event_ids:
            logger.warning("HR odds: no Odds API event matched %s @ %s -- team-abbr mismatch.",
                           game.away_team, game.home_team)
            continue
        odds_by_player = {}
        for eid in event_ids:
            odds_by_player = _fetch_event_hr_odds(eid)
            if odds_by_player:
                break
        books_seen = {v["book"] for v in odds_by_player.values()}
        logger.info("HR odds: %s@%s -> %d player price(s) from: %s.",
                    game.away_team, game.home_team, len(odds_by_player),
                    ", ".join(sorted(books_seen)) or "none")
        for pick in [p for p in hr_props if p.get("game_id") == game_id]:
            key = _norm_name(pick["player_name"])
            if key in odds_by_player:
                out[(game_id, key)] = odds_by_player[key]
            else:
                logger.info("HR odds: '%s' not offered by any book.", pick["player_name"])
    logger.info("HR odds: matched prices for %d/%d pick(s).", len(out), len(hr_props))
    return out


def _event_id_map():
    """Matchup -> [event ids]. Cached: /events is cheap but pointless to repeat."""
    global _QUOTA_EXHAUSTED

    cached = _cache_read("mlb_events")
    if cached is not None:
        return {tuple(k.split("|")): v for k, v in cached.items()}

    if _QUOTA_EXHAUSTED:
        stale = _cache_read("mlb_events", allow_stale=True)
        return {tuple(k.split("|")): v for k, v in stale.items()} if stale else {}

    url = f"{config.ODDS_API_BASE_URL}/sports/baseball_mlb/events"
    try:
        resp = requests.get(url, params={"apiKey": config.ODDS_API_KEY}, timeout=15)
        if resp.status_code == 401:
            _QUOTA_EXHAUSTED = True
            logger.error("HR odds: 401 OUT OF CREDITS -- no further paid prop calls this run.")
            stale = _cache_read("mlb_events", allow_stale=True)
            return {tuple(k.split("|")): v for k, v in stale.items()} if stale else {}
        resp.raise_for_status()
        events = resp.json()
    except Exception as exc:
        logger.warning("HR odds: /events fetch failed (%s).", exc)
        stale = _cache_read("mlb_events", allow_stale=True)
        return {tuple(k.split("|")): v for k, v in stale.items()} if stale else {}

    out = {}
    for ev in events:
        home = normalize_team(ev.get("home_team", ""))
        away = normalize_team(ev.get("away_team", ""))
        out.setdefault((home, away), []).append(ev.get("id"))
    _cache_write("mlb_events", {"|".join(k): v for k, v in out.items()})
    return out


def _is_hr_yes_line(outcome):
    side = str(outcome.get("name", "")).lower()
    if side not in ("over", "yes"):
        return False
    point = outcome.get("point")
    return point is None or abs(float(point) - 0.5) < 1e-6


def _fetch_event_hr_odds(event_id):
    """Cached per event. Takes only the 0.5 'to hit a HR' line and keeps the
    best-preference book's price per player."""
    global _QUOTA_EXHAUSTED

    cache_key = f"hr_{event_id}"
    cached = _cache_read(cache_key)
    if cached is not None:
        logger.info("HR odds: cache HIT for event %s -- 0 credits used.", event_id)
        return cached

    if _QUOTA_EXHAUSTED:
        stale = _cache_read(cache_key, allow_stale=True)
        return stale if stale is not None else {}

    url = f"{config.ODDS_API_BASE_URL}/sports/baseball_mlb/events/{event_id}/odds"
    params = {
        "apiKey": config.ODDS_API_KEY,
        "regions": "us",
        "markets": config.ODDS_API_HR_MARKET,
        "oddsFormat": "american",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 401:
            _QUOTA_EXHAUSTED = True
            logger.error("HR odds: 401 OUT OF CREDITS on event %s -- stopping paid prop calls.", event_id)
            stale = _cache_read(cache_key, allow_stale=True)
            return stale if stale is not None else {}
        resp.raise_for_status()
        payload = resp.json()
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            logger.info("HR odds: event %s | credits remaining %s.", event_id, remaining)
    except Exception as exc:
        logger.warning("HR odds: event %s fetch failed: %s.", event_id, exc)
        stale = _cache_read(cache_key, allow_stale=True)
        return stale if stale is not None else {}

    def pref_rank(book_key):
        return BOOK_PREFERENCE.index(book_key) if book_key in BOOK_PREFERENCE else len(BOOK_PREFERENCE)

    prices = {}
    for bm in payload.get("bookmakers", []):
        book_key = bm.get("key")
        rank = pref_rank(book_key)
        label = BOOK_LABELS.get(book_key, book_key)
        for market in bm.get("markets", []):
            if market.get("key") != config.ODDS_API_HR_MARKET:
                continue
            for outcome in market.get("outcomes", []):
                if not _is_hr_yes_line(outcome):
                    continue
                player = _norm_name(outcome.get("description") or outcome.get("participant") or "")
                price = outcome.get("price")
                if not player or price is None:
                    continue
                price = int(price)
                if abs(price) > HR_ODDS_MAX_PRICE:
                    continue
                cur = prices.get(player)
                if cur is None or rank < cur["_rank"]:
                    prices[player] = {"odds": price, "book": label, "_rank": rank}

    result = {k: {"odds": v["odds"], "book": v["book"]} for k, v in prices.items()}
    if result:
        _cache_write(cache_key, result)
    return result

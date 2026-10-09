"""
data/espn_fetch.py
===================
Resilient ESPN scoreboard fetch, shared by every ESPN-based schedule provider.

WHY (Aug 24, 2026): WNBA and NFL tabs vanished from the report. The chain was:
ESPN's site.api host returns nothing from GitHub Actions' datacenter IPs, so
the provider fell through to The Odds API -- and once the paid Odds API credits
ran out, there was NO schedule at all, so no games, so no tab.

The fix: ESPN publishes the SAME scoreboard on several hosts. All three of
these were verified returning identical data (same event ids) for the same
date:
    site.api.espn.com      (the original)
    site.web.api.espn.com  (alternate API host)
    cdn.espn.com/core/...  (CDN, different infrastructure entirely)

A block on one host is very unlikely to hit all three, so we try them in
order and return the first that yields events. This keeps schedules FREE
(no Odds API credits burned just to learn what games exist) and keeps the
Odds API purely as a last-resort fallback.

Note the CDN host nests its payload differently:
    site hosts -> {"events": [...]}
    cdn core   -> {"content": {"sbData": {"events": [...]}}}
Both shapes are unwrapped here so callers always just get a list of events.

=====================================================================
DATE FILTER (Oct 9, 2026) -- the "9-0 on a Tuesday" bug.
=====================================================================
On a day with no games for a league, ESPN does NOT return an empty board. It
quietly falls back to the CURRENT WEEK's games. The Oct 6 run (a Tuesday) asked
for that day's NFL slate, got Sunday and Monday's already-finished games back,
picked them as if they were upcoming, and graded them on the spot. The player
stats feeding the picks already included those games, so it "picked" players
who had already scored and went a perfect 9-0 on bets nobody could have made.

Every event is now checked against the requested date in the engine's
timezone (config.TIMEZONE), and anything from another day is dropped. A 10:30
PM ET kickoff is 02:30 UTC the next day, which is why the comparison uses local
time and not the UTC date. Events with no parseable start time are kept, so a
data quirk can't silently empty a real slate.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import config

logger = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}

# ESPN league paths, e.g. "basketball/wnba" -> cdn core slug "wnba".
CDN_CORE_SLUG = {
    "basketball/wnba": "wnba",
    "basketball/nba": "nba",
    "basketball/mens-college-basketball": "college-basketball",
    "football/nfl": "nfl",
    "football/college-football": "college-football",
    "hockey/nhl": "nhl",
    "baseball/mlb": "mlb",
}


def _tz():
    try:
        return ZoneInfo(config.TIMEZONE)
    except Exception:
        return ZoneInfo("America/New_York")


def _local_date(event, tz):
    """The event's start date in the engine's timezone, or None if unknown."""
    raw = event.get("date")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.astimezone(tz).strftime("%Y-%m-%d")
    except Exception:
        return None


def _extract_events(payload):
    if not isinstance(payload, dict):
        return []
    events = payload.get("events")
    if isinstance(events, list) and events:
        return events
    sb = (payload.get("content") or {}).get("sbData") or {}
    events = sb.get("events")
    return events if isinstance(events, list) else []


def fetch_scoreboard_events(league_path, date_str, season_types=(None,), referer=None,
                            same_day_only=True):
    """Events for `league_path` (e.g. 'basketball/wnba') on `date_str`
    ('YYYY-MM-DD'), trying every known ESPN host. season_types lets callers
    sweep pre/regular/post (NFL preseason lives under seasontype=1); pass
    (None,) to let ESPN decide.

    same_day_only (default True) drops any event whose local start date isn't
    date_str -- see the DATE FILTER note above. Returns [] on total failure --
    never raises."""
    day = date_str.replace("-", "")
    slug = CDN_CORE_SLUG.get(league_path)
    headers = dict(BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer
    tz = _tz()

    hosts = [
        f"https://site.api.espn.com/apis/site/v2/sports/{league_path}/scoreboard",
        f"https://site.web.api.espn.com/apis/site/v2/sports/{league_path}/scoreboard",
    ]
    if slug:
        hosts.append(f"https://cdn.espn.com/core/{slug}/scoreboard")

    seen_ids = set()
    collected = []
    dropped_other_days = 0
    for url in hosts:
        for stype in season_types:
            params = {"dates": day, "limit": 400}
            if stype is not None:
                params["seasontype"] = stype
            if "cdn.espn.com" in url:
                params["xhr"] = 1
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=15)
                resp.raise_for_status()
                events = _extract_events(resp.json())
            except Exception as exc:
                logger.debug("ESPN fetch failed (%s st=%s): %s", url, stype, exc)
                continue
            for ev in events:
                eid = ev.get("id")
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                if same_day_only:
                    local = _local_date(ev, tz)
                    if local is not None and local != date_str:
                        dropped_other_days += 1
                        continue
                collected.append(ev)
        if collected:
            logger.info("ESPN scoreboard %s %s: %d event(s) via %s%s.",
                        league_path, date_str, len(collected), url.split("/")[2],
                        f" ({dropped_other_days} from other days dropped)" if dropped_other_days else "")
            return collected

    if dropped_other_days:
        logger.info("ESPN scoreboard %s %s: no games that day (ignored %d event(s) ESPN returned "
                    "from other days).", league_path, date_str, dropped_other_days)
    else:
        logger.info("ESPN scoreboard %s %s: no events from any host.", league_path, date_str)
    return []

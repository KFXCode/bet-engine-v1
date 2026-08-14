"""
data/recent_form.py
====================
Recent-form HR signal from the MLB Stats API game logs -- the source that
loads reliably from GitHub Actions cloud IPs (unlike the pybaseball/Baseball
Savant barrel scrape, which is usually blocked there).

For a batter, get_recent_form(player_id) returns a dict over the last
LOOKBACK_DAYS of games:
    games, pa, hr, xbh, tb, ab
    iso            -- (TB - H) / AB   ... isolated power, the cleanest "pop" rate
    hr_rate        -- HR / PA
    hot_score      -- 0..1 rolled-up "how hot is this bat RIGHT NOW" number

This is the signal that catches a genuinely hot mid-power bat the season-total
scoring misses. Cached 12h in the stats_cache table (shared with stats_provider).
"""

import json
import logging
import time
from datetime import datetime

import requests

import config

logger = logging.getLogger("recent_form")

MLB_STATS_API = "https://statsapi.mlb.com/api/v1"
LOOKBACK_DAYS = 15
CACHE_TTL_HOURS = 12


def _season():
    return datetime.now().year


def _cache():
    import sqlite3
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS stats_cache (
        key TEXT PRIMARY KEY, payload TEXT NOT NULL, cached_at REAL NOT NULL)""")
    conn.commit()
    return conn


def _cache_get(conn, key):
    row = conn.execute("SELECT payload, cached_at FROM stats_cache WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    payload, cached_at = row
    if time.time() - cached_at > CACHE_TTL_HOURS * 3600:
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


def _cache_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO stats_cache (key, payload, cached_at) VALUES (?, ?, ?)",
                 (key, json.dumps(value), time.time()))
    conn.commit()


def get_recent_form(player_id):
    """player_id: statsapi personId. Returns a form dict, or None if no id /
    no recent games / any error (caller treats None as 'no bonus, no penalty')."""
    if not player_id:
        return None
    conn = _cache()
    key = f"recent_form:{player_id}:{_season()}:d{LOOKBACK_DAYS}"
    cached = _cache_get(conn, key)
    if cached is not None:
        return cached

    try:
        url = f"{MLB_STATS_API}/people/{player_id}/stats"
        resp = requests.get(url, params={
            "stats": "gameLog", "group": "hitting", "season": _season(),
        }, timeout=15)
        resp.raise_for_status()
        splits = resp.json().get("stats", [{}])[0].get("splits", [])
    except Exception as exc:
        logger.debug("recent-form fetch failed for %s: %s", player_id, exc)
        return None

    if not splits:
        _cache_set(conn, key, None)
        return None

    # splits are chronological; take the most recent LOOKBACK_DAYS calendar days.
    def _pdate(s):
        try:
            return datetime.strptime(s.get("date", ""), "%Y-%m-%d")
        except Exception:
            return None

    dated = [(s, _pdate(s)) for s in splits]
    dated = [(s, d) for s, d in dated if d is not None]
    if not dated:
        _cache_set(conn, key, None)
        return None
    latest = max(d for _, d in dated)
    window = [s for s, d in dated if (latest - d).days <= LOOKBACK_DAYS]

    ab = pa = hr = h = tb = xbh = 0
    for s in window:
        st = s.get("stat", {})
        ab += int(st.get("atBats", 0) or 0)
        pa += int(st.get("plateAppearances", 0) or 0)
        hr += int(st.get("homeRuns", 0) or 0)
        h += int(st.get("hits", 0) or 0)
        tb += int(st.get("totalBases", 0) or 0)
        doubles = int(st.get("doubles", 0) or 0)
        triples = int(st.get("triples", 0) or 0)
        xbh += doubles + triples + int(st.get("homeRuns", 0) or 0)

    iso = (tb - h) / ab if ab else 0.0
    hr_rate = hr / pa if pa else 0.0

    # hot_score: blend recent HR rate, ISO, and raw HR count into 0..1.
    #  - hr_rate 0.06+ (a HR every ~16 PA) is elite-hot  -> ~1.0 on that term
    #  - iso 0.250+ is strong power                       -> ~1.0 on that term
    #  - 3+ HR in the window is a clear heater            -> ~1.0 on that term
    hr_rate_term = min(1.0, hr_rate / 0.06)
    iso_term = min(1.0, iso / 0.250)
    count_term = min(1.0, hr / 3.0)
    hot_score = round(0.45 * hr_rate_term + 0.30 * iso_term + 0.25 * count_term, 3)

    form = {
        "games": len(window), "pa": pa, "ab": ab, "hr": hr, "xbh": xbh, "tb": tb,
        "iso": round(iso, 3), "hr_rate": round(hr_rate, 3), "hot_score": hot_score,
    }
    _cache_set(conn, key, form)
    return form

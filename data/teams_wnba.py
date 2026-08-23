"""
data/teams_wnba.py
===================
WNBA team list + name normalization, same pattern as data/teams.py (kept
separate rather than merged so MLB abbreviations never collide with WNBA
ones).

TWO FIXES (Aug 23, 2026):

1. EXPANSION TEAMS. Portland Fire (POR) and Toronto Tempo (TOR) joined for
   2026 and were missing from this table entirely.

2. SUBSTRING MISMATCH. The old fuzzy fallback did `if alias in key or key in
   alias` on raw substrings, so "Portland Fire" matched the two-letter alias
   "la" (port-LA-nd) and was normalized to LA -- the Sparks. Any short
   abbreviation could hijack a longer city name that happened to contain it.
   Matching is now: exact -> word-boundary alias (longest alias first, so
   "los angeles" beats "la") -> give up and use the raw prefix. No more
   accidental hits inside the middle of a word.
"""

import re

WNBA_TEAMS = {
    "ATL": {"name": "Atlanta Dream", "aliases": ["dream", "atlanta"]},
    "CHI": {"name": "Chicago Sky", "aliases": ["sky", "chicago"]},
    "CON": {"name": "Connecticut Sun", "aliases": ["sun", "connecticut"]},
    "DAL": {"name": "Dallas Wings", "aliases": ["wings", "dallas"]},
    "GSV": {"name": "Golden State Valkyries", "aliases": ["valkyries", "golden state"]},
    "IND": {"name": "Indiana Fever", "aliases": ["fever", "indiana"]},
    "LAS": {"name": "Las Vegas Aces", "aliases": ["aces", "las vegas", "vegas"]},
    "LA": {"name": "Los Angeles Sparks", "aliases": ["sparks", "los angeles"]},
    "MIN": {"name": "Minnesota Lynx", "aliases": ["lynx", "minnesota"]},
    "NY": {"name": "New York Liberty", "aliases": ["liberty", "new york"]},
    "PHX": {"name": "Phoenix Mercury", "aliases": ["mercury", "phoenix"]},
    "POR": {"name": "Portland Fire", "aliases": ["fire", "portland"]},
    "SEA": {"name": "Seattle Storm", "aliases": ["storm", "seattle"]},
    "TOR": {"name": "Toronto Tempo", "aliases": ["tempo", "toronto"]},
    "WAS": {"name": "Washington Mystics", "aliases": ["mystics", "washington"]},
}

_LOOKUP = {}
for _abbr, _info in WNBA_TEAMS.items():
    _LOOKUP[_abbr.lower()] = _abbr
    _LOOKUP[_info["name"].lower()] = _abbr
    for _alias in _info["aliases"]:
        _LOOKUP[_alias.lower()] = _abbr

# Longest aliases first so "los angeles" is tried before "la", "las vegas"
# before "vegas", etc. -- prevents a short alias winning on a longer name.
_ORDERED = sorted(_LOOKUP.items(), key=lambda kv: len(kv[0]), reverse=True)


def normalize_wnba_team(raw):
    if not raw:
        return raw
    key = raw.strip().lower()
    if key in _LOOKUP:
        return _LOOKUP[key]
    for alias, abbr in _ORDERED:
        # Word-boundary match only: "la" will hit "la storm" but NOT "portland".
        if re.search(rf"\b{re.escape(alias)}\b", key):
            return abbr
    return raw.strip().upper()[:3]


def wnba_team_full_name(abbr):
    return WNBA_TEAMS.get(abbr, {}).get("name", abbr)

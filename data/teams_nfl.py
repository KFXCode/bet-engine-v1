"""
data/teams_nfl.py
==================
NFL team list + name normalization, same pattern as data/teams.py (MLB) and
data/teams_wnba.py. Kept separate so each sport's abbreviations never collide.
"""

NFL_TEAMS = {
    "ARI": {"name": "Arizona Cardinals", "aliases": ["cardinals", "arizona"]},
    "ATL": {"name": "Atlanta Falcons", "aliases": ["falcons", "atlanta"]},
    "BAL": {"name": "Baltimore Ravens", "aliases": ["ravens", "baltimore"]},
    "BUF": {"name": "Buffalo Bills", "aliases": ["bills", "buffalo"]},
    "CAR": {"name": "Carolina Panthers", "aliases": ["panthers", "carolina"]},
    "CHI": {"name": "Chicago Bears", "aliases": ["bears", "chicago"]},
    "CIN": {"name": "Cincinnati Bengals", "aliases": ["bengals", "cincinnati"]},
    "CLE": {"name": "Cleveland Browns", "aliases": ["browns", "cleveland"]},
    "DAL": {"name": "Dallas Cowboys", "aliases": ["cowboys", "dallas"]},
    "DEN": {"name": "Denver Broncos", "aliases": ["broncos", "denver"]},
    "DET": {"name": "Detroit Lions", "aliases": ["lions", "detroit"]},
    "GB": {"name": "Green Bay Packers", "aliases": ["packers", "green bay"]},
    "HOU": {"name": "Houston Texans", "aliases": ["texans", "houston"]},
    "IND": {"name": "Indianapolis Colts", "aliases": ["colts", "indianapolis"]},
    "JAX": {"name": "Jacksonville Jaguars", "aliases": ["jaguars", "jacksonville"]},
    "KC": {"name": "Kansas City Chiefs", "aliases": ["chiefs", "kansas city"]},
    "LV": {"name": "Las Vegas Raiders", "aliases": ["raiders", "las vegas"]},
    "LAC": {"name": "Los Angeles Chargers", "aliases": ["chargers", "la chargers"]},
    "LAR": {"name": "Los Angeles Rams", "aliases": ["rams", "la rams"]},
    "MIA": {"name": "Miami Dolphins", "aliases": ["dolphins", "miami"]},
    "MIN": {"name": "Minnesota Vikings", "aliases": ["vikings", "minnesota"]},
    "NE": {"name": "New England Patriots", "aliases": ["patriots", "new england"]},
    "NO": {"name": "New Orleans Saints", "aliases": ["saints", "new orleans"]},
    "NYG": {"name": "New York Giants", "aliases": ["giants", "ny giants"]},
    "NYJ": {"name": "New York Jets", "aliases": ["jets", "ny jets"]},
    "PHI": {"name": "Philadelphia Eagles", "aliases": ["eagles", "philadelphia"]},
    "PIT": {"name": "Pittsburgh Steelers", "aliases": ["steelers", "pittsburgh"]},
    "SF": {"name": "San Francisco 49ers", "aliases": ["49ers", "san francisco", "niners"]},
    "SEA": {"name": "Seattle Seahawks", "aliases": ["seahawks", "seattle"]},
    "TB": {"name": "Tampa Bay Buccaneers", "aliases": ["buccaneers", "bucs", "tampa bay"]},
    "TEN": {"name": "Tennessee Titans", "aliases": ["titans", "tennessee"]},
    "WAS": {"name": "Washington Commanders", "aliases": ["commanders", "washington"]},
}

_LOOKUP = {}
for _abbr, _info in NFL_TEAMS.items():
    _LOOKUP[_abbr.lower()] = _abbr
    _LOOKUP[_info["name"].lower()] = _abbr
    for _alias in _info["aliases"]:
        _LOOKUP[_alias.lower()] = _abbr


def normalize_nfl_team(raw):
    if not raw:
        return raw
    key = raw.strip().lower()
    if key in _LOOKUP:
        return _LOOKUP[key]
    for alias, abbr in _LOOKUP.items():
        if alias in key or key in alias:
            return abbr
    return raw.strip().upper()[:3]


def nfl_team_full_name(abbr):
    return NFL_TEAMS.get(abbr, {}).get("name", abbr)

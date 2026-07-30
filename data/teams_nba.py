"""
data/teams_nba.py
==================
NBA team list + name normalization, same pattern as the other pro-league
team files. Kept separate so each sport's abbreviations never collide.
"""

NBA_TEAMS = {
    "ATL": {"name": "Atlanta Hawks", "aliases": ["hawks", "atlanta"]},
    "BOS": {"name": "Boston Celtics", "aliases": ["celtics", "boston"]},
    "BKN": {"name": "Brooklyn Nets", "aliases": ["nets", "brooklyn"]},
    "CHA": {"name": "Charlotte Hornets", "aliases": ["hornets", "charlotte"]},
    "CHI": {"name": "Chicago Bulls", "aliases": ["bulls", "chicago"]},
    "CLE": {"name": "Cleveland Cavaliers", "aliases": ["cavaliers", "cavs", "cleveland"]},
    "DAL": {"name": "Dallas Mavericks", "aliases": ["mavericks", "mavs", "dallas"]},
    "DEN": {"name": "Denver Nuggets", "aliases": ["nuggets", "denver"]},
    "DET": {"name": "Detroit Pistons", "aliases": ["pistons", "detroit"]},
    "GSW": {"name": "Golden State Warriors", "aliases": ["warriors", "golden state"]},
    "HOU": {"name": "Houston Rockets", "aliases": ["rockets", "houston"]},
    "IND": {"name": "Indiana Pacers", "aliases": ["pacers", "indiana"]},
    "LAC": {"name": "LA Clippers", "aliases": ["clippers", "la clippers"]},
    "LAL": {"name": "Los Angeles Lakers", "aliases": ["lakers", "la lakers", "los angeles lakers"]},
    "MEM": {"name": "Memphis Grizzlies", "aliases": ["grizzlies", "memphis"]},
    "MIA": {"name": "Miami Heat", "aliases": ["heat", "miami"]},
    "MIL": {"name": "Milwaukee Bucks", "aliases": ["bucks", "milwaukee"]},
    "MIN": {"name": "Minnesota Timberwolves", "aliases": ["timberwolves", "wolves", "minnesota"]},
    "NOP": {"name": "New Orleans Pelicans", "aliases": ["pelicans", "pels", "new orleans"]},
    "NYK": {"name": "New York Knicks", "aliases": ["knicks", "new york"]},
    "OKC": {"name": "Oklahoma City Thunder", "aliases": ["thunder", "oklahoma city", "okc"]},
    "ORL": {"name": "Orlando Magic", "aliases": ["magic", "orlando"]},
    "PHI": {"name": "Philadelphia 76ers", "aliases": ["76ers", "sixers", "philadelphia"]},
    "PHX": {"name": "Phoenix Suns", "aliases": ["suns", "phoenix"]},
    "POR": {"name": "Portland Trail Blazers", "aliases": ["trail blazers", "blazers", "portland"]},
    "SAC": {"name": "Sacramento Kings", "aliases": ["kings", "sacramento"]},
    "SAS": {"name": "San Antonio Spurs", "aliases": ["spurs", "san antonio"]},
    "TOR": {"name": "Toronto Raptors", "aliases": ["raptors", "toronto"]},
    "UTA": {"name": "Utah Jazz", "aliases": ["jazz", "utah"]},
    "WAS": {"name": "Washington Wizards", "aliases": ["wizards", "washington"]},
}

_LOOKUP = {}
for _abbr, _info in NBA_TEAMS.items():
    _LOOKUP[_abbr.lower()] = _abbr
    _LOOKUP[_info["name"].lower()] = _abbr
    for _alias in _info["aliases"]:
        _LOOKUP[_alias.lower()] = _abbr


def normalize_nba_team(raw):
    if not raw:
        return raw
    key = raw.strip().lower()
    if key in _LOOKUP:
        return _LOOKUP[key]
    for alias, abbr in _LOOKUP.items():
        if alias in key or key in alias:
            return abbr
    return raw.strip().upper()[:3]


def nba_team_full_name(abbr):
    return NBA_TEAMS.get(abbr, {}).get("name", abbr)

"""
data/teams_nhl.py
==================
NHL team list + name normalization, same pattern as the other pro-league
team files. Kept separate so each sport's abbreviations never collide.
"""

NHL_TEAMS = {
    "ANA": {"name": "Anaheim Ducks", "aliases": ["ducks", "anaheim"]},
    "BOS": {"name": "Boston Bruins", "aliases": ["bruins", "boston"]},
    "BUF": {"name": "Buffalo Sabres", "aliases": ["sabres", "buffalo"]},
    "CGY": {"name": "Calgary Flames", "aliases": ["flames", "calgary"]},
    "CAR": {"name": "Carolina Hurricanes", "aliases": ["hurricanes", "canes", "carolina"]},
    "CHI": {"name": "Chicago Blackhawks", "aliases": ["blackhawks", "chicago"]},
    "COL": {"name": "Colorado Avalanche", "aliases": ["avalanche", "avs", "colorado"]},
    "CBJ": {"name": "Columbus Blue Jackets", "aliases": ["blue jackets", "columbus"]},
    "DAL": {"name": "Dallas Stars", "aliases": ["stars", "dallas"]},
    "DET": {"name": "Detroit Red Wings", "aliases": ["red wings", "detroit"]},
    "EDM": {"name": "Edmonton Oilers", "aliases": ["oilers", "edmonton"]},
    "FLA": {"name": "Florida Panthers", "aliases": ["panthers", "florida"]},
    "LAK": {"name": "Los Angeles Kings", "aliases": ["kings", "la kings", "los angeles"]},
    "MIN": {"name": "Minnesota Wild", "aliases": ["wild", "minnesota"]},
    "MTL": {"name": "Montreal Canadiens", "aliases": ["canadiens", "habs", "montreal"]},
    "NSH": {"name": "Nashville Predators", "aliases": ["predators", "preds", "nashville"]},
    "NJD": {"name": "New Jersey Devils", "aliases": ["devils", "new jersey"]},
    "NYI": {"name": "New York Islanders", "aliases": ["islanders", "isles"]},
    "NYR": {"name": "New York Rangers", "aliases": ["rangers"]},
    "OTT": {"name": "Ottawa Senators", "aliases": ["senators", "sens", "ottawa"]},
    "PHI": {"name": "Philadelphia Flyers", "aliases": ["flyers", "philadelphia"]},
    "PIT": {"name": "Pittsburgh Penguins", "aliases": ["penguins", "pens", "pittsburgh"]},
    "SJS": {"name": "San Jose Sharks", "aliases": ["sharks", "san jose"]},
    "SEA": {"name": "Seattle Kraken", "aliases": ["kraken", "seattle"]},
    "STL": {"name": "St. Louis Blues", "aliases": ["blues", "st louis", "st. louis"]},
    "TBL": {"name": "Tampa Bay Lightning", "aliases": ["lightning", "bolts", "tampa bay"]},
    "TOR": {"name": "Toronto Maple Leafs", "aliases": ["maple leafs", "leafs", "toronto"]},
    "UTA": {"name": "Utah Hockey Club", "aliases": ["utah", "utah hockey club", "mammoth"]},
    "VAN": {"name": "Vancouver Canucks", "aliases": ["canucks", "vancouver"]},
    "VGK": {"name": "Vegas Golden Knights", "aliases": ["golden knights", "vegas", "las vegas"]},
    "WSH": {"name": "Washington Capitals", "aliases": ["capitals", "caps", "washington"]},
    "WPG": {"name": "Winnipeg Jets", "aliases": ["jets", "winnipeg"]},
}

_LOOKUP = {}
for _abbr, _info in NHL_TEAMS.items():
    _LOOKUP[_abbr.lower()] = _abbr
    _LOOKUP[_info["name"].lower()] = _abbr
    for _alias in _info["aliases"]:
        _LOOKUP[_alias.lower()] = _abbr


def normalize_nhl_team(raw):
    if not raw:
        return raw
    key = raw.strip().lower()
    if key in _LOOKUP:
        return _LOOKUP[key]
    for alias, abbr in _LOOKUP.items():
        if alias in key or key in alias:
            return abbr
    return raw.strip().upper()[:3]


def nhl_team_full_name(abbr):
    return NHL_TEAMS.get(abbr, {}).get("name", abbr)

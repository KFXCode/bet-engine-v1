"""
data/stats_provider.py
=======================
Advanced pitching/batting metrics via pybaseball (free, wraps FanGraphs +
Baseball Savant). Every call is wrapped so a single failed lookup degrades
that ONE factor instead of killing the whole daily run.
"""

import logging
import time
from dataclasses import dataclass, asdict

import pandas as pd
import requests

import config
from data.http_utils import patch_requests_for_scraping

logger = logging.getLogger(__name__)

CACHE_TTL_HOURS = 12
MLB_STATS_API = "https://statsapi.mlb.com/api/v1"

patch_requests_for_scraping()


@dataclass
class PitcherProfile:
    name: str
    era: float = None
    fip: float = None
    k_pct: float = None
    bb_pct: float = None
    barrel_pct_allowed: float = None
    hard_hit_pct_allowed: float = None
    hr_per_9: float = None
    data_quality: str = "ok"


@dataclass
class TeamOffenseProfile:
    team: str
    barrel_pct: float = None
    hard_hit_pct: float = None
    woba: float = None
    data_quality: str = "ok"


@dataclass
class BatterProfile:
    name: str
    team: str = None
    barrel_pct: float = None
    hard_hit_pct: float = None
    iso: float = None
    hr_count: int = None
    recent_barrel_trend: float = None
    # --- expanded Statcast contact-quality metrics (Grok HR system) ---
    avg_exit_velo: float = None    # season avg EV on batted balls (mph)
    max_exit_velo: float = None    # season max EV (mph) -- raw power ceiling
    xwoba: float = None            # expected wOBA -- "true talent" contact
    hr_fb_pct: float = None        # HR per fly ball (%)
    pull_pct: float = None         # pull% -- most HRs are pulled
    data_quality: str = "ok"


class _StatsCache:
    def __init__(self, db_path=None):
        import sqlite3
        self.conn = sqlite3.connect(str(db_path or config.DB_PATH))
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS stats_cache (
                   key TEXT PRIMARY KEY, payload TEXT NOT NULL, cached_at REAL NOT NULL
               )"""
        )
        self.conn.commit()

    def get(self, key):
        import json
        row = self.conn.execute(
            "SELECT payload, cached_at FROM stats_cache WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        payload, cached_at = row
        if time.time() - cached_at > CACHE_TTL_HOURS * 3600:
            return None
        try:
            return json.loads(payload)
        except Exception:
            return None

    def set(self, key, value):
        import json
        self.conn.execute(
            "INSERT OR REPLACE INTO stats_cache (key, payload, cached_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        self.conn.commit()


class StatsProvider:
    def get_pitcher_profile(self, pitcher_name, player_id=None):
        raise NotImplementedError

    def get_team_offense_profile(self, team_abbr):
        raise NotImplementedError

    def get_batter_profile(self, batter_name, team_abbr=None):
        raise NotImplementedError


class PyBaseballStatsProvider(StatsProvider):
    def __init__(self):
        self.cache = _StatsCache()
        self._pitching_table = None
        self._batting_table = None
        self._pitcher_barrel_table = None
        self._batter_barrel_table = None
        self._batter_savant_table = None   # expected-stats leaderboard (xwOBA, etc.)

    def get_pitcher_profile(self, pitcher_name, player_id=None):
        if not pitcher_name or pitcher_name == "TBD":
            return PitcherProfile(name=pitcher_name or "TBD", data_quality="missing")

        cache_key = f"pitcher:{pitcher_name}:{_season()}"
        cached = self.cache.get(cache_key)
        if cached:
            return PitcherProfile(**cached)

        profile = PitcherProfile(name=pitcher_name, data_quality="not_found")

        if player_id:
            mlb_stats = _mlb_stats_api_pitcher_season(player_id)
            if mlb_stats:
                profile.era = mlb_stats.get("era")
                profile.hr_per_9 = mlb_stats.get("hr_per_9")
                profile.k_pct = mlb_stats.get("k_pct")
                profile.bb_pct = mlb_stats.get("bb_pct")
                profile.data_quality = "ok"

        try:
            row = self._lookup_pitcher_row(pitcher_name)
            if row is not None:
                profile.fip = _safe_float(row.get("FIP"))
                if profile.era is None:
                    profile.era = _safe_float(row.get("ERA"))
                if profile.hr_per_9 is None:
                    profile.hr_per_9 = _safe_float(row.get("HR/9"))
                if profile.data_quality == "not_found":
                    profile.data_quality = "ok"
            barrel, hard_hit = self._statcast_barrel_hard_hit_allowed(pitcher_name)
            profile.barrel_pct_allowed = barrel
            profile.hard_hit_pct_allowed = hard_hit
        except Exception as exc:
            logger.debug("FanGraphs/Statcast bonus lookup failed for %s: %s", pitcher_name, exc)

        if profile.era is None and profile.fip is None:
            profile.data_quality = "not_found"

        self.cache.set(cache_key, asdict(profile))
        return profile

    def _lookup_pitcher_row(self, pitcher_name):
        import pybaseball as pyb
        if self._pitching_table is None:
            pyb.cache.enable()
            season = _season()
            self._pitching_table = pyb.pitching_stats(season, season, qual=0)
        table = self._pitching_table
        matches = table[table["Name"].str.lower() == pitcher_name.lower()]
        if matches.empty:
            last = pitcher_name.split()[-1].lower()
            matches = table[table["Name"].str.lower().str.contains(last, na=False)]
        if matches.empty:
            return None
        return matches.iloc[0].to_dict()

    def _statcast_barrel_hard_hit_allowed(self, pitcher_name):
        try:
            import pybaseball as pyb
            if self._pitcher_barrel_table is None:
                self._pitcher_barrel_table = pyb.statcast_pitcher_exitvelo_barrels(_season(), minBBE=30)
            table = self._pitcher_barrel_table
            if table is None or table.empty:
                return None, None
            row = _match_savant_name(table, pitcher_name)
            if row is None:
                return None, None
            barrel = _plausible_barrel(_safe_float(row.get("brl_percent")))
            hard_hit = _safe_float(row.get("ev95percent"))
            return barrel, hard_hit
        except Exception as exc:
            logger.debug("statcast pitcher leaderboard lookup failed for %s: %s", pitcher_name, exc)
            return None, None

    def get_team_offense_profile(self, team_abbr):
        cache_key = f"team_offense:{team_abbr}:{_season()}"
        cached = self.cache.get(cache_key)
        if cached:
            return TeamOffenseProfile(**cached)
        try:
            import pybaseball as pyb
            season = _season()
            table = pyb.team_batting(season, season)
            row = table[table["Team"].str.contains(team_abbr, case=False, na=False)]
            if row.empty:
                profile = TeamOffenseProfile(team=team_abbr, data_quality="not_found")
            else:
                r = row.iloc[0].to_dict()
                profile = TeamOffenseProfile(
                    team=team_abbr, woba=_safe_float(r.get("wOBA")),
                    data_quality="partial",
                )
        except Exception as exc:
            logger.warning("team offense lookup failed for %s: %s", team_abbr, exc)
            profile = TeamOffenseProfile(team=team_abbr, data_quality="degraded")
        self.cache.set(cache_key, asdict(profile))
        return profile

    def get_batter_profile(self, batter_name, team_abbr=None):
        cache_key = f"batter:{batter_name}:{_season()}:v2"
        cached = self.cache.get(cache_key)
        if cached:
            return BatterProfile(**cached)
        try:
            import pybaseball as pyb
            if self._batter_barrel_table is None:
                # minBBE=30: exclude tiny batted-ball samples. With minBBE=0 a
                # hitter with 1 batted ball and 1 barrel reads as a 100% barrel
                # rate (the Bryan De La Cruz bug) -- impossible, and it was
                # inflating HR scores. 30 keeps regulars, kills the noise.
                self._batter_barrel_table = pyb.statcast_batter_exitvelo_barrels(_season(), minBBE=30)
            table = self._batter_barrel_table
            if table is None or table.empty:
                profile = BatterProfile(name=batter_name, team=team_abbr, data_quality="degraded")
            else:
                row = _match_savant_name(table, batter_name)
                if row is None:
                    profile = BatterProfile(name=batter_name, team=team_abbr, data_quality="not_found")
                else:
                    barrel = _plausible_barrel(_safe_float(row.get("brl_percent")))
                    hard_hit = _safe_float(row.get("ev95percent"))
                    avg_ev = _safe_float(row.get("avg_hit_speed"))
                    max_ev = _safe_float(row.get("max_hit_speed"))
                    pull_pct = _safe_float(row.get("pull_percent"))
                    hr_count = None
                    pid = row.get("player_id")
                    if pid is not None:
                        try:
                            hr_count = _mlb_stats_api_batter_hr(int(pid))
                        except Exception:
                            hr_count = None
                    xwoba = self._batter_xwoba(batter_name)
                    profile = BatterProfile(
                        name=batter_name, team=team_abbr, barrel_pct=barrel, hard_hit_pct=hard_hit,
                        hr_count=hr_count, avg_exit_velo=avg_ev, max_exit_velo=max_ev,
                        pull_pct=pull_pct, xwoba=xwoba,
                        data_quality="ok" if barrel is not None else "partial",
                    )
        except Exception as exc:
            logger.warning("batter profile lookup failed for %s: %s", batter_name, exc)
            profile = BatterProfile(name=batter_name, team=team_abbr, data_quality="degraded")
        self.cache.set(cache_key, asdict(profile))
        return profile

    def _batter_xwoba(self, batter_name):
        """xwOBA from Savant's expected-stats leaderboard (one bulk call,
        cached). Best-effort -- returns None on any failure."""
        try:
            import pybaseball as pyb
            if self._batter_savant_table is None:
                self._batter_savant_table = pyb.statcast_batter_expected_stats(_season(), minPA=50)
            table = self._batter_savant_table
            if table is None or table.empty:
                return None
            row = _match_savant_name(table, batter_name)
            if row is None:
                return None
            return _safe_float(row.get("est_woba"))
        except Exception as exc:
            logger.debug("xwOBA lookup failed for %s: %s", batter_name, exc)
            return None


def _season():
    from datetime import datetime
    return datetime.now().year


def _mlb_stats_api_batter_hr(player_id):
    import requests
    url = f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats"
    params = {"stats": "season", "group": "hitting", "season": _season()}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    splits = resp.json().get("stats", [{}])[0].get("splits", [])
    if not splits:
        return None
    hr = splits[0]["stat"].get("homeRuns")
    return int(hr) if hr is not None else None


def _mlb_stats_api_pitcher_season(player_id):
    try:
        url = f"{MLB_STATS_API}/people/{player_id}/stats"
        resp = requests.get(url, params={"stats": "season", "group": "pitching", "season": _season()}, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        splits = payload.get("stats", [{}])[0].get("splits", [])
        if not splits:
            return None
        s = splits[0]["stat"]
        ip = _safe_float(s.get("inningsPitched"))
        hr = _safe_float(s.get("homeRuns"))
        bf = _safe_float(s.get("battersFaced"))
        so = _safe_float(s.get("strikeOuts"))
        bb = _safe_float(s.get("baseOnBalls"))
        return {
            "era": _safe_float(s.get("era")),
            "hr_per_9": round(hr * 9 / ip, 2) if hr is not None and ip else None,
            "k_pct": round(100 * so / bf, 1) if so is not None and bf else None,
            "bb_pct": round(100 * bb / bf, 1) if bb is not None and bf else None,
        }
    except Exception as exc:
        logger.debug("MLB Stats API pitcher lookup failed for player_id=%s: %s", player_id, exc)
        return None


def _match_savant_name(table, full_name):
    if "last_name, first_name" not in table.columns:
        return None
    first, last = _split_name(full_name)
    target = f"{last}, {first}".strip().lower()
    col = table["last_name, first_name"].astype(str).str.lower()
    exact = table[col == target]
    if not exact.empty:
        return exact.iloc[0].to_dict()
    contains = table[col.str.contains(last.lower(), na=False)]
    if not contains.empty:
        return contains.iloc[0].to_dict()
    return None


def _plausible_barrel(v):
    """Reject impossible/absurd barrel rates. Real MLB barrel% tops out ~26%;
    anything above 35 (or below 0) is a bad read (name mismatch or tiny
    sample) -- return None so it flows through as neutral, never elite."""
    if v is None:
        return None
    if v < 0 or v > 35:
        return None
    return v


def _safe_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _split_name(full_name):
    parts = full_name.strip().split()
    if len(parts) < 2:
        return full_name, ""
    return parts[0], " ".join(parts[1:])


class _MockStatsProvider(StatsProvider):
    def get_pitcher_profile(self, pitcher_name, player_id=None):
        return PitcherProfile(name=pitcher_name or "TBD", fip=4.20, era=4.20, k_pct=22.0,
                               bb_pct=8.0, barrel_pct_allowed=7.5, hard_hit_pct_allowed=38.0,
                               hr_per_9=1.3, data_quality="mock")

    def get_team_offense_profile(self, team_abbr):
        return TeamOffenseProfile(team=team_abbr, barrel_pct=7.5, hard_hit_pct=38.0, woba=0.315, data_quality="mock")

    def get_batter_profile(self, batter_name, team_abbr=None):
        return BatterProfile(name=batter_name, team=team_abbr, barrel_pct=7.5, hard_hit_pct=38.0,
                              iso=0.16, hr_count=20, recent_barrel_trend=0.0,
                              avg_exit_velo=90.0, max_exit_velo=110.0, xwoba=0.340,
                              hr_fb_pct=15.0, pull_pct=42.0, data_quality="mock")


def get_stats_provider():
    if config.STATS_MODE == "api":
        return PyBaseballStatsProvider()
    return _MockStatsProvider()

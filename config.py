"""
config.py
=========
Single source of truth for every tunable in the system.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_STORE_DIR = BASE_DIR / "data_store"
REPORTS_DIR = DATA_STORE_DIR / "reports"
DB_PATH = DATA_STORE_DIR / "betting_engine.db"
MANUAL_INPUTS_DIR = BASE_DIR / "manual_inputs"

DATA_STORE_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
MANUAL_INPUTS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Data source modes
# ---------------------------------------------------------------------------
ODDS_MODE = os.getenv("ODDS_MODE", "mock")            # mock | api
STATS_MODE = os.getenv("STATS_MODE", "api")

PUBLIC_BETTING_MODE = os.getenv("PUBLIC_BETTING_MODE", "manual")  # manual | url | mock | api
PUBLIC_BETTING_URL = os.getenv("PUBLIC_BETTING_URL", "")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BOOKMAKER = "fanduel"
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"

HR_ODDS_ENABLED = True
ODDS_API_HR_MARKET = "batter_home_runs"

# ---------------------------------------------------------------------------
# Bankroll & staking
# ---------------------------------------------------------------------------
UNIT_SIZE_DOLLARS = float(os.getenv("UNIT_SIZE_DOLLARS", "100"))
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "0"))
FLAT_STAKE_UNITS = 1.0

# ---------------------------------------------------------------------------
# Strategy engine thresholds
# ---------------------------------------------------------------------------
MIN_EDGE = 0.02
# Per-sport minimum edge. MLB carries many data factors (pitching, OPS, HR/9,
# park) so it clears 2% readily. Sports with fewer live factors (records +
# public + astro only) produce smaller edges, so they get a slightly lower
# floor -- otherwise they'd almost never trigger a pick, which is why WNBA kept
# coming up empty. Still a real edge bar, just calibrated to each sport's
# available signal. As the scores-based records accumulate, these edges grow.
MIN_EDGE_BY_SPORT = {
    "MLB": 0.02,
    "WNBA": 0.015,
    "NBA": 0.015,
    "NHL": 0.015,
    "NFL": 0.015,
    "NCAAF": 0.015,
    "NCAAB": 0.015,
}


def min_edge_for(sport):
    return MIN_EDGE_BY_SPORT.get(sport, MIN_EDGE)


MAX_PLAYS_PER_DAY = 5
SECOND_PLAY_TOLERANCE = 0.0
TARGET_EDGE_MIN = 0.045
TARGET_EDGE_MAX = 0.05

# ---------------------------------------------------------------------------
# Sports covered
# ---------------------------------------------------------------------------
ENABLED_SPORTS = ["MLB", "WNBA", "NFL", "NCAAF", "NCAAB", "NHL", "NBA"]

DIVERSIFICATION_LOOKBACK_DAYS = 3
DIVERSIFICATION_EXTRA_EDGE = 0.03
DIVERSIFICATION_MIN_STRONG_FACTORS = 4

LINE_MOVE_DROP_CENTS = 15
LINE_MOVE_REQUIRES_SHARP_CONFIRM = True
HEAVY_MONEY_HANDLE_THRESHOLD = 0.65

# ---------------------------------------------------------------------------
# Fade list
# ---------------------------------------------------------------------------
FADE_ENABLED = False
FADE_MIN_EDGE = 0.05
FADE_MAX_PER_DAY = 5

# ---------------------------------------------------------------------------
# HR Prop workflow
# ---------------------------------------------------------------------------
HR_PROPS_ENABLED = True
HR_PROP_MIN_SCORE = 0
HR_PROP_MAX_PER_DAY = 3
HR_PROP_ROSTER_LIMIT = 9
HR_PROP_STRONG_SCORE = 70
# Lowered 12 -> 10: the 12 floor was benching hot mid-power value bats (the
# exact Day-1 winner profile, e.g. a rookie catcher with 10-11 HR in a great
# spot). 10 still requires legitimate power -- no more "7-HR guy" picks -- but
# lets the spot-driven value bats back into the pool.
HR_PROP_MIN_SEASON_HR = 10
HR_PROP_TOP_N_POOL = 20

HR_EV_FILTER_ENABLED = True
HR_MIN_EV_EDGE = 0.05
HR_PROB_BASE = 0.045
HR_PROB_PER_POINT = 0.0035
HR_PROB_MAX = 0.32
HR_PROB_MIN = 0.02

HR_CATEGORY_POINTS = {
    "contact_quality": 30,
    "park_weather": 25,
    "matchup": 25,
    "pitcher_context": 15,
    "confirmation": 5,
}
assert sum(HR_CATEGORY_POINTS.values()) == 100

HR_PROP_MIN_CLUSTERS = 3
HR_WEATHER_ENABLED = True

# ---------------------------------------------------------------------------
# Optional parlay
# ---------------------------------------------------------------------------
PARLAY_ENABLED = True
PARLAY_MAX_LEGS = 4
PARLAY_MIN_LEGS = 2

# ---------------------------------------------------------------------------
# Grading factor weights (model nudges the market, not replaces it)
# ---------------------------------------------------------------------------
FACTOR_WEIGHTS = {
    "matchup_pitching": 0.065,
    "public_sharp_split": 0.06,
    "advanced_analytics": 0.045,
    "underdog_value": 0.03,
    "historical_form": 0.025,
    "talent_gap": 0.025,
    "moon_zodiac": 0.03,
    "numerology": 0.02,
    "bullpen_fatigue": 0.02,
    "situational": 0.01,
    "motivation": 0.01,
}
assert abs(sum(FACTOR_WEIGHTS.values()) - 0.34) < 1e-9

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
DAILY_RUN_HOUR = int(os.getenv("DAILY_RUN_HOUR", "10"))
DAILY_RUN_MINUTE = int(os.getenv("DAILY_RUN_MINUTE", "0"))
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")
AUTO_RUN_LEAD_MINUTES = int(os.getenv("AUTO_RUN_LEAD_MINUTES", "60"))

# ---------------------------------------------------------------------------
# GitHub Pages publishing (optional)
# ---------------------------------------------------------------------------
GITHUB_PAGES_ENABLED = os.getenv("GITHUB_PAGES_ENABLED", "false").lower() == "true"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_PAGES_PATH = os.getenv("GITHUB_PAGES_PATH", "index.html")

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
MIN_SLATE_SIZE = 3
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

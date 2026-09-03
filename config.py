"""
config.py
=========
Single source of truth for every tunable in the system.
"""

import os
from datetime import date as _date
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

PUBLIC_BETTING_MODE = os.getenv("PUBLIC_BETTING_MODE", "manual")
PUBLIC_BETTING_URL = os.getenv("PUBLIC_BETTING_URL", "")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BOOKMAKER = "fanduel"
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"

# HR odds fetching is off with HR props retired -- leaving it on would keep
# spending paid player-prop credits on a market we no longer publish.
HR_ODDS_ENABLED = False
ODDS_API_HR_MARKET = "batter_home_runs"

# ---------------------------------------------------------------------------
# API CREDIT CONTROL
# ---------------------------------------------------------------------------
SPORT_SEASON_WINDOWS = {
    "MLB":   ((2, 15), (11, 15)),
    "WNBA":  ((4, 25), (10, 25)),
    "NFL":   ((7, 20), (2, 20)),
    "NCAAF": ((7, 25), (1, 25)),
    "NCAAB": ((10, 25), (4, 15)),
    "NHL":   ((9, 10), (6, 30)),
    "NBA":   ((9, 25), (6, 30)),
}

ODDS_CACHE_MINUTES = int(os.getenv("ODDS_CACHE_MINUTES", "240"))
ODDS_CREDIT_RESERVE = int(os.getenv("ODDS_CREDIT_RESERVE", "250"))


def in_season(sport, on_date=None):
    window = SPORT_SEASON_WINDOWS.get(sport)
    if not window:
        return True
    on_date = on_date or _date.today()
    start, end = window
    today = (on_date.month, on_date.day)
    if start <= end:
        return start <= today <= end
    return today >= start or today <= end


def sports_in_season(on_date=None):
    return [s for s in ENABLED_SPORTS if in_season(s, on_date)]


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
MIN_EDGE_BY_SPORT = {
    "MLB": 0.02, "WNBA": 0.015, "NBA": 0.015, "NHL": 0.015,
    "NFL": 0.015, "NCAAF": 0.015, "NCAAB": 0.015,
}


def min_edge_for(sport):
    return MIN_EDGE_BY_SPORT.get(sport, MIN_EDGE)


MAX_PLAYS_PER_DAY = 5
SECOND_PLAY_TOLERANCE = 0.0
TARGET_EDGE_MIN = 0.045
TARGET_EDGE_MAX = 0.05

# ---------------------------------------------------------------------------
# MONEYLINE PRICE POLICY  (from the Aug 29 grade of 215 graded picks)
# ---------------------------------------------------------------------------
# Actual results, flat 1 unit:
#     big dogs (+150 or longer)  28-30   +44.0u   ROI +75.8%
#     favorites (-200..-1)       64-38    +7.0u   ROI  +6.8%
#     heavy favs (-200 or worse) 21-6     +0.1u   ROI  +0.4%
#     small dogs (+1..+149)      12-16    -2.2u   ROI  -7.8%
# Heavy favorites win 78% of the time and return essentially NOTHING -- laying
# -235 to make 100 is break-even at best, and every slot spent there is a slot
# not spent on the one bucket that actually prints. Small dogs lose outright.
# So: refuse heavy chalk entirely, and require a bigger modelled edge on small
# dogs before they're allowed on the board.
ML_MAX_FAVORITE_PRICE = -200      # refuse anything at -200 or worse
ML_SMALL_DOG_MIN_EDGE = 0.045     # +1..+149 must clear a higher bar
ML_BIG_DOG_MIN_ODDS = 150         # the proven bucket

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
# HR PROPS -- RETIRED Sep 3, 2026
# ---------------------------------------------------------------------------
# MLB is moneyline-only now. The decision came straight off the ledger: 131
# graded HR picks went 11-120 (8.4%), and on the 40 that had a real recorded
# price that was -18.4 units at ROI -46%. An average price of +348 needs about
# 22% to break even, so the market was never mispriced in our favor -- the
# model was simply wrong about how often these hit, by a factor of roughly
# four. Recalibrating the curve made the +EV tag honest but didn't create an
# edge that wasn't there.
#
# Player props for the OTHER sports are unaffected: NFL anytime-TD props and
# NCAAF totals both stay live with their own models below.
#
# The settings are kept (not deleted) so the workflow can be switched back on
# in one line if HR props are ever revisited with a different approach.
HR_PROPS_ENABLED = False
HR_PROP_MIN_SCORE = 0
HR_PROP_MAX_PER_DAY = 3
HR_PROP_ROSTER_LIMIT = 9
HR_PROP_STRONG_SCORE = 70
HR_PROP_MIN_SEASON_HR = 4
HR_PROP_TOP_N_POOL = 200

HR_VALUE_LONGSHOT_SLOTS = 0
HR_LONGSHOT_MIN_ODDS = 450

HR_REQUIRE_REAL_ODDS = True
HR_TARGET_ODDS_MIN = 300
HR_TARGET_ODDS_MAX = 420
HR_HARD_ODDS_MIN = 200
HR_HARD_ODDS_MAX = 650
HR_OFF_BAND_MIN_SCORE = 72

HR_EV_FILTER_ENABLED = True
HR_MIN_EV_EDGE = 0.05
HR_PROB_BASE = 0.06
HR_PROB_PER_POINT = 0.0025
HR_PROB_MAX = 0.20
HR_PROB_MIN = 0.02

HR_CATEGORY_POINTS = {
    "contact_quality": 30, "park_weather": 25, "matchup": 25,
    "pitcher_context": 15, "confirmation": 5,
}
assert sum(HR_CATEGORY_POINTS.values()) == 100

HR_PROP_MIN_CLUSTERS = 3
HR_WEATHER_ENABLED = True

# ---------------------------------------------------------------------------
# NFL TD props  (still live)
# ---------------------------------------------------------------------------
TD_PROP_MAX_PER_DAY = 3
TD_PROP_STRONG_SCORE = 70
TD_MIN_EV_EDGE = 0.05
TD_MIN_LAMBDA = 0.06

# ---------------------------------------------------------------------------
# Totals (NCAAF)  (still live)
# ---------------------------------------------------------------------------
TOTALS_MIN_GAMES = 3
TOTALS_MIN_EDGE = 0.03
TOTALS_MAX_PER_DAY = 3
TOTALS_SPORTS = ["NCAAF"]

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
    "football_context": 0.035,
    "underdog_value": 0.03,
    "moon_zodiac": 0.03,
    "historical_form": 0.025,
    "talent_gap": 0.025,
    "numerology": 0.02,
    "bullpen_fatigue": 0.02,
    "situational": 0.01,
    "motivation": 0.01,
}
assert abs(sum(FACTOR_WEIGHTS.values()) - 0.375) < 1e-9

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

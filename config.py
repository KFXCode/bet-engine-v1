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
# EDGE FLOORS BY SPORT (raised for non-MLB, Sep 30, 2026).
# Until today the non-MLB floor was 1.5% -- LOWER than MLB's 2% -- even though
# non-MLB is the weaker model. That was backwards. The Sep 30 calibration grade
# of 52 non-MLB moneylines: model predicted 44.1%, won 38.5%, ROI -10.3%, and a
# Brier score slightly worse than the market's. MLB, over 324 picks, is
# calibrated and beats the market. MLB has probable pitchers, bullpen load,
# park and platoon inputs; the other moneylines run on price plus generic team
# factors. A thinner model has to show a BIGGER gap before its number is worth
# betting, so non-MLB now needs 4%.
MIN_EDGE = 0.02
MIN_EDGE_BY_SPORT = {
    "MLB": 0.02, "NBA": 0.04, "NHL": 0.04,
    "NFL": 0.04, "NCAAF": 0.04, "NCAAB": 0.04,
}


def min_edge_for(sport):
    return MIN_EDGE_BY_SPORT.get(sport, MIN_EDGE)


MAX_PLAYS_PER_DAY = 5
SECOND_PLAY_TOLERANCE = 0.0
TARGET_EDGE_MIN = 0.045
TARGET_EDGE_MAX = 0.05

# ---------------------------------------------------------------------------
# MONEYLINE PRICE POLICY
# ---------------------------------------------------------------------------
# Aug 29 grade of 215 picks, flat 1 unit:
#     big dogs (+150 or longer)  28-30   +44.0u   ROI +75.8%
#     favorites (-200..-1)       64-38    +7.0u   ROI  +6.8%
#     heavy favs (-200 or worse) 21-6     +0.1u   ROI  +0.4%
#     small dogs (+1..+149)      12-16    -2.2u   ROI  -7.8%
ML_MAX_FAVORITE_PRICE = -200      # refuse anything at -200 or worse
ML_SMALL_DOG_MIN_EDGE = 0.045     # +1..+149 must clear a higher bar
ML_BIG_DOG_MIN_ODDS = 150         # the proven bucket (on MLB)

# MLB: dogs rank first for the daily slots, and favourites need a 5% edge.
# Sep 30 calibration: 47.5 of MLB's +69 units came from 32 underdog picks the
# model rated 30-40% that actually won 59%; small-edge favourites returned
# only ~2-5% ROI.
ML_DOG_FIRST_SPORTS = ["MLB"]
ML_FAVORITE_MIN_EDGE = 0.05

# NON-MLB LONG DOGS need a 7% edge. The dog lean that PAYS on MLB is the exact
# thing BLEEDING elsewhere: non-MLB picks the model rated 30-40% won only 22.7%
# (22 picks, ROI -17.2%). MLB's model underrates its dogs; the non-MLB model
# overrates them. So a non-MLB +150 or longer now has to show a much wider gap.
ML_NON_MLB_LONG_DOG_MIN_EDGE = 0.07

# ---------------------------------------------------------------------------
# Sports covered
# ---------------------------------------------------------------------------
# WNBA RETIRED Sep 4, 2026 (10-12 on moneyline, and the source of most
# team-name settle bugs). Its modules stay in the repo; re-enabling is a
# one-word change.
ENABLED_SPORTS = ["MLB", "NFL", "NCAAF", "NCAAB", "NHL", "NBA"]

# Sports whose moneylines PUBLISH as bets. All of them -- non-MLB moneylines
# stay on the board and are improved through the stricter edge floors and the
# long-dog rule above rather than being hidden. Remove a sport from this list
# to make its moneylines tracking-only (still logged and graded, not published).
ML_BETTABLE_SPORTS = list(ENABLED_SPORTS)

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
# HR PROPS -- RETIRED Sep 3, 2026 (11-120 over 131 graded picks, ROI -46%).
# Settings kept so the workflow can be switched back on in one line.
# ---------------------------------------------------------------------------
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
# NFL ANYTIME-TD PROPS  (board 1 of 2) -- a CAP, not a quota.
# ---------------------------------------------------------------------------
TD_PROP_MAX_PER_DAY = 10
TD_PROP_STRONG_SCORE = 70
TD_MIN_EV_EDGE = 0.05
TD_MIN_LAMBDA = 0.06

# ---------------------------------------------------------------------------
# NFL PLAYER PROPS -- yards / receptions / pass TDs  (board 2 of 2)
# ---------------------------------------------------------------------------
PLAYER_PROPS_ENABLED = True
PLAYER_PROP_MAX_PER_DAY = 10
PLAYER_PROP_MIN_EDGE = 0.05

PLAYER_PROP_MARKETS = [
    "player_pass_yds",
    "player_rush_yds",
    "player_reception_yds",
    "player_receptions",
    "player_pass_tds",
]

PLAYER_PROP_MIN_GAMES = 3

PLAYER_PROP_SIGMA = {
    "player_pass_yds": 62.0,
    "player_rush_yds": 28.0,
    "player_reception_yds": 30.0,
    "player_receptions": 1.9,
    "player_pass_tds": 1.05,
}

# Unders are skipped for injury-questionable players: a scratch voids the bet
# at most books but grades it UNDER at a few.
PLAYER_PROP_SKIP_UNDER_IF_QUESTIONABLE = True

# ---------------------------------------------------------------------------
# Totals (NCAAF)
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

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

# HR prop odds (FanDuel). NOTE: player props are a PAID Odds API market -- on
# the free tier this returns nothing and the report shows "odds n/a". That's
# expected, not a bug.
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
MAX_PLAYS_PER_DAY = 5
SECOND_PLAY_TOLERANCE = 0.0
TARGET_EDGE_MIN = 0.045
TARGET_EDGE_MAX = 0.05

# ---------------------------------------------------------------------------
# Sports covered
# ---------------------------------------------------------------------------
# Each sport activates automatically when its schedule opens; off-season it
# just returns no games. All six leagues are wired.
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
HR_PROP_MIN_SEASON_HR = 12
HR_PROP_TOP_N_POOL = 20

# --- HR +EV edge filter (now that FanDuel HR odds are live) ---------------
# The system estimates each hitter's true HR probability from its 0-100 score
# and compares it to the sportsbook's implied probability. A pick is +EV only
# when our probability beats the book's implied by at least HR_MIN_EV_EDGE.
# When a hitter's HR odds are unavailable (free-tier gaps), we CAN'T compute
# EV -- those fall back to score-only so the slate is never empty, but genuine
# +EV picks are always preferred and shown first.
HR_EV_FILTER_ENABLED = True
HR_MIN_EV_EDGE = 0.05           # our_prob - implied_prob must clear this
# Score -> true HR probability map. Baseline hitter (~score 50) homers ~4.5%
# of games; each point above 50 adds ~0.35%. Capped so nothing reads as a
# lock. Tunable as real graded results come in.
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
# Grading factor weights (sum to 0.30 so the model nudges the market, not replaces it)
# ---------------------------------------------------------------------------
FACTOR_WEIGHTS = {
    "matchup_pitching": 0.065,
    "public_sharp_split": 0.05,
    "advanced_analytics": 0.045,
    "historical_form": 0.045,
    "talent_gap": 0.025,
    "moon_zodiac": 0.03,
    "numerology": 0.02,
    "situational": 0.01,
    "motivation": 0.01,
}
assert abs(sum(FACTOR_WEIGHTS.values()) - 0.30) < 1e-9

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

"""
engine/models.py
=================
Plain dataclasses shared across the pipeline. Keeping these in one file
means data/, engine/, and output/ all speak the same shapes without
importing each other in a tangle.
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class ProbablePitcher:
    name: str
    player_id: Optional[int] = None
    throws: Optional[str] = None  # 'L' or 'R'


@dataclass
class Game:
    game_id: str
    date: str
    home_team: str
    away_team: str
    game_time_utc: Optional[str]
    home_pitcher: Optional[ProbablePitcher] = None
    away_pitcher: Optional[ProbablePitcher] = None
    sport: str = "MLB"          # "MLB" | "WNBA" | ... -- drives which grading inputs apply
    pitchers_confirmed: bool = False   # True once BOTH starters confirmed via boxscore (see data/lineups.py)


@dataclass
class MoneylineOdds:
    book: str
    home_ml: Optional[int]
    away_ml: Optional[int]
    captured_at: str
    home_spread: Optional[float] = None
    away_spread: Optional[float] = None
    total: Optional[float] = None


@dataclass
class FactorScore:
    key: str
    label: str
    signal: float          # -1..+1, positive leans HOME, negative leans AWAY
    weight: float
    reasoning: str
    data_quality: str = "ok"   # ok | mock | manual | missing | degraded | partial | not_found


@dataclass
class SideEvaluation:
    game: Game
    odds: MoneylineOdds
    factor_scores: List[FactorScore]
    market_prob_home: Optional[float]
    market_prob_away: Optional[float]
    model_prob_home: Optional[float]
    model_prob_away: Optional[float]
    recommended_side: Optional[str]  # "home" | "away" | None
    edge_pct: float
    dropped_reason: Optional[str] = None


@dataclass
class Recommendation:
    game: Game
    side: str             # "home" | "away"
    team: str
    sport: str
    odds_american: int
    edge_pct: float
    model_prob: float
    market_prob: float
    stake_units: float
    stake_dollars: float
    reasoning: List[str]
    factor_scores: List[FactorScore]
    odds_source: str = "mock"   # MoneylineOdds.book -- "fanduel" (real) | "mock" (simulated fallback)
    diversification_flag: Optional[str] = None
    line_movement_flag: Optional[str] = None


@dataclass
class FadeTeam:
    """A team the system reads as bad value today -- opposite of a
    Recommendation, this is informational (never staked): 'don't bet this
    team's ML.' edge_pct is negative (magnitude of value against them)."""
    game: Game
    team: str
    sport: str
    opponent: str
    odds_american: Optional[int]
    odds_source: str
    edge_pct: float
    model_prob: Optional[float]
    market_prob: Optional[float]
    reasoning: List[str]


@dataclass
class ParlayRecommendation:
    legs: List[Recommendation]
    combined_odds_american: int
    combined_prob: float
    stake_units: float
    reasoning: str


@dataclass
class DailyReport:
    date: str
    slate_size: int
    plays: List[Recommendation]
    fade_teams: List[FadeTeam]
    hr_props: List[dict]
    parlay: Optional[ParlayRecommendation]
    dropped_notes: List[str]
    celestial: dict
    numerology: dict
    bankroll_summary: dict
    data_warnings: List[str]
    results_recap: dict = field(default_factory=dict)   # {date, items:[{label,status,kind}]} -- last completed slate's check/X scorecard"""
engine/models.py
=================
Plain dataclasses shared across the pipeline. Keeping these in one file
means data/, engine/, and output/ all speak the same shapes without
importing each other in a tangle.
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class ProbablePitcher:
    name: str
    player_id: Optional[int] = None
    throws: Optional[str] = None  # 'L' or 'R'


@dataclass
class Game:
    game_id: str
    date: str
    home_team: str
    away_team: str
    game_time_utc: Optional[str]
    home_pitcher: Optional[ProbablePitcher] = None
    away_pitcher: Optional[ProbablePitcher] = None
    sport: str = "MLB"          # "MLB" | "WNBA" | ... -- drives which grading inputs apply
    pitchers_confirmed: bool = False   # True once BOTH starters confirmed via boxscore (see data/lineups.py)


@dataclass
class MoneylineOdds:
    book: str
    home_ml: Optional[int]
    away_ml: Optional[int]
    captured_at: str
    home_spread: Optional[float] = None
    away_spread: Optional[float] = None
    total: Optional[float] = None


@dataclass
class FactorScore:
    key: str
    label: str
    signal: float          # -1..+1, positive leans HOME, negative leans AWAY
    weight: float
    reasoning: str
    data_quality: str = "ok"   # ok | mock | manual | missing | degraded | partial | not_found


@dataclass
class SideEvaluation:
    game: Game
    odds: MoneylineOdds
    factor_scores: List[FactorScore]
    market_prob_home: Optional[float]
    market_prob_away: Optional[float]
    model_prob_home: Optional[float]
    model_prob_away: Optional[float]
    recommended_side: Optional[str]  # "home" | "away" | None
    edge_pct: float
    dropped_reason: Optional[str] = None


@dataclass
class Recommendation:
    game: Game
    side: str             # "home" | "away"
    team: str
    sport: str
    odds_american: int
    edge_pct: float
    model_prob: float
    market_prob: float
    stake_units: float
    stake_dollars: float
    reasoning: List[str]
    factor_scores: List[FactorScore]
    odds_source: str = "mock"   # MoneylineOdds.book -- "fanduel" (real) | "mock" (simulated fallback)
    diversification_flag: Optional[str] = None
    line_movement_flag: Optional[str] = None


@dataclass
class FadeTeam:
    """A team the system reads as bad value today -- opposite of a
    Recommendation, this is informational (never staked): 'don't bet this
    team's ML.' edge_pct is negative (magnitude of value against them)."""
    game: Game
    team: str
    sport: str
    opponent: str
    odds_american: Optional[int]
    odds_source: str
    edge_pct: float
    model_prob: Optional[float]
    market_prob: Optional[float]
    reasoning: List[str]


@dataclass
class ParlayRecommendation:
    legs: List[Recommendation]
    combined_odds_american: int
    combined_prob: float
    stake_units: float
    reasoning: str


@dataclass
class DailyReport:
    date: str
    slate_size: int
    plays: List[Recommendation]
    fade_teams: List[FadeTeam]
    hr_props: List[dict]
    parlay: Optional[ParlayRecommendation]
    dropped_notes: List[str]
    celestial: dict
    numerology: dict
    bankroll_summary: dict
    data_warnings: List[str]
    results_recap: dict = field(default_factory=dict)   # {date, items:[{label,status,kind}]} -- last completed slate's check/X scorecard

"""
engine/models.py
=================
Plain dataclasses shared across the pipeline.
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class ProbablePitcher:
    name: str
    player_id: Optional[int] = None
    throws: Optional[str] = None


@dataclass
class Game:
    game_id: str
    date: str
    home_team: str
    away_team: str
    game_time_utc: Optional[str]
    home_pitcher: Optional[ProbablePitcher] = None
    away_pitcher: Optional[ProbablePitcher] = None
    sport: str = "MLB"
    pitchers_confirmed: bool = False
    game_number: int = 1
    doubleheader: bool = False

    def dh_label(self):
        return f" (Gm {self.game_number})" if self.doubleheader else ""

    def dh_reasoning(self):
        if not self.doubleheader:
            return None
        away_sp = self.away_pitcher.name if self.away_pitcher else "TBD"
        home_sp = self.home_pitcher.name if self.home_pitcher else "TBD"
        return (f"[DOUBLEHEADER] This is GAME {self.game_number} of a two-game day between "
                f"{self.away_team} and {self.home_team} -- starters {away_sp} (away) vs {home_sp} (home). "
                f"Make sure you bet the GAME {self.game_number} line specifically, not the other game.")


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
    signal: float
    weight: float
    reasoning: str
    data_quality: str = "ok"


@dataclass
class SideEvaluation:
    game: Game
    odds: MoneylineOdds
    factor_scores: List[FactorScore]
    market_prob_home: Optional[float]
    market_prob_away: Optional[float]
    model_prob_home: Optional[float]
    model_prob_away: Optional[float]
    recommended_side: Optional[str]
    edge_pct: float
    dropped_reason: Optional[str] = None


@dataclass
class Recommendation:
    game: Game
    side: str
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
    odds_source: str = "mock"
    diversification_flag: Optional[str] = None
    line_movement_flag: Optional[str] = None


@dataclass
class FadeTeam:
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
    results_recap: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    daily_parlay: dict = field(default_factory=dict)
    # Per-sport best parlays: {sport: parlay_dict}. Each sport's own ticket.
    sport_parlays: dict = field(default_factory=dict)
    # The single cross-sport TOP PARLAY -- best legs from any mix of sports.
    top_parlay: dict = field(default_factory=dict)
    # Sports that had at least one game today (drives which sections render).
    active_sports: list = field(default_factory=list)

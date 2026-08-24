"""
engine/totals.py
=================
Over/Under (game total) picks -- built for NCAA football, generic enough for
any sport whose records carry points-for / points-against.

HOW THE EDGE IS FOUND (and why this shape):
  1. PROJECT the game's total from both teams' scoring profiles:
         home_expected = (home PPG scored + away PPG allowed) / 2
         away_expected = (away PPG scored + home PPG allowed) / 2
         projected     = home_expected + away_expected
     Averaging each side's offense against the other's defense is the standard
     approach and it self-corrects: a team that has feasted on bad defenses
     gets pulled back toward what its opponent actually allows.

  2. CONVERT the gap to a probability instead of betting the gap directly.
     Final totals scatter widely around their expectation, so "we project 3
     points over the line" is NOT a 100% bet -- with a ~13-point spread of
     outcomes in college football it's barely a 59% one. We model the total as
     normal around the projection and read the probability off that curve:
         P(over) = 1 - CDF((line - projected) / sigma)
     Skipping this step is how totals models talk themselves into "huge
     edges" that are really noise.

  3. COMPARE to the market's implied number. Standard -110 pricing implies
     52.38%, so a real edge has to clear that, not 50%.

DATA HONESTY: early season there are almost no games stored, so scoring
averages are meaningless. MIN_GAMES_EACH gates that -- with too small a
sample we return no pick rather than a confident-looking guess.
"""

import logging
import math

import config

logger = logging.getLogger("totals")

# Spread of final totals around expectation, by sport (points).
# College football scores are the most volatile of these by a wide margin.
SIGMA_BY_SPORT = {
    "NCAAF": 13.5,
    "NFL": 10.5,
    "NCAAB": 12.0,
    "NBA": 11.5,
    "WNBA": 11.0,
    "NHL": 2.0,
    "MLB": 3.2,
}

STANDARD_PRICE = -110
MIN_GAMES_EACH = getattr(config, "TOTALS_MIN_GAMES", 3)
MIN_EDGE = getattr(config, "TOTALS_MIN_EDGE", 0.03)
MAX_PER_DAY = getattr(config, "TOTALS_MAX_PER_DAY", 3)
TOTALS_SPORTS = getattr(config, "TOTALS_SPORTS", ["NCAAF"])


def american_to_implied(ml):
    ml = float(ml)
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def _normal_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _ppg(record):
    """(points scored per game, points allowed per game, games) or None."""
    if not record:
        return None
    games = (record.get("wins") or 0) + (record.get("losses") or 0)
    if games <= 0:
        return None
    pf = record.get("runs_scored")
    pa = record.get("runs_allowed")
    if pf is None or pa is None:
        return None
    return pf / games, pa / games, games


def evaluate_totals(games, odds_by_game, team_records):
    """Returns a list of total picks (strongest edge first)."""
    picks = []
    skipped_no_line = 0
    skipped_thin = 0

    for game in games:
        if game.sport not in TOTALS_SPORTS:
            continue
        odds = odds_by_game.get(game.game_id)
        line = getattr(odds, "total", None) if odds else None
        if line is None:
            skipped_no_line += 1
            continue
        try:
            line = float(line)
        except (TypeError, ValueError):
            skipped_no_line += 1
            continue

        home = _ppg(team_records.get(game.home_team))
        away = _ppg(team_records.get(game.away_team))
        if not home or not away:
            skipped_thin += 1
            continue
        home_pf, home_pa, home_games = home
        away_pf, away_pa, away_games = away
        if home_games < MIN_GAMES_EACH or away_games < MIN_GAMES_EACH:
            skipped_thin += 1
            continue

        home_expected = (home_pf + away_pa) / 2.0
        away_expected = (away_pf + home_pa) / 2.0
        projected = home_expected + away_expected

        sigma = SIGMA_BY_SPORT.get(game.sport, 12.0)
        z = (line - projected) / sigma
        prob_over = 1.0 - _normal_cdf(z)
        prob_under = 1.0 - prob_over

        market_prob = american_to_implied(STANDARD_PRICE)
        if prob_over >= prob_under:
            side, model_prob = "over", prob_over
        else:
            side, model_prob = "under", prob_under
        edge = model_prob - market_prob
        if edge < MIN_EDGE:
            continue

        reasoning = [
            f"Projected total {projected:.1f} vs the market's {line:.1f} "
            f"({projected - line:+.1f} points).",
            f"{game.home_team}: scoring {home_pf:.1f}/gm, allowing {home_pa:.1f}/gm "
            f"({home_games} games stored).",
            f"{game.away_team}: scoring {away_pf:.1f}/gm, allowing {away_pa:.1f}/gm "
            f"({away_games} games stored).",
            f"Matchup projection -- {game.home_team} {home_expected:.1f}, "
            f"{game.away_team} {away_expected:.1f}.",
            f"Converted with a {sigma}-point spread of outcomes (how much {game.sport} totals "
            f"actually scatter), giving {side.upper()} a {model_prob*100:.1f}% chance.",
            f"Market at {STANDARD_PRICE} implies {market_prob*100:.1f}%, so the edge is "
            f"{edge*100:+.1f} points of probability -- not the raw {abs(projected - line):.1f}-point gap.",
        ]
        if min(home_games, away_games) < 6:
            reasoning.append(
                f"CAUTION: only {min(home_games, away_games)} games of scoring data on one side -- "
                f"early-season averages move fast, so treat this as a lean.")

        picks.append({
            "game_id": game.game_id,
            "sport": game.sport,
            "matchup": f"{game.away_team} @ {game.home_team}",
            "side": side,
            "line": line,
            "projected": round(projected, 1),
            "odds_american": STANDARD_PRICE,
            "model_prob": model_prob,
            "market_prob": market_prob,
            "edge_pct": edge,
            "reasoning": reasoning,
        })

    picks.sort(key=lambda p: p["edge_pct"], reverse=True)
    logger.info("TOTALS: %d pick(s) cleared %.1f%% edge (%d games had no posted total, "
                "%d had too little scoring data).",
                len(picks), MIN_EDGE * 100, skipped_no_line, skipped_thin)
    for p in picks[:6]:
        logger.info("TOTALS: %s %s %.1f -- proj %.1f, edge %+.1f%%",
                    p["matchup"], p["side"].upper(), p["line"], p["projected"], p["edge_pct"] * 100)
    return picks[:MAX_PER_DAY]


def label_for(pick):
    """Report/history label, e.g. 'Over 54.5 (BAMA @ LSU)'."""
    return f"{pick['side'].title()} {pick['line']:.1f} ({pick['matchup']})"

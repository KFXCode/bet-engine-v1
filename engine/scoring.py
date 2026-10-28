"""
engine/scoring.py
==================
Turns market odds + all graded factors into one number: model edge %.

Method (weighted-nudge-from-market):
  1. Convert American moneyline odds to implied probabilities.
  2. De-vig (remove the sportsbook's overround) to get a "fair" market
     probability for each side -- this is the number a bet needs to beat.
  3. Run every grading factor, each contributing +/- its configured weight
     (config.FACTOR_WEIGHTS) to a model probability that starts at the fair
     market number.
  4. edge_pct = model probability of the recommended side minus the fair
     market probability of that side.

FADE-PHASE MOON (Aug 21, 2026): the moon phase is now passed into the grading
context so score_moon_zodiac can DOUBLE its own weight on fade phases
(Waxing Gibbous / Full Moon / Waning Gibbous) -- the "public confidence high,
overvalued favorites become fade material" nights. On those days lunar energy
carries the same weight as the public/sharp money factor instead of being a
rounding error that heavy favorites sail straight through.
"""

from datetime import date as date_cls

from data.celestial import celestial_signal_for, moon_phase_for
from data.numerology import numerology_signal_for
from engine.grading_factors import GradingContext, score_all_factors
from engine.models import SideEvaluation
import config


def american_to_decimal(odds):
    odds = float(odds)
    return 1.0 + (odds / 100.0 if odds > 0 else 100.0 / -odds)


def _evaluate_vs_sharp(game, odds, factor_scores, fair_home, fair_away):
    """NBA/NHL: the model's chance IS the sharp books' fair chance. Pick the
    side FanDuel overpays, only if it pays SHARP_MIN_EV more than fair."""
    sharp_home = getattr(odds, "sharp_home_prob", None)
    if sharp_home is None:
        return SideEvaluation(game=game, odds=odds, factor_scores=factor_scores,
                              market_prob_home=fair_home, market_prob_away=fair_away,
                              model_prob_home=fair_home, model_prob_away=fair_away,
                              recommended_side=None, edge_pct=0.0,
                              dropped_reason="No sharp-book price for this game -- no pick.")
    sharp_away = 1.0 - sharp_home
    ev_home = sharp_home * american_to_decimal(odds.home_ml) - 1.0
    ev_away = sharp_away * american_to_decimal(odds.away_ml) - 1.0
    side, ev = ("home", ev_home) if ev_home >= ev_away else ("away", ev_away)
    edge = (sharp_home - fair_home) if side == "home" else (sharp_away - fair_away)
    reason = None
    if ev < config.SHARP_MIN_EV:
        reason = (f"FanDuel's best side pays only {ev:+.1%} vs the {odds.sharp_source} fair price "
                  f"(needs +{config.SHARP_MIN_EV:.0%}).")
    elif ev > config.SHARP_MAX_EV:
        reason = (f"FanDuel looks {ev:+.1%} better than {odds.sharp_source} -- too good to be real "
                  f"(likely a stale price). Skipped.")
    return SideEvaluation(
        game=game, odds=odds, factor_scores=factor_scores,
        market_prob_home=fair_home, market_prob_away=fair_away,
        model_prob_home=sharp_home, model_prob_away=sharp_away,
        recommended_side=None if reason else side, edge_pct=0.0 if reason else edge,
        dropped_reason=reason, sharp_ev=ev,
    )


def american_to_implied_prob(odds):
    if odds is None:
        return None
    odds = float(odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def devig_two_way(prob_home, prob_away):
    """Proportional de-vig: scales both implied probabilities down so they
    sum to 1.0, removing the sportsbook's overround."""
    if prob_home is None or prob_away is None:
        return prob_home, prob_away
    total = prob_home + prob_away
    if total <= 0:
        return prob_home, prob_away
    return prob_home / total, prob_away / total


def build_grading_context(game, odds, home_pitcher_profile, away_pitcher_profile,
                           home_offense, away_offense, home_record, away_record,
                           public_split, situational, run_date):
    fair_home, fair_away = devig_two_way(
        american_to_implied_prob(odds.home_ml), american_to_implied_prob(odds.away_ml)
    )
    home_is_favorite = (fair_home or 0) >= (fair_away or 0)

    raw_celestial, celestial_reasoning, _ = celestial_signal_for(run_date)
    raw_numerology, numerology_reasoning, _ = numerology_signal_for(run_date)
    # celestial/numerology speak in "favorite vs underdog" -- convert to home/away
    celestial_signal = raw_celestial if home_is_favorite else -raw_celestial
    numerology_signal = raw_numerology if home_is_favorite else -raw_numerology

    try:
        moon_phase, _illum = moon_phase_for(run_date)
    except Exception:
        moon_phase = None

    ctx = GradingContext(
        game=game, home_pitcher_profile=home_pitcher_profile, away_pitcher_profile=away_pitcher_profile,
        home_offense=home_offense, away_offense=away_offense, home_record=home_record, away_record=away_record,
        public_split=public_split, situational=situational,
        celestial_signal=celestial_signal, celestial_reasoning=celestial_reasoning,
        numerology_signal=numerology_signal, numerology_reasoning=numerology_reasoning,
        home_ml=odds.home_ml, away_ml=odds.away_ml,
        moon_phase=moon_phase, home_is_favorite=home_is_favorite,
    )
    return ctx, fair_home, fair_away


def evaluate_game(game, odds, home_pitcher_profile, away_pitcher_profile,
                   home_offense, away_offense, home_record, away_record,
                   public_split, situational, run_date=None):
    run_date = run_date or date_cls.today()
    ctx, fair_home, fair_away = build_grading_context(
        game, odds, home_pitcher_profile, away_pitcher_profile, home_offense,
        away_offense, home_record, away_record, public_split, situational, run_date,
    )
    factor_scores = score_all_factors(ctx)

    if fair_home is None or fair_away is None:
        return SideEvaluation(game=game, odds=odds, factor_scores=factor_scores,
                               market_prob_home=fair_home, market_prob_away=fair_away,
                               model_prob_home=fair_home, model_prob_away=fair_away,
                               recommended_side=None, edge_pct=0.0,
                               dropped_reason="No usable market odds today.")

    if config.uses_sharp_price(game.sport):
        return _evaluate_vs_sharp(game, odds, factor_scores, fair_home, fair_away)

    weighted_nudge = sum(fs.signal * fs.weight for fs in factor_scores)
    model_home = min(0.98, max(0.02, fair_home + weighted_nudge))
    model_away = 1.0 - model_home

    edge_home = model_home - fair_home
    edge_away = model_away - fair_away

    if edge_home >= edge_away:
        side, edge = "home", edge_home
    else:
        side, edge = "away", edge_away

    return SideEvaluation(
        game=game, odds=odds, factor_scores=factor_scores,
        market_prob_home=fair_home, market_prob_away=fair_away,
        model_prob_home=model_home, model_prob_away=model_away,
        recommended_side=side, edge_pct=edge,
    )

"""
engine/parlay.py
=================
Parlay builders:
  - maybe_build_parlay: the optional moon/numerology "green light" ML parlay.
  - build_daily_parlay: the always-on "Best Parlay of the Day" tab.
  - build_double_parlay: the 2 safest ML picks that combine to ~2x (double
    your money, roughly +100 combined).
"""

import config
from engine.models import ParlayRecommendation

GREEN_LIGHT_THRESHOLD = 0.35  # avg |signal| across celestial+numerology must clear this


def maybe_build_parlay(plays, celestial_signal, numerology_signal):
    if not config.PARLAY_ENABLED:
        return None
    if len(plays) < config.PARLAY_MIN_LEGS:
        return None

    combined_energy = (abs(celestial_signal) + abs(numerology_signal)) / 2
    if combined_energy < GREEN_LIGHT_THRESHOLD:
        return None

    legs = plays[: config.PARLAY_MAX_LEGS]
    combined_prob = 1.0
    combined_decimal_odds = 1.0
    for leg in legs:
        combined_prob *= leg.model_prob
        combined_decimal_odds *= _american_to_decimal(leg.odds_american)

    combined_american = _decimal_to_american(combined_decimal_odds)
    reasoning = (f"Moon/numerology green light today (combined energy {combined_energy:.2f} >= "
                 f"{GREEN_LIGHT_THRESHOLD}) -- every leg already clears MIN_EDGE on its own; "
                 f"this parlay is a bonus, not a substitute for the straight plays.")

    return ParlayRecommendation(
        legs=legs, combined_odds_american=combined_american,
        combined_prob=combined_prob, stake_units=config.FLAT_STAKE_UNITS,
        reasoning=reasoning,
    )


def build_daily_parlay(plays, hr_props, max_legs=None):
    """The always-on 'Best Parlay of the Day' (its own tab). Scans the bet
    types the system computes a real edge on today -- moneyline plays and HR
    props -- picks the highest-confidence legs (mixing both), caps at
    max_legs, and returns combined odds + win probability. Returns {} when
    there aren't at least 2 qualifying legs."""
    max_legs = max_legs or config.PARLAY_MAX_LEGS
    candidates = []

    for p in plays:
        conf = min(1.0, max(0.0, p.edge_pct * 10))
        candidates.append({
            "label": f"{p.team} ML ({p.odds_american:+d})",
            "kind": "moneyline", "odds": p.odds_american,
            "prob": p.model_prob, "confidence": conf,
        })

    for h in hr_props:
        odds = h.get("odds_american")
        if odds is None:
            continue
        score = h.get("score", 0)
        conf = min(1.0, max(0.0, (score - 60) / 40.0))
        candidates.append({
            "label": f"{h['player_name']} HR ({odds:+d})",
            "kind": "hr_prop", "odds": odds,
            "prob": _american_to_implied(odds), "confidence": conf,
        })

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    legs = candidates[:max_legs]
    if len(legs) < 2:
        return {}

    combined_prob = 1.0
    combined_decimal = 1.0
    for leg in legs:
        combined_prob *= leg["prob"]
        combined_decimal *= _american_to_decimal(leg["odds"])

    return {
        "legs": legs,
        "combined_odds_american": _decimal_to_american(combined_decimal),
        "combined_prob": combined_prob,
        "leg_count": len(legs),
    }


def build_double_parlay(plays):
    """'Double Your Money' -- the 2 SAFEST moneyline picks across all sports
    whose combined odds land near +100 (roughly doubles a 1-unit stake).
    Safest = highest model win probability. Returns {} if fewer than 2 ML
    plays exist. Picks the pair whose combined decimal odds are closest to
    2.0 while keeping both legs high-probability."""
    ml = sorted(plays, key=lambda p: p.model_prob, reverse=True)
    if len(ml) < 2:
        return {}

    # From the safest legs, find the pair whose combined odds are closest to
    # ~2x (decimal 2.0) so the payout roughly doubles the stake.
    top = ml[:6]  # only consider the 6 safest to keep both legs strong
    best_pair = None
    best_gap = None
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            dec = _american_to_decimal(top[i].odds_american) * _american_to_decimal(top[j].odds_american)
            gap = abs(dec - 2.0)
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_pair = (top[i], top[j])

    if not best_pair:
        return {}

    combined_prob = 1.0
    combined_decimal = 1.0
    legs = []
    for leg in best_pair:
        combined_prob *= leg.model_prob
        combined_decimal *= _american_to_decimal(leg.odds_american)
        legs.append({
            "label": f"{leg.team} ML ({leg.odds_american:+d})",
            "sport": leg.sport, "kind": "moneyline",
        })

    return {
        "legs": legs,
        "combined_odds_american": _decimal_to_american(combined_decimal),
        "combined_prob": combined_prob,
        "leg_count": 2,
    }


def _american_to_implied(ml):
    ml = float(ml)
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def _american_to_decimal(ml):
    ml = float(ml)
    return 1 + (ml / 100.0 if ml > 0 else 100.0 / -ml)


def _decimal_to_american(decimal_odds):
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1) * 100)
    return round(-100 / (decimal_odds - 1))

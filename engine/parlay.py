"""
engine/parlay.py
=================
Parlay builders:
  - maybe_build_parlay : the optional moon/numerology "green light" ML parlay.
  - build_daily_parlay : the always-on "Best Parlay of the Day" tab.
  - build_double_parlay: 2 ML picks combining to roughly +100 (~2x).

PRICE POLICY (Aug 29, 2026) -- from the grade of 215 graded moneylines:
    big dogs (+150 or longer)   28-30   +44.0u   ROI +75.8%
    favorites (-199..-1)        64-38    +7.0u   ROI  +6.8%
    heavy favs (-200 or worse)  21-6     +0.1u   ROI  +0.4%
    small dogs (+1..+149)       12-16    -2.2u   ROI  -7.8%

"Double Your Money" was the worst offender in the whole system: it explicitly
selected the SAFEST legs by model probability, which is the definition of
heavy chalk -- the exact bucket that returned +0.1 units on 27 bets. Laying
-235 to win 100 wins the pick 78% of the time and earns nothing, and two of
them stacked is how you turn a winning model into a break-even one. Safety and
value are not the same thing, and this file was optimising for the wrong one.

Both builders now refuse prices at or worse than config.ML_MAX_FAVORITE_PRICE
and rank by MODEL EDGE rather than raw win probability. Double Your Money
still targets ~2x, but assembles it from priced legs with a real edge instead
of the shortest numbers on the board.
"""

import config
from engine.models import ParlayRecommendation

GREEN_LIGHT_THRESHOLD = 0.35  # avg |signal| across celestial+numerology must clear this

MAX_FAV = getattr(config, "ML_MAX_FAVORITE_PRICE", -200)


def _is_heavy_favorite(odds):
    """True for prices at or worse than the heavy-chalk wall (e.g. -235)."""
    return odds is not None and odds <= MAX_FAV


def maybe_build_parlay(plays, celestial_signal, numerology_signal):
    if not config.PARLAY_ENABLED:
        return None
    eligible = [p for p in plays if not _is_heavy_favorite(p.odds_american)]
    if len(eligible) < config.PARLAY_MIN_LEGS:
        return None

    combined_energy = (abs(celestial_signal) + abs(numerology_signal)) / 2
    if combined_energy < GREEN_LIGHT_THRESHOLD:
        return None

    legs = eligible[: config.PARLAY_MAX_LEGS]
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
    """'Best Parlay of the Day'. Ranks legs by modelled EDGE, not by how short
    the price is, and excludes heavy chalk (which historically added win
    probability but no money). Returns {} with fewer than 2 qualifying legs."""
    max_legs = max_legs or config.PARLAY_MAX_LEGS
    candidates = []
    skipped_chalk = 0

    for p in plays:
        if _is_heavy_favorite(p.odds_american):
            skipped_chalk += 1
            continue
        # Confidence IS the edge -- how far the model beats the market price.
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
            "prob": h.get("model_prob") or _american_to_implied(odds),
            "confidence": conf,
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
    """'Double Your Money' -- 2 moneyline legs whose combined price lands near
    +100 (~2x a 1-unit stake).

    Chosen by EDGE, not by shortest price. The old version took the two
    highest-probability legs, which meant heavy favourites: a bucket that went
    21-6 and returned +0.1 units across 27 bets. Winning 78% of the time at
    -235 is not a profitable bet, and building the flagship ticket out of that
    bucket made the whole product look busy while earning nothing.

    Heavy chalk is excluded outright; among what remains we take the pair
    closest to 2.0 decimal, preferring higher combined edge on ties."""
    eligible = [p for p in plays if not _is_heavy_favorite(p.odds_american)]
    if len(eligible) < 2:
        return {}

    # Consider the strongest legs by edge, then find the pair nearest ~2x.
    top = sorted(eligible, key=lambda p: p.edge_pct, reverse=True)[:8]

    best_pair = None
    best_key = None
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            dec = (_american_to_decimal(top[i].odds_american)
                   * _american_to_decimal(top[j].odds_american))
            gap = abs(dec - 2.0)
            edge_sum = top[i].edge_pct + top[j].edge_pct
            # Nearest to 2x first; break ties toward the bigger combined edge.
            key = (round(gap, 3), -edge_sum)
            if best_key is None or key < best_key:
                best_key = key
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

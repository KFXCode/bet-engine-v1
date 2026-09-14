"""
engine/parlay.py
=================
Parlay builders:
  - maybe_build_parlay : the optional moon/numerology "green light" ML parlay.
  - build_daily_parlay : the always-on "Best Parlay" tab (per sport, and the
                         cross-sport TOP PARLAY).
  - build_double_parlay: 2 ML picks combining to roughly +100 (~2x).

PRICE POLICY (Aug 29, 2026) -- from the grade of 215 graded moneylines:
    big dogs (+150 or longer)   28-30   +44.0u   ROI +75.8%
    favorites (-199..-1)        64-38    +7.0u   ROI  +6.8%
    heavy favs (-200 or worse)  21-6     +0.1u   ROI  +0.4%
    small dogs (+1..+149)       12-16    -2.2u   ROI  -7.8%

Both builders refuse prices at or worse than config.ML_MAX_FAVORITE_PRICE and
rank by MODEL EDGE rather than raw win probability. Safety and value are not
the same thing.

CROSS-SPORT DIVERSITY (Sep 14, 2026) -- the TOP PARLAY was supposed to be the
best ticket across every active sport, and instead it came out all-MLB every
day. Not a crash, just arithmetic: legs were ranked purely by edge and MLB
runs 15 games a night against the NFL's 1-14 a WEEK, so MLB simply owned every
slot by volume. A "best of all sports" ticket that never leaves one league
isn't what it claims to be.

build_daily_parlay now takes max_per_sport. The per-sport tabs pass None (all
legs are that sport anyway); the cross-sport TOP PARLAY passes a cap so no
single league can take every slot. Within that constraint it still ranks by
edge -- legs are picked by strength, the cap only stops one sport monopolising
the ticket. If only one sport has games, the cap is irrelevant and the parlay
builds normally from that sport.
"""

import config
from engine.models import ParlayRecommendation

GREEN_LIGHT_THRESHOLD = 0.35  # avg |signal| across celestial+numerology must clear this

MAX_FAV = getattr(config, "ML_MAX_FAVORITE_PRICE", -200)

# Most legs any ONE sport may contribute to the cross-sport TOP PARLAY.
TOP_PARLAY_MAX_PER_SPORT = getattr(config, "TOP_PARLAY_MAX_PER_SPORT", 2)


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


def build_daily_parlay(plays, hr_props, max_legs=None, max_per_sport=None,
                        td_props=None, player_props=None):
    """Best Parlay. Ranks legs by modelled EDGE (not by how short the price
    is) and excludes heavy chalk.

    max_per_sport: cap on legs from any single sport. Pass a number for the
    cross-sport TOP PARLAY so MLB's nightly 15-game slate can't take every
    slot; pass None for a single-sport tab. Returns {} with fewer than 2
    qualifying legs.
    """
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
            "sport": getattr(p, "sport", "MLB"),
        })

    for h in (hr_props or []):
        odds = h.get("odds_american")
        if odds is None:
            continue
        score = h.get("score", 0)
        conf = min(1.0, max(0.0, (score - 60) / 40.0))
        candidates.append({
            "label": f"{h['player_name']} HR ({odds:+d})",
            "kind": "hr_prop", "odds": odds,
            "prob": h.get("model_prob") or _american_to_implied(odds),
            "confidence": conf, "sport": "MLB",
        })

    # NFL props are legitimate parlay legs too -- without them an NFL-only
    # Sunday could never build a ticket even with ten strong props on the board.
    for t in (td_props or []):
        odds = t.get("odds_american")
        if odds is None:
            continue
        prob = t.get("model_prob")
        if not prob:
            continue
        implied = _american_to_implied(odds)
        candidates.append({
            "label": f"{t['player_name']} anytime TD ({odds:+d})",
            "kind": "td_prop", "odds": odds, "prob": prob,
            "confidence": min(1.0, max(0.0, (prob - implied) * 10)),
            "sport": "NFL",
        })

    for pp in (player_props or []):
        odds = pp.get("odds_american")
        if odds is None:
            continue
        candidates.append({
            "label": f"{pp['player_name']} {pp['side'].title()} {pp['line']:g} "
                     f"{pp['market_label']} ({odds:+d})",
            "kind": "player_prop", "odds": odds,
            "prob": pp.get("model_prob") or _american_to_implied(odds),
            "confidence": min(1.0, max(0.0, pp.get("edge_pct", 0) * 10)),
            "sport": "NFL",
        })

    candidates.sort(key=lambda c: c["confidence"], reverse=True)

    if max_per_sport:
        legs = []
        per_sport = {}
        overflow = []
        for c in candidates:
            sport = c.get("sport") or "?"
            if per_sport.get(sport, 0) >= max_per_sport:
                overflow.append(c)
                continue
            per_sport[sport] = per_sport.get(sport, 0) + 1
            legs.append(c)
            if len(legs) >= max_legs:
                break
        # Only one sport has games today? The cap has nothing to diversify
        # across, so fall back to the plain best-edge board rather than
        # publishing a thinner ticket than the slate supports.
        if len(legs) < 2 and overflow:
            legs = candidates[:max_legs]
    else:
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
        "sports": sorted({leg.get("sport") for leg in legs if leg.get("sport")}),
    }


def build_double_parlay(plays):
    """'Double Your Money' -- 2 moneyline legs whose combined price lands near
    +100 (~2x a 1-unit stake).

    Chosen by EDGE, not by shortest price. The old version took the two
    highest-probability legs, which meant heavy favourites: a bucket that went
    21-6 and returned +0.1 units across 27 bets. Winning 78% of the time at
    -235 is not a profitable bet.

    Prefers legs from DIFFERENT sports when the slate allows, for the same
    reason the TOP PARLAY does -- two MLB games on the same night share
    weather, umpiring and league-wide scoring conditions, so they are less
    independent than an MLB leg paired with an NFL one."""
    eligible = [p for p in plays if not _is_heavy_favorite(p.odds_american)]
    if len(eligible) < 2:
        return {}

    top = sorted(eligible, key=lambda p: p.edge_pct, reverse=True)[:8]

    best_pair = None
    best_key = None
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            dec = (_american_to_decimal(top[i].odds_american)
                   * _american_to_decimal(top[j].odds_american))
            gap = abs(dec - 2.0)
            edge_sum = top[i].edge_pct + top[j].edge_pct
            same_sport = getattr(top[i], "sport", None) == getattr(top[j], "sport", None)
            # Nearest to 2x first, then prefer cross-sport, then bigger edge.
            key = (round(gap, 3), 1 if same_sport else 0, -edge_sum)
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

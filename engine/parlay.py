"""
engine/parlay.py
=================
Parlay builders:
  - maybe_build_parlay : the optional moon/numerology "green light" ML parlay.
  - build_daily_parlay : the always-on "Best Parlay" tab (per sport, and the
                         cross-sport TOP PARLAY).
  - build_double_parlay: 2 moneyline picks combining to roughly +100 (~2x).

PRICE POLICY: every builder refuses prices at or worse than
config.ML_MAX_FAVORITE_PRICE and ranks by MODEL EDGE, not raw win probability.

CROSS-SPORT DIVERSITY: the TOP PARLAY passes max_per_sport so MLB's nightly
15-game slate can't take every slot, and both NFL prop boards are eligible
legs. If only one sport has eligible legs, it builds from that sport.

=====================================================================
DOUBLE YOUR MONEY IS BUILT FROM THE TOP PARLAY (Sep 30, 2026)
=====================================================================
The two tickets used to be picked by two different methods: the Top Parlay
ranked legs by edge, while the Double searched the top eight plays for
whichever PAIR happened to multiply closest to +100. The pair nearest 2x was
usually not the two strongest picks, so the tickets disagreed most days --
two "best" tickets built from different picks, which reads as the engine
contradicting itself.

Now the Double comes FROM the Top Parlay. Of the Top Parlay's moneyline legs,
it takes the pair that combines closest to 2x (ties go to the stronger pair).
So the Double is always a subset of the Top Parlay, and both tickets express
the same read.

Fallback: if the Top Parlay has fewer than two moneyline legs (an NFL-prop-
heavy Sunday, say), the Double keeps whatever ML leg the Top Parlay does have
and fills from the strongest remaining plays, so it stays anchored to the same
picks where it can.

How it finds the Top Parlay: build_daily_parlay remembers its most recent
CROSS-SPORT result (the call with max_per_sport set) in _LAST_TOP_PARLAY.
run_daily builds the Top Parlay before the Double, so the Double always reads
today's ticket. The per-sport tabs don't pass max_per_sport, so they never
overwrite it.
"""

import config
from engine.models import ParlayRecommendation

GREEN_LIGHT_THRESHOLD = 0.35  # avg |signal| across celestial+numerology must clear this

MAX_FAV = getattr(config, "ML_MAX_FAVORITE_PRICE", -200)

# Most legs any ONE sport may contribute to the cross-sport TOP PARLAY.
TOP_PARLAY_MAX_PER_SPORT = getattr(config, "TOP_PARLAY_MAX_PER_SPORT", 2)

# Most recent cross-sport Top Parlay, read by build_double_parlay.
_LAST_TOP_PARLAY = {}


def _is_heavy_favorite(odds):
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

    reasoning = (f"Moon/numerology green light today (combined energy {combined_energy:.2f} >= "
                 f"{GREEN_LIGHT_THRESHOLD}) -- every leg already clears the board on its own; "
                 f"this parlay is a bonus, not a substitute for the straight plays.")

    return ParlayRecommendation(
        legs=legs, combined_odds_american=_decimal_to_american(combined_decimal_odds),
        combined_prob=combined_prob, stake_units=config.FLAT_STAKE_UNITS,
        reasoning=reasoning,
    )


def build_daily_parlay(plays, hr_props, max_legs=None, max_per_sport=None,
                        td_props=None, player_props=None):
    """Best Parlay. Ranks legs by modelled EDGE and excludes heavy chalk.

    max_per_sport: pass a number for the cross-sport TOP PARLAY; pass None for
    a single-sport tab. Returns {} with fewer than 2 qualifying legs."""
    global _LAST_TOP_PARLAY

    max_legs = max_legs or config.PARLAY_MAX_LEGS
    candidates = []

    for p in plays:
        if _is_heavy_favorite(p.odds_american):
            continue
        candidates.append({
            "label": f"{p.team} ML ({p.odds_american:+d})",
            "kind": "moneyline", "odds": p.odds_american,
            "prob": p.model_prob,
            "confidence": min(1.0, max(0.0, p.edge_pct * 10)),
            "sport": getattr(p, "sport", "MLB"),
        })

    for h in (hr_props or []):
        odds = h.get("odds_american")
        if odds is None:
            continue
        candidates.append({
            "label": f"{h['player_name']} HR ({odds:+d})",
            "kind": "hr_prop", "odds": odds,
            "prob": h.get("model_prob") or _american_to_implied(odds),
            "confidence": min(1.0, max(0.0, (h.get("score", 0) - 60) / 40.0)),
            "sport": "MLB",
        })

    for t in (td_props or []):
        odds = t.get("odds_american")
        prob = t.get("model_prob")
        if odds is None or not prob:
            continue
        candidates.append({
            "label": f"{t['player_name']} anytime TD ({odds:+d})",
            "kind": "td_prop", "odds": odds, "prob": prob,
            "confidence": min(1.0, max(0.0, (prob - _american_to_implied(odds)) * 10)),
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
        legs, per_sport, overflow = [], {}, []
        for c in candidates:
            sport = c.get("sport") or "?"
            if per_sport.get(sport, 0) >= max_per_sport:
                overflow.append(c)
                continue
            per_sport[sport] = per_sport.get(sport, 0) + 1
            legs.append(c)
            if len(legs) >= max_legs:
                break
        if len(legs) < 2 and overflow:
            legs = candidates[:max_legs]
    else:
        legs = candidates[:max_legs]

    if len(legs) < 2:
        if max_per_sport:
            _LAST_TOP_PARLAY = {}
        return {}

    combined_prob = 1.0
    combined_decimal = 1.0
    for leg in legs:
        combined_prob *= leg["prob"]
        combined_decimal *= _american_to_decimal(leg["odds"])

    result = {
        "legs": legs,
        "combined_odds_american": _decimal_to_american(combined_decimal),
        "combined_prob": combined_prob,
        "leg_count": len(legs),
        "sports": sorted({leg.get("sport") for leg in legs if leg.get("sport")}),
    }
    if max_per_sport:
        _LAST_TOP_PARLAY = result
    return result


def _pair_closest_to_double(legs):
    """From a list of leg dicts (label/odds/prob/confidence/sport), the pair
    whose combined price is nearest 2x; ties go to the stronger pair."""
    best, best_key = None, None
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            dec = _american_to_decimal(legs[i]["odds"]) * _american_to_decimal(legs[j]["odds"])
            key = (round(abs(dec - 2.0), 3), -(legs[i]["confidence"] + legs[j]["confidence"]))
            if best_key is None or key < best_key:
                best_key, best = key, (legs[i], legs[j])
    return best


def build_double_parlay(plays):
    """'Double Your Money': 2 moneyline legs combining to roughly +100,
    drawn FROM today's Top Parlay so the two tickets always agree."""
    top_ml = [leg for leg in (_LAST_TOP_PARLAY.get("legs") or [])
              if leg.get("kind") == "moneyline"]

    if len(top_ml) >= 2:
        pair = _pair_closest_to_double(top_ml)
    else:
        # Keep any Top Parlay ML leg, fill from the strongest remaining plays.
        anchored = list(top_ml)
        taken = {leg["label"] for leg in anchored}
        pool = sorted((p for p in plays if not _is_heavy_favorite(p.odds_american)),
                      key=lambda p: p.edge_pct, reverse=True)
        extra = []
        for p in pool:
            label = f"{p.team} ML ({p.odds_american:+d})"
            if label in taken:
                continue
            extra.append({"label": label, "kind": "moneyline", "odds": p.odds_american,
                          "prob": p.model_prob,
                          "confidence": min(1.0, max(0.0, p.edge_pct * 10)),
                          "sport": p.sport})
            if len(extra) >= 7:
                break
        if anchored:
            best, best_key = None, None
            for leg in extra:
                dec = _american_to_decimal(anchored[0]["odds"]) * _american_to_decimal(leg["odds"])
                key = (round(abs(dec - 2.0), 3), -leg["confidence"])
                if best_key is None or key < best_key:
                    best_key, best = key, (anchored[0], leg)
            pair = best
        else:
            pair = _pair_closest_to_double(extra) if len(extra) >= 2 else None

    if not pair:
        return {}

    combined_prob = 1.0
    combined_decimal = 1.0
    for leg in pair:
        combined_prob *= leg["prob"]
        combined_decimal *= _american_to_decimal(leg["odds"])

    return {
        "legs": [{"label": leg["label"], "sport": leg["sport"], "kind": "moneyline"} for leg in pair],
        "combined_odds_american": _decimal_to_american(combined_decimal),
        "combined_prob": combined_prob,
        "leg_count": 2,
        "from_top_parlay": len(top_ml) >= 2,
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

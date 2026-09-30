"""
engine/parlay.py
=================
Parlay builders:
  - maybe_build_parlay : the optional moon/numerology "green light" ML parlay.
  - build_daily_parlay : the always-on "Best Parlay" tab (per sport, and the
                         cross-sport TOP PARLAY).
  - build_double_parlay: "Double Your Money" -- the day's two strongest
                         moneylines, across sports.

PRICE POLICY: every builder refuses prices at or worse than
config.ML_MAX_FAVORITE_PRICE and ranks by MODEL EDGE, not raw win probability.

=====================================================================
TOP PARLAY: EVERY ACTIVE SPORT GETS A SEAT (Sep 30, 2026)
=====================================================================
The Top Parlay is meant to be the best ticket across the whole day. Ranking by
edge with only a per-sport cap still let the two strongest sports take all
four slots -- on an MLB + NFL + NHL day, NHL could be shut out even with a
good pick. It now builds in two passes:
  1. SEAT EVERY SPORT: the single strongest leg from each sport that has an
     eligible leg (if there are more sports than slots, the sports with the
     strongest best-leg win the seats).
  2. FILL THE REST by edge, still capped at max_per_sport per sport.

=====================================================================
DOUBLE YOUR MONEY: THE TWO BEST PICKS, ACROSS SPORTS (Sep 30, 2026)
=====================================================================
Simply the day's two strongest moneylines, and from two DIFFERENT sports
whenever more than one sport has an eligible pick -- e.g. a Yankees ML with a
Browns ML. It is not tied to the Top Parlay and not searched for a price near
+100; it's the two best reads on the board. Different sports are preferred
because two same-sport games on one night share conditions, so a cross-sport
pair is the more independent two-leg ticket. Only on a single-sport day are
both legs from the same sport.
"""

import config
from engine.models import ParlayRecommendation

GREEN_LIGHT_THRESHOLD = 0.35  # avg |signal| across celestial+numerology must clear this

MAX_FAV = getattr(config, "ML_MAX_FAVORITE_PRICE", -200)

# Most legs any ONE sport may contribute to the cross-sport TOP PARLAY.
TOP_PARLAY_MAX_PER_SPORT = getattr(config, "TOP_PARLAY_MAX_PER_SPORT", 2)


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


def _candidates(plays, hr_props=None, td_props=None, player_props=None):
    """Every eligible leg as a dict, strongest first."""
    out = []
    for p in plays:
        if _is_heavy_favorite(p.odds_american):
            continue
        out.append({
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
        out.append({
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
        out.append({
            "label": f"{t['player_name']} anytime TD ({odds:+d})",
            "kind": "td_prop", "odds": odds, "prob": prob,
            "confidence": min(1.0, max(0.0, (prob - _american_to_implied(odds)) * 10)),
            "sport": "NFL",
        })
    for pp in (player_props or []):
        odds = pp.get("odds_american")
        if odds is None:
            continue
        out.append({
            "label": f"{pp['player_name']} {pp['side'].title()} {pp['line']:g} "
                     f"{pp['market_label']} ({odds:+d})",
            "kind": "player_prop", "odds": odds,
            "prob": pp.get("model_prob") or _american_to_implied(odds),
            "confidence": min(1.0, max(0.0, pp.get("edge_pct", 0) * 10)),
            "sport": "NFL",
        })
    out.sort(key=lambda c: c["confidence"], reverse=True)
    return out


def _seat_every_sport(candidates, max_legs, max_per_sport):
    """Pass 1: best leg from each sport. Pass 2: fill by edge, capped."""
    best_by_sport = {}
    for c in candidates:                     # already strongest-first
        best_by_sport.setdefault(c.get("sport") or "?", c)
    seats = sorted(best_by_sport.values(), key=lambda c: c["confidence"], reverse=True)[:max_legs]

    legs = list(seats)
    per_sport = {}
    for c in legs:
        s = c.get("sport") or "?"
        per_sport[s] = per_sport.get(s, 0) + 1
    chosen = {id(c) for c in legs}

    for c in candidates:
        if len(legs) >= max_legs:
            break
        if id(c) in chosen:
            continue
        s = c.get("sport") or "?"
        if per_sport.get(s, 0) >= max_per_sport:
            continue
        per_sport[s] = per_sport.get(s, 0) + 1
        legs.append(c)
        chosen.add(id(c))

    # Keep the ticket ordered strongest-first for display.
    legs.sort(key=lambda c: c["confidence"], reverse=True)
    return legs


def build_daily_parlay(plays, hr_props, max_legs=None, max_per_sport=None,
                        td_props=None, player_props=None):
    """Best Parlay. max_per_sport set = the cross-sport TOP PARLAY, which
    seats every active sport first. None = a single-sport tab.
    Returns {} with fewer than 2 qualifying legs."""
    max_legs = max_legs or config.PARLAY_MAX_LEGS
    candidates = _candidates(plays, hr_props, td_props, player_props)

    if max_per_sport:
        legs = _seat_every_sport(candidates, max_legs, max_per_sport)
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
    """The day's two strongest moneylines, from two different sports whenever
    more than one sport has an eligible pick."""
    ranked = sorted((p for p in plays if not _is_heavy_favorite(p.odds_american)),
                    key=lambda p: p.edge_pct, reverse=True)
    if len(ranked) < 2:
        return {}

    first = ranked[0]
    second = next((p for p in ranked[1:] if p.sport != first.sport), None)
    cross_sport = second is not None
    if second is None:
        second = ranked[1]

    combined_prob = 1.0
    combined_decimal = 1.0
    legs = []
    for p in (first, second):
        combined_prob *= p.model_prob
        combined_decimal *= _american_to_decimal(p.odds_american)
        legs.append({"label": f"{p.team} ML ({p.odds_american:+d})",
                     "sport": p.sport, "kind": "moneyline"})

    return {
        "legs": legs,
        "combined_odds_american": _decimal_to_american(combined_decimal),
        "combined_prob": combined_prob,
        "leg_count": 2,
        "cross_sport": cross_sport,
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

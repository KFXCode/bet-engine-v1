"""
engine/grading_factors.py
==========================
One function per grading factor. Each takes a GradingContext and returns an
engine.models.FactorScore (signal -1..+1, + leans HOME).

Reasoning strings name the actual TEAMS (home/away abbreviations) instead of
the words "home"/"away", so a card is instantly readable.

celestial_signal / numerology_signal arrive already converted to home/away
convention by engine/scoring.py.

FADE-PHASE MOON (Aug 21, 2026): on Waxing Gibbous / Full Moon / Waning Gibbous
-- the "public confidence high, overvalued favorites become fade material"
nights -- score_moon_zodiac DOUBLES its weight (0.03 -> 0.06), putting lunar
energy on par with the public/sharp money factor. On every other phase it
stays at its normal, deliberately-small nudge weight.
"""

from dataclasses import dataclass

import config
from engine.models import FactorScore

# Phases where the system's rule is "fade tired favorites & hyped teams".
FADE_PHASES = {"Waxing Gibbous", "Full Moon", "Waning Gibbous"}
FADE_PHASE_WEIGHT_MULTIPLIER = 2.0


@dataclass
class GradingContext:
    game: object
    home_pitcher_profile: object
    away_pitcher_profile: object
    home_offense: object
    away_offense: object
    home_record: dict
    away_record: dict
    public_split: object
    situational: dict
    celestial_signal: float
    celestial_reasoning: str
    numerology_signal: float
    numerology_reasoning: str
    home_ml: int = None
    away_ml: int = None
    moon_phase: str = None
    home_is_favorite: bool = None


def _clip(x):
    return max(-1.0, min(1.0, x))


def _g(obj, attr):
    return getattr(obj, attr, None) if obj else None


def _teams(ctx):
    """(home_abbr, away_abbr) with safe fallbacks."""
    g = ctx.game
    return (getattr(g, "home_team", "HOME") or "HOME",
            getattr(g, "away_team", "AWAY") or "AWAY")


def score_talent_gap(ctx):
    h, a = _teams(ctx)
    hr = ctx.home_record or {}
    ar = ctx.away_record or {}
    home_pyth = _pyth_win_pct(hr.get("runs_scored"), hr.get("runs_allowed"))
    away_pyth = _pyth_win_pct(ar.get("runs_scored"), ar.get("runs_allowed"))
    if home_pyth is None or away_pyth is None:
        return FactorScore("talent_gap", "Talent gap / better team", 0.0,
                            config.FACTOR_WEIGHTS["talent_gap"],
                            "Not enough season data yet to separate these teams on talent.",
                            "degraded")
    diff = home_pyth - away_pyth
    signal = _clip(diff * 4)
    deeper = h if diff > 0 else a
    reasoning = (f"Pythagorean win%: {h} {home_pyth:.3f} vs {a} {away_pyth:.3f} "
                 f"({deeper} the deeper team)")
    return FactorScore("talent_gap", "Talent gap / better team", signal,
                        config.FACTOR_WEIGHTS["talent_gap"], reasoning, "ok")


def score_matchup_pitching(ctx):
    h, a = _teams(ctx)
    hp, ap = ctx.home_pitcher_profile, ctx.away_pitcher_profile
    if not hp or not ap or hp.era is None or ap.era is None:
        return FactorScore("matchup_pitching", "Matchup advantage (pitching)", 0.0,
                            config.FACTOR_WEIGHTS["matchup_pitching"],
                            "Probable starters' ERA unavailable -- neutral until confirmed.",
                            "degraded")
    era_gap = ap.era - hp.era
    signal = _clip(era_gap / 2.0)
    better = h if era_gap > 0 else a
    home_name = ctx.game.home_pitcher.name if ctx.game.home_pitcher else "TBD"
    away_name = ctx.game.away_pitcher.name if ctx.game.away_pitcher else "TBD"
    fip_note = f" (FIP: {hp.fip:.2f} vs {ap.fip:.2f})" if hp.fip is not None and ap.fip is not None else ""
    reasoning = f"Starter ERA: {h} {home_name} {hp.era:.2f} vs {a} {away_name} {ap.era:.2f}{fip_note} -> edge to {better}'s starter"
    return FactorScore("matchup_pitching", "Matchup advantage (pitching)", signal,
                        config.FACTOR_WEIGHTS["matchup_pitching"], reasoning, "ok")


def score_advanced_analytics(ctx):
    """Advanced pitching/hitting edge, anchored on RELIABLE MLB Stats API data
    (team OPS, pitcher HR/9 allowed, pitcher K%). Barrel% / hard-hit% are a
    bonus when they load. Averages every available sub-signal."""
    h, a = _teams(ctx)
    hp, ap = ctx.home_pitcher_profile, ctx.away_pitcher_profile
    ho, ao = ctx.home_offense, ctx.away_offense
    parts = []
    subs = []

    ho_ops, ao_ops = _g(ho, "ops"), _g(ao, "ops")
    if ho_ops is not None and ao_ops is not None:
        subs.append(_clip((ho_ops - ao_ops) / 0.060))
        parts.append(f"team OPS: {h} {ho_ops:.3f} vs {a} {ao_ops:.3f}")

    if hp and ap and hp.hr_per_9 is not None and ap.hr_per_9 is not None:
        subs.append(_clip((ap.hr_per_9 - hp.hr_per_9) / 0.8))
        parts.append(f"HR/9 allowed: {h} SP {hp.hr_per_9:.2f} vs {a} SP {ap.hr_per_9:.2f}")

    if hp and ap and hp.k_pct is not None and ap.k_pct is not None:
        subs.append(_clip((hp.k_pct - ap.k_pct) / 8.0))
        parts.append(f"K%: {h} SP {hp.k_pct:.1f} vs {a} SP {ap.k_pct:.1f}")

    if hp and ap and hp.hard_hit_pct_allowed is not None and ap.hard_hit_pct_allowed is not None:
        subs.append(_clip((ap.hard_hit_pct_allowed - hp.hard_hit_pct_allowed) / 10.0))
        parts.append(f"hard-hit% allowed: {h} SP {hp.hard_hit_pct_allowed:.1f} vs {a} SP {ap.hard_hit_pct_allowed:.1f}")

    if not subs:
        return FactorScore("advanced_analytics", "Advanced analytics (OPS/HR9/K%/barrel)", 0.0,
                            config.FACTOR_WEIGHTS["advanced_analytics"],
                            "No advanced data available today (team OPS + pitcher stats both missing) -- neutral.",
                            "degraded")
    signal = _clip(sum(subs) / len(subs))
    return FactorScore("advanced_analytics", "Advanced analytics (OPS/HR9/K%/barrel)", signal,
                        config.FACTOR_WEIGHTS["advanced_analytics"], "; ".join(parts), "ok")


def score_underdog_value(ctx):
    """Research-backed dog edge -- leans TOWARD an underdog the market shades against."""
    h, a = _teams(ctx)
    w = config.FACTOR_WEIGHTS["underdog_value"]
    home_ml, away_ml = ctx.home_ml, ctx.away_ml

    dog_side = None
    dog_ml = None
    if home_ml is not None and away_ml is not None:
        if home_ml > 0 and away_ml < 0:
            dog_side, dog_ml = "home", home_ml
        elif away_ml > 0 and home_ml < 0:
            dog_side, dog_ml = "away", away_ml
        elif home_ml > away_ml:
            dog_side, dog_ml = "home", home_ml
        else:
            dog_side, dog_ml = "away", away_ml
    else:
        hr = ctx.home_record or {}
        ar = ctx.away_record or {}
        hp = _pyth_win_pct(hr.get("runs_scored"), hr.get("runs_allowed"))
        ap = _pyth_win_pct(ar.get("runs_scored"), ar.get("runs_allowed"))
        if hp is not None and ap is not None:
            dog_side = "home" if hp < ap else "away"

    if dog_side is None:
        return FactorScore("underdog_value", "Underdog value (home-dog / fade shaded favorite)", 0.0,
                            w, "No odds or records yet to identify a live underdog -- neutral.", "degraded")

    dog_team = h if dog_side == "home" else a
    dir_to_home = 1.0 if dog_side == "home" else -1.0
    strength = 0.0
    notes = []

    if dog_side == "home" and dog_ml is not None and dog_ml >= 120:
        strength += 0.45
        notes.append(f"{dog_team} is a home dog at {dog_ml:+d} (+120-or-longer home dogs are historically underpriced)")
    elif dog_side == "home" and dog_ml is not None and dog_ml >= 100:
        strength += 0.25
        notes.append(f"{dog_team} is a home dog at {dog_ml:+d}")

    split = ctx.public_split
    if split and split.data_quality not in ("mock", "missing"):
        fav_tickets = (100 - split.tickets_pct_home) if dog_side == "home" else split.tickets_pct_home
        if fav_tickets >= 65:
            strength += 0.40
            notes.append(f"public {fav_tickets:.0f}% on the favorite -- price is shaded, {dog_team} gains value")
        elif fav_tickets >= 58:
            strength += 0.20
            notes.append(f"public leaning {fav_tickets:.0f}% to the favorite")

    hr = ctx.home_record or {}
    ar = ctx.away_record or {}
    hp = _pyth_win_pct(hr.get("runs_scored"), hr.get("runs_allowed"))
    ap = _pyth_win_pct(ar.get("runs_scored"), ar.get("runs_allowed"))
    if hp is not None and ap is not None:
        dog_pyth = hp if dog_side == "home" else ap
        fav_pyth = ap if dog_side == "home" else hp
        if dog_pyth >= fav_pyth - 0.03:
            strength += 0.30
            notes.append(f"{dog_team}'s run differential ({dog_pyth:.3f}) nearly matches the favorite ({fav_pyth:.3f})")

    if strength <= 0:
        return FactorScore("underdog_value", "Underdog value (home-dog / fade shaded favorite)", 0.0,
                            w, "No live underdog value today -- no home-dog price, public fade, or competitive-dog signal.", "ok")

    signal = _clip(dir_to_home * strength)
    reasoning = f"Underdog value on {dog_team}: " + "; ".join(notes)
    return FactorScore("underdog_value", "Underdog value (home-dog / fade shaded favorite)", signal,
                        w, reasoning, "ok")


def score_bullpen_fatigue(ctx):
    h, a = _teams(ctx)
    w = config.FACTOR_WEIGHTS["bullpen_fatigue"]
    sit = ctx.situational or {}
    hb = sit.get("home_bullpen") or {}
    ab = sit.get("away_bullpen") or {}
    hf = hb.get("fatigue")
    af = ab.get("fatigue")
    if hf is None or af is None:
        return FactorScore("bullpen_fatigue", "Bullpen fatigue (rested vs overworked pen)", 0.0,
                            w, "Recent bullpen workload unavailable -- neutral.", "degraded")
    signal = _clip((af - hf) * 1.5)
    fresher = h if hf < af else a if af < hf else "even"
    reasoning = (f"Bullpen load (last 3d): {h} {hb.get('games','?')}g/"
                 f"{hb.get('extra_innings',0)} extra-inn (fatigue {hf:.2f}) "
                 f"vs {a} {ab.get('games','?')}g/{ab.get('extra_innings',0)} extra-inn (fatigue {af:.2f}) "
                 f"-> fresher pen: {fresher}")
    return FactorScore("bullpen_fatigue", "Bullpen fatigue (rested vs overworked pen)", signal,
                        w, reasoning, "ok")


def score_motivation(ctx):
    h, a = _teams(ctx)
    hr = ctx.home_record or {}
    ar = ctx.away_record or {}
    signal = 0.0
    notes = []
    h_gb, a_gb = hr.get("games_back"), ar.get("games_back")
    if h_gb is not None and a_gb is not None:
        motivation_gap = (a_gb - h_gb) / 10.0
        signal += _clip(motivation_gap) * 0.6
        notes.append(f"games back: {h} {h_gb} vs {a} {a_gb}")
    h_streak, a_streak = hr.get("streak") or 0, ar.get("streak") or 0
    signal += _clip((h_streak - a_streak) / 5.0) * 0.4
    notes.append(f"streak: {h} {h_streak:+d} vs {a} {a_streak:+d}")
    signal = _clip(signal)
    quality = "ok" if h_gb is not None else "degraded"
    return FactorScore("motivation", "Motivation (playoffs/revenge/tanking/streak)", signal,
                        config.FACTOR_WEIGHTS["motivation"], "; ".join(notes), quality)


def score_public_sharp_split(ctx):
    h, a = _teams(ctx)
    split = ctx.public_split
    if not split:
        return FactorScore("public_sharp_split", "Public vs. sharp money", 0.0,
                            config.FACTOR_WEIGHTS["public_sharp_split"],
                            "No public betting data available today.", "degraded")
    tickets_home = split.tickets_pct_home
    handle_home = split.handle_pct_home
    sharp_gap = (handle_home - tickets_home) / 100.0
    signal = _clip(sharp_gap * 3.0)
    lean = f"sharp money on {h}" if sharp_gap > 0 else f"sharp money on {a}" if sharp_gap < 0 else "no split"
    reasoning = f"Tickets {tickets_home:.0f}% {h} / Handle {handle_home:.0f}% {h} -> {lean}"
    if split.data_quality in ("mock", "missing"):
        reasoning += "  [SIMULATED/PLACEHOLDER DATA -- fill in manual_inputs/public_betting_*.json]"
    elif split.data_quality == "partial":
        reasoning += "  [Bet% only -- Handle% wasn't found/is paywalled on the source page, so no real sharp-money divergence read today]"
    return FactorScore("public_sharp_split", "Public vs. sharp money", signal,
                        config.FACTOR_WEIGHTS["public_sharp_split"], reasoning, split.data_quality)


def score_situational(ctx):
    h, a = _teams(ctx)
    sit = ctx.situational or {}
    signal = 0.0
    notes = [f"park run factor {sit.get('park_runs_factor', 100)}"]
    home_injuries = sit.get("home_injuries", [])
    away_injuries = sit.get("away_injuries", [])
    signal += _clip(_injury_score(away_injuries) - _injury_score(home_injuries))
    if home_injuries or away_injuries:
        notes.append(f"injuries: {h} {len(home_injuries)}, {a} {len(away_injuries)}")
    home_rest = (sit.get("home_rest") or {}).get("rest_days")
    away_rest = (sit.get("away_rest") or {}).get("rest_days")
    if home_rest is not None and away_rest is not None:
        signal += _clip((home_rest - away_rest) / 3.0) * 0.3
        notes.append(f"rest: {h} {home_rest}d, {a} {away_rest}d")
    signal = _clip(signal)
    return FactorScore("situational", "Situational (injuries/rest/park)", signal,
                        config.FACTOR_WEIGHTS["situational"], "; ".join(notes), "ok")


def _injury_score(injuries):
    weight = {"high": 1.0, "medium": 0.5, "low": 0.2}
    return sum(weight.get(i.get("impact", "low"), 0.2) for i in injuries)


def score_moon_zodiac(ctx):
    """Lunar energy. On FADE PHASES (Waxing Gibbous / Full / Waning Gibbous) --
    the nights where public confidence runs high and overvalued favorites
    become fade material -- this factor's weight DOUBLES so the fade actually
    has teeth against a heavy chalk favorite instead of being outvoted."""
    weight = config.FACTOR_WEIGHTS["moon_zodiac"]
    reasoning = ctx.celestial_reasoning
    phase = ctx.moon_phase
    if phase in FADE_PHASES:
        weight = round(weight * FADE_PHASE_WEIGHT_MULTIPLIER, 4)
        reasoning = (f"{reasoning}  [FADE PHASE: {phase} -- lunar weight doubled to {weight:.3f} "
                     f"(same as the public/sharp factor). Public confidence runs high tonight, so "
                     f"hyped/tired favorites get pushed down and the value side gets pushed up.]")
    return FactorScore("moon_zodiac", "Moon phase + zodiac energy", ctx.celestial_signal,
                        weight, reasoning, "ok")


def score_numerology(ctx):
    return FactorScore("numerology", "Numerology of the date", ctx.numerology_signal,
                        config.FACTOR_WEIGHTS["numerology"], ctx.numerology_reasoning, "ok")


ALL_FACTOR_SCORERS = [
    score_talent_gap,
    score_matchup_pitching,
    score_advanced_analytics,
    score_underdog_value,
    score_bullpen_fatigue,
    score_motivation,
    score_public_sharp_split,
    score_situational,
    score_moon_zodiac,
    score_numerology,
]


def score_all_factors(ctx):
    return [scorer(ctx) for scorer in ALL_FACTOR_SCORERS]


def _pyth_win_pct(runs_scored, runs_allowed, exponent=1.83):
    if not runs_scored or not runs_allowed:
        return None
    try:
        rs, ra = float(runs_scored), float(runs_allowed)
        if rs <= 0 or ra <= 0:
            return None
        return rs ** exponent / (rs ** exponent + ra ** exponent)
    except (TypeError, ValueError, ZeroDivisionError):
        return None

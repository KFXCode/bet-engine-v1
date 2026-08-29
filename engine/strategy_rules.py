"""
engine/strategy_rules.py
=========================
Non-negotiable rules layered on top of raw edge numbers:
  - never below the sport's edge floor (config.min_edge_for(sport))
  - PRICE POLICY: no heavy chalk, higher bar on small dogs (see below)
  - flat 1-unit sizing
  - up to MAX_PLAYS_PER_DAY plays PER SPORT
  - team diversification (no same team 3+ days without stricter re-confirm)
  - line movement (only drop on significant adverse move AND heavy money)
  - doubleheader safety (one game per pairing; every label names Gm 1/Gm 2)

PRICE POLICY (Aug 29, 2026), straight from 215 graded moneylines:
    big dogs (+150 or longer)   28-30   +44.0u   ROI +75.8%
    favorites (-199..-1)        64-38    +7.0u   ROI  +6.8%
    heavy favs (-200 or worse)  21-6     +0.1u   ROI  +0.4%
    small dogs (+1..+149)       12-16    -2.2u   ROI  -7.8%

Heavy favourites won 78% of the time and returned +0.1 units across 27 bets --
laying -235 to make 100 is break-even at best, and every slot spent there is a
slot NOT spent on the one bucket that actually prints. A high win rate that
earns nothing is the most seductive way to lose: the board looks great and the
bankroll doesn't move. Small dogs lost outright, so they now need a bigger
modelled edge to qualify. Both rules are enforced here, before a pick can ever
reach the report.

The reasoning each pick carries is SPLIT so the card is honest:
  1. an EDGE SOURCE line -- data factors vs astrology/numerology,
  2. supporting factors, 3. neutral context, 4. counter-signals.
"""

import config
from engine.models import Recommendation, FadeTeam

ASTRO_KEYS = {"moon_zodiac", "numerology"}

MAX_FAV = getattr(config, "ML_MAX_FAVORITE_PRICE", -200)
SMALL_DOG_MIN_EDGE = getattr(config, "ML_SMALL_DOG_MIN_EDGE", 0.045)
BIG_DOG_MIN_ODDS = getattr(config, "ML_BIG_DOG_MIN_ODDS", 150)


def _price_check(odds, edge_pct):
    """(ok, note). Applies the graded price policy to one candidate."""
    if odds is None:
        return True, None
    if odds <= MAX_FAV:
        return False, (f"priced {odds:+d} -- heavy favourites ({MAX_FAV:+d} or worse) have gone "
                       f"21-6 for +0.1 units all season (ROI +0.4%). A 78% win rate that earns "
                       f"nothing isn't a bet, so this bucket is off the board.")
    if 0 <= odds < BIG_DOG_MIN_ODDS and edge_pct < SMALL_DOG_MIN_EDGE:
        return False, (f"priced {odds:+d} with only a {edge_pct:.1%} edge -- small dogs "
                       f"(+1 to +{BIG_DOG_MIN_ODDS - 1}) are 12-16 for -2.2 units, so they need "
                       f"at least a {SMALL_DOG_MIN_EDGE:.1%} edge to qualify.")
    return True, None


def _build_reasoning(ev, dh_note=None):
    """Returns (reasoning_list, edge_data_pct, edge_astro_pct)."""
    side = ev.recommended_side
    support, context, counter = [], [], []
    edge_data = 0.0
    edge_astro = 0.0
    for fs in ev.factor_scores:
        toward = fs.signal if side == "home" else -fs.signal
        contribution = toward * fs.weight
        if fs.key in ASTRO_KEYS:
            edge_astro += contribution
        else:
            edge_data += contribution
        if toward > 0.02:
            support.append(fs.reasoning)
        elif toward < -0.02:
            counter.append(fs.reasoning)
        else:
            context.append(fs.reasoning)

    edge_data_pct = edge_data * 100
    edge_astro_pct = edge_astro * 100

    reasoning = []
    if dh_note:
        reasoning.append(dh_note)
    reasoning.append(
        f"EDGE SOURCE: data factors {edge_data_pct:+.1f}%, astrology/numerology "
        f"{edge_astro_pct:+.1f}% (of the {ev.edge_pct * 100:.1f}% total edge). "
        f"A healthy pick is driven mostly by DATA -- if astro is carrying it, treat it as thin."
    )
    reasoning += support
    reasoning += context
    if counter:
        reasoning.append("— Counter-signals we weighed (leaned toward the other side but didn't outweigh the pick):")
        reasoning += counter
    return reasoning, edge_data_pct, edge_astro_pct


def select_daily_plays(evaluations, db, public_splits, run_date_str):
    candidates = [e for e in evaluations
                  if e.recommended_side and e.edge_pct >= config.min_edge_for(e.game.sport)]
    candidates.sort(key=_edge_rank_key)

    recent_picks = {p["team"] for p in db.get_recent_team_picks(run_date_str, config.DIVERSIFICATION_LOOKBACK_DAYS)}
    picked_today = {}
    per_sport_count = {}

    plays = []
    dropped_notes = []
    for ev in candidates:
        sport = ev.game.sport
        if per_sport_count.get(sport, 0) >= config.MAX_PLAYS_PER_DAY:
            continue

        team = ev.game.home_team if ev.recommended_side == "home" else ev.game.away_team
        label = team + ev.game.dh_label()
        matchup = f"{ev.game.away_team} @ {ev.game.home_team}{ev.game.dh_label()}"
        odds_american = ev.odds.home_ml if ev.recommended_side == "home" else ev.odds.away_ml

        price_ok, price_note = _price_check(odds_american, ev.edge_pct)
        if not price_ok:
            dropped_notes.append(f"{label} ({matchup}): {price_note}")
            continue

        if team in picked_today:
            dropped_notes.append(
                f"{label} ({matchup}): already locked in today's stronger play on {team} from the "
                f"{picked_today[team]} game -- not doubling up on the same team twice in one day."
            )
            continue

        diversification_flag = None
        if team in recent_picks:
            strong_factors = sum(1 for fs in ev.factor_scores if abs(fs.signal) >= 0.5)
            required_edge = config.min_edge_for(sport) + config.DIVERSIFICATION_EXTRA_EDGE
            if ev.edge_pct < required_edge or strong_factors < config.DIVERSIFICATION_MIN_STRONG_FACTORS:
                dropped_notes.append(
                    f"{label} ({matchup}): skipped -- played in the last {config.DIVERSIFICATION_LOOKBACK_DAYS} "
                    f"day(s) and didn't clear the stricter re-confirmation bar."
                )
                continue
            diversification_flag = (f"{team} played within the last {config.DIVERSIFICATION_LOOKBACK_DAYS} days -- "
                                     f"needed {required_edge:.1%}+ edge and {config.DIVERSIFICATION_MIN_STRONG_FACTORS}+ "
                                     f"strong factors, and it cleared both.")

        split = public_splits.get(ev.game.game_id) if public_splits else None
        line_flag, dropped = _check_line_movement(ev, db, split)
        if dropped:
            dropped_notes.append(f"{label} ({matchup}): {line_flag}")
            continue

        dh_note = ev.game.dh_reasoning()
        reasoning, edge_data_pct, edge_astro_pct = _build_reasoning(ev, dh_note)

        if odds_american is not None and odds_american >= BIG_DOG_MIN_ODDS:
            reasoning.append(
                f"[Proven price bucket] {odds_american:+d} is a {BIG_DOG_MIN_ODDS}-or-longer dog -- "
                f"the only bucket carrying this system (28-30 but +44.0 units, ROI +75.8%). "
                f"Books shade favourite prices toward public money, which is what leaves value here.")

        plays.append(Recommendation(
            game=ev.game, side=ev.recommended_side, team=label, sport=ev.game.sport,
            odds_american=odds_american, odds_source=ev.odds.book, edge_pct=ev.edge_pct,
            model_prob=ev.model_prob_home if ev.recommended_side == "home" else ev.model_prob_away,
            market_prob=ev.market_prob_home if ev.recommended_side == "home" else ev.market_prob_away,
            stake_units=config.FLAT_STAKE_UNITS,
            stake_dollars=config.FLAT_STAKE_UNITS * config.UNIT_SIZE_DOLLARS,
            reasoning=reasoning,
            factor_scores=ev.factor_scores,
            diversification_flag=diversification_flag,
            line_movement_flag=line_flag,
        ))
        picked_today[team] = ev.game.dh_label().strip() or matchup
        per_sport_count[sport] = per_sport_count.get(sport, 0) + 1

    return plays, dropped_notes


def _edge_rank_key(ev):
    in_band = config.TARGET_EDGE_MIN <= ev.edge_pct <= config.TARGET_EDGE_MAX
    if in_band:
        band_center = (config.TARGET_EDGE_MIN + config.TARGET_EDGE_MAX) / 2
        return (0, abs(ev.edge_pct - band_center))
    return (1, -ev.edge_pct)


def select_fade_teams(evaluations):
    if not config.FADE_ENABLED:
        return []

    candidates = [e for e in evaluations if e.recommended_side and e.edge_pct >= config.FADE_MIN_EDGE]
    candidates.sort(key=lambda e: e.edge_pct, reverse=True)

    fades = []
    for ev in candidates[:config.FADE_MAX_PER_DAY]:
        fade_side = "away" if ev.recommended_side == "home" else "home"
        team = ev.game.away_team if fade_side == "away" else ev.game.home_team
        opponent = ev.game.home_team if fade_side == "away" else ev.game.away_team
        odds_american = ev.odds.away_ml if fade_side == "away" else ev.odds.home_ml
        model_prob = ev.model_prob_away if fade_side == "away" else ev.model_prob_home
        market_prob = ev.market_prob_away if fade_side == "away" else ev.market_prob_home

        reasoning = [f"Model favors {opponent} instead, by a {ev.edge_pct:.1%} edge."]
        reasoning += [fs.reasoning for fs in ev.factor_scores]

        fades.append(FadeTeam(
            game=ev.game, team=team + ev.game.dh_label(), sport=ev.game.sport, opponent=opponent,
            odds_american=odds_american, odds_source=ev.odds.book, edge_pct=-ev.edge_pct,
            model_prob=model_prob, market_prob=market_prob, reasoning=reasoning,
        ))
    return fades


def get_parlay_pool(evaluations):
    """All games that independently cleared their sport's edge floor AND the
    price policy, sorted by edge desc. Best-edge-first + the seen-team guard
    means a doubleheader contributes only its STRONGER game to the parlay."""
    candidates = [e for e in evaluations
                  if e.recommended_side and e.edge_pct >= config.min_edge_for(e.game.sport)]
    candidates.sort(key=lambda e: e.edge_pct, reverse=True)
    pool = []
    seen_teams = set()
    for ev in candidates:
        team = ev.game.home_team if ev.recommended_side == "home" else ev.game.away_team
        if team in seen_teams:
            continue
        odds_american = ev.odds.home_ml if ev.recommended_side == "home" else ev.odds.away_ml
        price_ok, _ = _price_check(odds_american, ev.edge_pct)
        if not price_ok:
            continue
        seen_teams.add(team)
        model_prob = ev.model_prob_home if ev.recommended_side == "home" else ev.model_prob_away
        market_prob = ev.market_prob_home if ev.recommended_side == "home" else ev.market_prob_away
        reasoning, _, _ = _build_reasoning(ev)
        pool.append(Recommendation(
            game=ev.game, side=ev.recommended_side, team=team + ev.game.dh_label(), sport=ev.game.sport,
            odds_american=odds_american, odds_source=ev.odds.book, edge_pct=ev.edge_pct,
            model_prob=model_prob, market_prob=market_prob,
            stake_units=config.FLAT_STAKE_UNITS, stake_dollars=config.FLAT_STAKE_UNITS * config.UNIT_SIZE_DOLLARS,
            reasoning=reasoning, factor_scores=ev.factor_scores,
        ))
    return pool


def american_prob(ml):
    ml = float(ml)
    if ml > 0:
        return 100.0 / (ml + 100.0)
    return -ml / (-ml + 100.0)


def _check_line_movement(ev, db, split):
    opening = db.get_opening_line(ev.game.game_id)
    if not opening:
        return None, False

    side = ev.recommended_side
    open_ml = opening["home_ml"] if side == "home" else opening["away_ml"]
    current_ml = ev.odds.home_ml if side == "home" else ev.odds.away_ml
    if open_ml is None or current_ml is None:
        return None, False

    cents_moved = abs(current_ml - open_ml)
    adverse = american_prob(current_ml) > american_prob(open_ml)

    if not adverse or cents_moved < config.LINE_MOVE_DROP_CENTS:
        return None, False

    if not config.LINE_MOVE_REQUIRES_SHARP_CONFIRM:
        return (f"Line moved {cents_moved:.0f} cents against the play (open {open_ml:+.0f} -> "
                f"now {current_ml:+.0f}) -- dropped."), True

    other_side_handle = None
    if split:
        other_side_handle = (100 - split.handle_pct_home) if side == "home" else split.handle_pct_home
    if other_side_handle is not None and other_side_handle >= config.HEAVY_MONEY_HANDLE_THRESHOLD * 100:
        return (f"Line moved {cents_moved:.0f} cents against the play AND {other_side_handle:.0f}% of handle is "
                f"on the other side -- dropped (smart money confirmed)."), True

    return (f"Line moved {cents_moved:.0f} cents against the play (open {open_ml:+.0f} -> now "
            f"{current_ml:+.0f}) but not confirmed by heavy money -- kept, watch closely."), False

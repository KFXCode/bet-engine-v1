"""
engine/strategy_rules.py
=========================
Non-negotiable rules layered on top of raw edge numbers:
  - never below the sport's edge floor (config.min_edge_for(sport))
  - PRICE POLICY: no heavy chalk, higher bars on small dogs, MLB favourites,
    and non-MLB long dogs
  - DOG-FIRST ranking on MLB
  - flat 1-unit sizing, up to MAX_PLAYS_PER_DAY plays PER SPORT
  - team diversification (no same team 3+ days without stricter re-confirm)
  - line movement (only drop on significant adverse move AND heavy money)
  - doubleheader safety (one game per pairing; every label names Gm 1/Gm 2)

PRICE POLICY (Aug 29, 2026), from 215 graded moneylines:
    big dogs (+150 or longer)   28-30   +44.0u   ROI +75.8%
    favorites (-199..-1)        64-38    +7.0u   ROI  +6.8%
    heavy favs (-200 or worse)  21-6     +0.1u   ROI  +0.4%
    small dogs (+1..+149)       12-16    -2.2u   ROI  -7.8%

CALIBRATION (Sep 30, 2026) -- the grade that split the rules by sport.

  MLB, 324 picks, bucketed by the model's own win probability:
      model 30-40%  ->  won 59.4%   ROI +148%   (32 picks)
      model 40-50%  ->  won 53.3%   ROI  +37%
      model 50-70%  ->  won ~58%    ROI  +2 to +5%
      model 80-90%  ->  won 73.3%   ROI   -8%
  The MLB model UNDERRATES its own dogs. So on MLB, dogs rank first for the
  daily slots and favourites need a 5% edge.

  NON-MLB, 52 picks: the OPPOSITE. It OVERRATES its dogs --
      model 30-40%  ->  won 22.7%   ROI  -17%   (22 picks)
  and overall it predicted 44.1% but won 38.5%, ROI -10.3%, with a Brier
  score slightly worse than the market's. So on every other sport:
    - the edge floor is 4% (config.MIN_EDGE_BY_SPORT), not 1.5%
    - a +150-or-longer dog needs a 7% edge (config.ML_NON_MLB_LONG_DOG_MIN_EDGE)
    - no dog-first ranking

The same rule that makes money on one sport was losing it on another, which is
why these are per-sport rather than global. Honest caveat on both: 32 and 22
picks are small samples. The DIRECTION of each finding is well supported; the
SIZE of the edges will shrink as the samples grow.

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

# MLB-only: dog-first ranking + a 5% bar on favourites.
DOG_FIRST_SPORTS = set(getattr(config, "ML_DOG_FIRST_SPORTS", ["MLB"]))
FAVORITE_MIN_EDGE = getattr(config, "ML_FAVORITE_MIN_EDGE", 0.05)

# Everyone else: a +150-or-longer dog needs a much wider gap.
NON_MLB_LONG_DOG_MIN_EDGE = getattr(config, "ML_NON_MLB_LONG_DOG_MIN_EDGE", 0.07)


def _odds_for(ev):
    return ev.odds.home_ml if ev.recommended_side == "home" else ev.odds.away_ml


def _price_check(odds, edge_pct, sport=None):
    """(ok, note). Applies the graded price policy to one candidate."""
    if odds is None:
        return True, None
    if odds <= MAX_FAV:
        return False, (f"priced {odds:+d} -- heavy favourites ({MAX_FAV:+d} or worse) have gone "
                       f"21-6 for +0.1 units (ROI +0.4%), and the calibration grade has 80%+ "
                       f"model favourites at ROI -8%. A high win rate that earns nothing isn't "
                       f"a bet, so this bucket is off the board.")

    dog_first = sport in DOG_FIRST_SPORTS

    if dog_first and odds < 0 and edge_pct < FAVORITE_MIN_EDGE:
        return False, (f"priced {odds:+d} with a {edge_pct:.1%} edge -- MLB favourites need "
                       f"{FAVORITE_MIN_EDGE:.0%}+. Small-edge favourites return ~2-5% ROI while "
                       f"the model's real value sits in underdogs, so a thin favourite doesn't "
                       f"get a slot a dog could use.")

    if (not dog_first and odds >= BIG_DOG_MIN_ODDS and edge_pct < NON_MLB_LONG_DOG_MIN_EDGE):
        return False, (f"priced {odds:+d} with a {edge_pct:.1%} edge -- {sport or 'non-MLB'} dogs "
                       f"at +{BIG_DOG_MIN_ODDS} or longer need {NON_MLB_LONG_DOG_MIN_EDGE:.0%}+. "
                       f"Outside MLB the model OVERRATES long dogs (rated 30-40%, won 22.7%, "
                       f"ROI -17%), so it has to show a much wider gap before its number is "
                       f"worth betting.")

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
    candidates.sort(key=_rank_key)

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
        odds_american = _odds_for(ev)

        price_ok, price_note = _price_check(odds_american, ev.edge_pct, sport)
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
            if sport in DOG_FIRST_SPORTS:
                reasoning.append(
                    f"[Proven price bucket] {odds_american:+d} is a {BIG_DOG_MIN_ODDS}-or-longer MLB dog -- "
                    f"the bucket carrying this system. The Sep 30 calibration grade has the MLB model "
                    f"UNDERRATING its dogs (said ~31%, won ~59%).")
            else:
                reasoning.append(
                    f"[Cleared the long-dog bar] {odds_american:+d} with a {ev.edge_pct:.1%} edge -- "
                    f"above the {NON_MLB_LONG_DOG_MIN_EDGE:.0%} bar {sport} long dogs need, because "
                    f"outside MLB the model has been overrating them.")

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


def _rank_key(ev):
    """Dogs first on DOG_FIRST_SPORTS (MLB), then the original edge ranking.
    Every other sport ranks on edge alone -- its model overrates dogs, so
    promoting them would promote its weakest picks."""
    dog_tier = 1
    if ev.game.sport in DOG_FIRST_SPORTS:
        odds = _odds_for(ev)
        if odds is not None and odds > 0:
            dog_tier = 0
    return (dog_tier,) + _edge_rank_key(ev)


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
    price policy, sorted by edge desc."""
    candidates = [e for e in evaluations
                  if e.recommended_side and e.edge_pct >= config.min_edge_for(e.game.sport)]
    candidates.sort(key=lambda e: e.edge_pct, reverse=True)
    pool = []
    seen_teams = set()
    for ev in candidates:
        team = ev.game.home_team if ev.recommended_side == "home" else ev.game.away_team
        if team in seen_teams:
            continue
        odds_american = _odds_for(ev)
        price_ok, _ = _price_check(odds_american, ev.edge_pct, ev.game.sport)
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

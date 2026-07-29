"""
engine/strategy_rules.py
=========================
Non-negotiable rules layered on top of raw edge numbers:
  - never below MIN_EDGE
  - flat 1-unit sizing
  - up to MAX_PLAYS_PER_DAY plays PER DAY, picked across ALL enabled sports
  - team diversification: don't play the same team 3+ days running without
    stricter re-confirmation
  - line movement: only drop on significant adverse movement AND heavy money
  - doubleheader safety: at most ONE game per team pairing, and every pick's
    team label names WHICH game (Gm 1 / Gm 2) so a bet is never ambiguous.

select_daily_plays() is the single entry point run_daily.py calls.
"""

import config
from engine.models import Recommendation, FadeTeam


def select_daily_plays(evaluations, db, public_splits, run_date_str):
    candidates = [e for e in evaluations if e.recommended_side and e.edge_pct >= config.MIN_EDGE]
    candidates.sort(key=_edge_rank_key)

    recent_picks = {p["team"] for p in db.get_recent_team_picks(run_date_str, config.DIVERSIFICATION_LOOKBACK_DAYS)}
    picked_today = {}

    plays = []
    dropped_notes = []
    for ev in candidates:
        if len(plays) >= config.MAX_PLAYS_PER_DAY:
            break

        team = ev.game.home_team if ev.recommended_side == "home" else ev.game.away_team
        label = team + ev.game.dh_label()
        matchup = f"{ev.game.away_team} @ {ev.game.home_team}{ev.game.dh_label()}"

        if team in picked_today:
            # Because candidates are sorted best-edge-first, the version of
            # this team already in `plays` is the STRONGER game -- so on a
            # doubleheader we keep the better matchup and note the other.
            dropped_notes.append(
                f"{label} ({matchup}): already locked in today's stronger play on {team} from the "
                f"{picked_today[team]} game -- not doubling up on the same team twice in one day."
            )
            continue

        diversification_flag = None
        if team in recent_picks:
            strong_factors = sum(1 for fs in ev.factor_scores if abs(fs.signal) >= 0.5)
            required_edge = config.MIN_EDGE + config.DIVERSIFICATION_EXTRA_EDGE
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

        reasoning = [fs.reasoning for fs in ev.factor_scores]
        dh_note = ev.game.dh_reasoning()
        if dh_note:
            reasoning.insert(0, dh_note)

        odds_american = ev.odds.home_ml if ev.recommended_side == "home" else ev.odds.away_ml
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
    """All games that independently cleared MIN_EDGE, sorted by edge desc.
    Best-edge-first + the seen-team guard means a doubleheader contributes
    only its STRONGER game to the parlay -- never both CIN games."""
    candidates = [e for e in evaluations if e.recommended_side and e.edge_pct >= config.MIN_EDGE]
    candidates.sort(key=lambda e: e.edge_pct, reverse=True)
    pool = []
    seen_teams = set()
    for ev in candidates:
        team = ev.game.home_team if ev.recommended_side == "home" else ev.game.away_team
        if team in seen_teams:
            continue
        seen_teams.add(team)
        odds_american = ev.odds.home_ml if ev.recommended_side == "home" else ev.odds.away_ml
        model_prob = ev.model_prob_home if ev.recommended_side == "home" else ev.model_prob_away
        market_prob = ev.market_prob_home if ev.recommended_side == "home" else ev.market_prob_away
        pool.append(Recommendation(
            game=ev.game, side=ev.recommended_side, team=team + ev.game.dh_label(), sport=ev.game.sport,
            odds_american=odds_american, odds_source=ev.odds.book, edge_pct=ev.edge_pct,
            model_prob=model_prob, market_prob=market_prob,
            stake_units=config.FLAT_STAKE_UNITS, stake_dollars=config.FLAT_STAKE_UNITS * config.UNIT_SIZE_DOLLARS,
            reasoning=[fs.reasoning for fs in ev.factor_scores], factor_scores=ev.factor_scores,
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

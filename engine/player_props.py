"""
engine/player_props.py
=======================
NFL player props: passing yards, rushing yards, receiving yards, receptions
and QB passing touchdowns. This is the SECOND prop board -- anytime-TD props
live in engine/td_props.py and are ranked separately, because a TD prop and a
receiving-yards prop are not comparable bets and shouldn't compete for slots.

HOW AN EDGE IS FOUND (and why it's shaped this way):

  1. PROJECT the player's number for THIS game:
         base per-game rate  ->  adjusted for the game environment
     The environment adjustment comes from the market itself. The total and
     spread imply how many points each team scores, and that changes volume in
     predictable directions -- a QB in a projected shootout throws more, and a
     RB whose team is favored by two scores runs more in the second half while
     the trailing team's back gets abandoned. Game script is the single
     biggest driver of NFL prop volume and the market hands it to us free.

  2. CONVERT the gap to a probability instead of betting the gap directly.
     Single-game outcomes scatter enormously -- one broken tackle is 40 yards.
     "We project 68 vs a 62.5 line" is NOT a lock; with a 30-yard spread of
     outcomes it's about 57%. We model the result as normal around the
     projection and read the probability off that curve:
         P(over) = 1 - CDF((line - projection) / sigma)
     Skipping this is how prop models convince themselves a 5-yard difference
     is a 90% bet. It's the same discipline that made the HR board's failure
     legible: the model claimed edges that the distribution never supported.

  3. COMPARE to the book's implied probability, de-vigged across the two
     sides. Standard -110/-110 pricing implies 52.4% per side, so a real edge
     has to clear the vig, not just 50%.

TWO HARD RULES:
  - MIN GAMES: below config.PLAYER_PROP_MIN_GAMES a per-game average is noise.
    Those players are skipped rather than priced off one big afternoon.
  - NO UNDERS ON QUESTIONABLE PLAYERS (your call). If a questionable player is
    scratched, most books void the bet but some grade it UNDER -- so the bet's
    settlement rules change by book. Overs are unaffected, since a scratch
    just voids them.

The board is a CAP, not a quota: props post only while they clear the edge
bar. A thin slate publishing 4 props is the correct outcome, not a failure.
"""

import logging
import math

import config

logger = logging.getLogger("player_props")

MARKETS = config.PLAYER_PROP_MARKETS
SIGMA = config.PLAYER_PROP_SIGMA
MIN_EDGE = config.PLAYER_PROP_MIN_EDGE
MIN_GAMES = config.PLAYER_PROP_MIN_GAMES
MAX_PER_DAY = config.PLAYER_PROP_MAX_PER_DAY
SKIP_UNDER_IF_Q = config.PLAYER_PROP_SKIP_UNDER_IF_QUESTIONABLE

LEAGUE_AVG_TEAM_TOTAL = 22.5   # points; the neutral game environment

# Which per-game profile field feeds each market, plus how to say it in English.
MARKET_SPEC = {
    "player_pass_yds":      {"field": "pass_yds_pg", "label": "Passing Yards",
                             "unit": "yds", "positions": {"QB"}},
    "player_rush_yds":      {"field": "rush_yds_pg", "label": "Rushing Yards",
                             "unit": "yds", "positions": {"RB", "FB", "QB"}},
    "player_reception_yds": {"field": "rec_yds_pg", "label": "Receiving Yards",
                             "unit": "yds", "positions": {"WR", "TE", "RB", "FB"}},
    "player_receptions":    {"field": "rec_pg", "label": "Receptions",
                             "unit": "rec", "positions": {"WR", "TE", "RB", "FB"}},
    "player_pass_tds":      {"field": "pass_td_pg", "label": "Passing TDs",
                             "unit": "TD", "positions": {"QB"}},
}


def american_to_implied(ml):
    ml = float(ml)
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def _normal_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _devig(p_over, p_under):
    """Strip the book's margin so the two sides sum to 1. Comparing our
    probability against a raw implied number would credit us with an edge that
    is really just the vig."""
    total = p_over + p_under
    if total <= 0:
        return p_over, p_under
    return p_over / total, p_under / total


def implied_team_totals(odds):
    """(home_total, away_total) from the market's total + spread.

    home_spread is negative when home is favored, so the favorite's implied
    total is total/2 + abs(spread)/2."""
    total = getattr(odds, "total", None)
    home_spread = getattr(odds, "home_spread", None)
    if total is None:
        return None, None
    try:
        total = float(total)
    except (TypeError, ValueError):
        return None, None
    if home_spread is None:
        return total / 2.0, total / 2.0
    try:
        hs = float(home_spread)
    except (TypeError, ValueError):
        return total / 2.0, total / 2.0
    home = (total / 2.0) - (hs / 2.0)
    return round(home, 2), round(total - home, 2)


def _environment_factor(market, team_total, team_spread):
    """How this game's projected script moves a player's volume, plus the
    sentence explaining it. Returns (multiplier, note or None).

    The direction differs by market, which is the whole point:
      - passing rises when a team is projected to score a lot OR is trailing
        (garbage-time volume is real volume for a yardage prop)
      - rushing rises with a big lead (clock-killing carries) and collapses
        when a team is chasing points
      - receiving sits between the two, following overall scoring
    """
    if team_total is None:
        return 1.0, None

    scoring = max(0.72, min(1.32, team_total / LEAGUE_AVG_TEAM_TOTAL))

    if market in ("player_pass_yds", "player_pass_tds"):
        factor = scoring
        note = (f"Team implied for {team_total:.1f} points"
                f"{' -- projected shootout' if scoring >= 1.12 else ''}"
                f"{' -- low-scoring script' if scoring <= 0.9 else ''}.")
        if team_spread is not None and team_spread >= 6.0:
            factor *= 1.06
            note += f" Underdog by {team_spread:.1f}, so likely throwing to catch up."
        return factor, note

    if market == "player_rush_yds":
        factor = 1.0 + (scoring - 1.0) * 0.6   # rushing tracks scoring, but weakly
        note = f"Team implied for {team_total:.1f} points."
        if team_spread is not None:
            if team_spread <= -6.0:
                factor *= 1.10
                note += f" Favored by {abs(team_spread):.1f} -- positive game script means clock-killing carries."
            elif team_spread >= 6.0:
                factor *= 0.88
                note += f" Underdog by {team_spread:.1f} -- trailing teams abandon the run."
        return factor, note

    # receptions / receiving yards
    factor = 1.0 + (scoring - 1.0) * 0.8
    note = f"Team implied for {team_total:.1f} points."
    if team_spread is not None and team_spread >= 7.0:
        factor *= 1.05
        note += f" Underdog by {team_spread:.1f} -- more pass volume chasing points."
    return factor, note


def _questionable(name, injuries_by_player):
    """True when the player carries a designation that could keep him out."""
    if not injuries_by_player:
        return False
    status = str(injuries_by_player.get(name, "")).strip().lower()
    return status in ("questionable", "doubtful", "out", "ir", "inactive")


def evaluate_player_props(games, rosters_by_team, profiles, prop_odds,
                          odds_by_game, injuries_by_player=None):
    """
    rosters_by_team : {team_abbr: [{player_id, name, position}]}
    profiles        : {player_id: profile from data/nfl_players.get_player_profile}
    prop_odds       : {(game_id, market, norm_name): {"line", "over", "under", "book"}}
    odds_by_game    : {game_id: MoneylineOdds}   -- for total/spread
    injuries_by_player : {player_name: status}   -- optional

    Returns the full scored pool sorted by edge, strongest first.
    """
    from data.prop_odds import norm_player   # shared normalizer, avoids drift

    pool = []
    considered = 0
    skipped_thin = 0
    skipped_no_line = 0
    skipped_under_q = 0

    for game in games:
        if game.sport != "NFL":
            continue
        if getattr(game, "is_preseason", False):
            continue

        odds = odds_by_game.get(game.game_id)
        home_total, away_total = implied_team_totals(odds) if odds else (None, None)
        home_spread = getattr(odds, "home_spread", None) if odds else None
        try:
            home_spread = float(home_spread) if home_spread is not None else None
        except (TypeError, ValueError):
            home_spread = None
        away_spread = -home_spread if home_spread is not None else None

        for team, opponent, team_total, team_spread in (
            (game.home_team, game.away_team, home_total, home_spread),
            (game.away_team, game.home_team, away_total, away_spread),
        ):
            for player in rosters_by_team.get(team, []):
                profile = profiles.get(player["player_id"])
                if not profile:
                    continue
                if (profile.get("games") or 0) < MIN_GAMES:
                    skipped_thin += 1
                    continue

                is_q = _questionable(player["name"], injuries_by_player)
                key_name = norm_player(player["name"])

                for market in MARKETS:
                    spec = MARKET_SPEC[market]
                    if player["position"] not in spec["positions"]:
                        continue

                    base = profile.get(spec["field"])
                    if not base or base <= 0:
                        continue

                    priced = prop_odds.get((game.game_id, market, key_name))
                    if not priced or priced.get("line") is None:
                        skipped_no_line += 1
                        continue

                    considered += 1
                    line = float(priced["line"])
                    factor, env_note = _environment_factor(market, team_total, team_spread)
                    projection = base * factor
                    sigma = SIGMA.get(market, 25.0)

                    z = (line - projection) / sigma
                    p_over_raw = 1.0 - _normal_cdf(z)
                    p_under_raw = 1.0 - p_over_raw

                    over_odds = priced.get("over")
                    under_odds = priced.get("under")
                    if over_odds is None or under_odds is None:
                        continue
                    mkt_over, mkt_under = _devig(american_to_implied(over_odds),
                                                 american_to_implied(under_odds))

                    for side, model_p, market_p, price in (
                        ("over", p_over_raw, mkt_over, over_odds),
                        ("under", p_under_raw, mkt_under, under_odds),
                    ):
                        if side == "under" and SKIP_UNDER_IF_Q and is_q:
                            skipped_under_q += 1
                            continue
                        edge = model_p - market_p
                        if edge < MIN_EDGE:
                            continue

                        reasoning = [
                            f"Projection {projection:.1f} {spec['unit']} vs a {line:g} line "
                            f"({projection - line:+.1f}).",
                            f"Season baseline {base:.1f} {spec['unit']}/game over "
                            f"{profile['games']} game(s)"
                            f"{f' ({profile[chr(34)+chr(34)] if False else profile.get(chr(115)+chr(101)+chr(97)+chr(115)+chr(111)+chr(110))} season)' if profile.get('season') else ''}.",
                        ]
                        if env_note:
                            reasoning.append(env_note)
                        reasoning.append(
                            f"A {sigma:g}-{spec['unit']} spread of real single-game outcomes puts "
                            f"{side.upper()} at {model_p * 100:.1f}%.")
                        reasoning.append(
                            f"Book implies {market_p * 100:.1f}% at {price:+d} after removing the vig "
                            f"-- a {edge * 100:+.1f} point edge.")
                        if is_q:
                            reasoning.append(
                                "Listed with an injury designation -- confirm he's active before betting.")

                        pool.append({
                            "player_name": player["name"],
                            "position": player["position"],
                            "team": team,
                            "opponent": opponent,
                            "game_id": game.game_id,
                            "market": market,
                            "market_label": spec["label"],
                            "unit": spec["unit"],
                            "side": side,
                            "line": line,
                            "projection": round(projection, 1),
                            "odds_american": price,
                            "book": priced.get("book"),
                            "model_prob": model_p,
                            "market_prob": market_p,
                            "edge_pct": edge,
                            "questionable": is_q,
                            "reasoning": reasoning,
                        })

    pool.sort(key=lambda c: c["edge_pct"], reverse=True)
    logger.info("PROP-DIAG: %d priced player-market combos considered | %d players too thin "
                "(<%d games) | %d had no posted line | %d unders skipped on questionable "
                "players | %d cleared the %.1f%% edge bar.",
                considered, skipped_thin, MIN_GAMES, skipped_no_line,
                skipped_under_q, len(pool), MIN_EDGE * 100)
    for c in pool[:8]:
        logger.info("PROP-DIAG: %s %s %s %g (%s) edge %+.1f%%",
                    c["player_name"], c["side"].upper(), c["market_label"],
                    c["line"], c["team"], c["edge_pct"] * 100)
    return pool


def finalize_player_props(pool, max_per_day=None):
    """Trim the pool to the published board.

    ONE PROP PER PLAYER: without this a single soft line produces both a
    receiving-yards and a receptions pick on the same man, which is one
    correlated opinion dressed up as two bets. We keep his strongest edge.

    The cap is a ceiling, never a target -- if only five props clear the bar,
    five is what publishes."""
    max_per_day = max_per_day or MAX_PER_DAY
    final = []
    seen_players = set()

    for c in pool:
        key = c["player_name"].lower()
        if key in seen_players:
            continue
        seen_players.add(key)
        final.append(c)
        if len(final) >= max_per_day:
            break

    logger.info("PROP-DIAG: FINAL %d player prop(s) (cap %d): %s",
                len(final), max_per_day,
                ", ".join(f"{c['player_name']} {c['side']} {c['line']:g} {c['market_label']}"
                          f" {c['edge_pct'] * 100:+.1f}%" for c in final) or "(none)")
    return final


def label_for(prop):
    """Ledger/report label, e.g. 'Bijan Robinson Over 68.5 Rushing Yards'."""
    return (f"{prop['player_name']} {prop['side'].title()} {prop['line']:g} "
            f"{prop['market_label']}")

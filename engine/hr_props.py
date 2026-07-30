"""
engine/hr_props.py
===================
HR Prop Workflow -- the "Grok HR Signal System": a weighted, multi-category
signal-cluster model (0-100). Each pick's score is the sum of five weighted
categories (points defined in config.HR_CATEGORY_POINTS):

  contact_quality (30) -- barrel%, avg+max exit velo, hard-hit%, xwOBA,
                          HR/FB, hot-streak trend  (the Statcast core)
  park_weather   (25)  -- park HR factor + LIVE temperature & wind direction
  matchup        (25)  -- opposing SP HR-vulnerability (HR/9, barrel/hard-hit allowed)
  pitcher_context(15)  -- overall SP quality (FIP/ERA, strikeout rate)
  confirmation   (5)   -- season HR volume, pull%, lineup confirmation

"Signal cluster" rule: a pick is only a STRONG play when >= config.
HR_PROP_MIN_CLUSTERS of the four predictive categories each hit their 60%
mark. Thin-cluster days are still shown but flagged, per your two-version spec.

Pool is first restricted to the top-N season-HR sluggers on the slate, and
deduped to ONE entry per player (best game on a doubleheader).
"""

import config
from data.park_factors import park_factor_for
from data.weather import get_park_weather


def evaluate_hr_prop_candidates(games, rosters, stats_provider, public_prop_splits,
                                 situational_by_team, lineup_source=None):
    lineup_source = lineup_source or {}
    pts = config.HR_CATEGORY_POINTS

    # --- Step 0: eligible power-hitter pool (top-N by season HR) ----------
    pool = []
    for game in games:
        for batting_team, opp_pitcher in (
            (game.home_team, game.away_pitcher),
            (game.away_team, game.home_pitcher),
        ):
            if not opp_pitcher:
                continue
            for batter_name in rosters.get(batting_team, []):
                bp = stats_provider.get_batter_profile(batter_name, batting_team)
                pool.append({"batter_name": batter_name, "batter_profile": bp,
                             "batting_team": batting_team, "opp_pitcher": opp_pitcher,
                             "game": game, "hr_count": bp.hr_count or 0})

    pool = [c for c in pool if c["hr_count"] >= config.HR_PROP_MIN_SEASON_HR]
    pool.sort(key=lambda c: c["hr_count"], reverse=True)
    pool = pool[: config.HR_PROP_TOP_N_POOL]

    # weather is per-park; fetch once per home team, cache in this dict
    weather_cache = {}

    candidates = []
    for entry in pool:
        game = entry["game"]
        opp_pitcher = entry["opp_pitcher"]
        batting_team = entry["batting_team"]
        batter_name = entry["batter_name"]
        batter = entry["batter_profile"]

        if batter.data_quality in ("degraded", "not_found") or batter.barrel_pct is None:
            continue

        pitcher = stats_provider.get_pitcher_profile(opp_pitcher.name)
        hr_park_factor = park_factor_for(game.home_team)[1]

        if config.HR_WEATHER_ENABLED:
            if game.home_team not in weather_cache:
                weather_cache[game.home_team] = get_park_weather(game.home_team, game.game_time_utc)
            weather = weather_cache[game.home_team]
        else:
            weather = {"available": False, "wind_effect": "neutral", "summary": "weather off"}

        cats, reasoning = _score_categories(batter, pitcher, hr_park_factor, weather, entry["hr_count"])

        # weighted 0-100 total
        score = round(sum(cats[name] / 100.0 * pts[name] for name in pts), 1)

        # signal-cluster count across the 4 predictive categories
        predictive = ["contact_quality", "park_weather", "matchup", "pitcher_context"]
        clusters = sum(1 for name in predictive if cats[name] >= 60)
        reasoning.append(f"[Signal Cluster] {clusters}/4 predictive categories are strong (60%+). "
                         f"Need {config.HR_PROP_MIN_CLUSTERS}+ for a full-confluence STRONG play.")
        if clusters < config.HR_PROP_MIN_CLUSTERS:
            reasoning.append("NOTE: thin cluster -- shown as the best available today, not a full-confluence spot.")

        public_lean = public_prop_splits.get((batting_team, batter_name)) if public_prop_splits else None
        if public_lean is not None and public_lean >= 80:
            if score < 90:
                continue
            reasoning.append(f"Public is {public_lean:.0f}% on the OVER -- kept only because the model is elite.")

        lineup_flag = lineup_source.get(batting_team, "confirmed")
        if lineup_flag == "roster":
            reasoning.append("NOTE: starting lineup not posted yet -- confirm this player is actually in today's lineup.")

        dh_note = game.dh_reasoning()
        if dh_note:
            reasoning.insert(0, dh_note)

        reasoning.append(f"FINAL HR SCORE: {score}/100 (weighted across all 5 categories).")

        candidates.append({
            "player_name": batter_name + game.dh_label(),
            "player_key": batter_name.strip().lower(),
            "team": batting_team,
            "game_id": game.game_id,
            "opponent_pitcher": opp_pitcher.name,
            "score": score,
            "clusters": clusters,
            "reasoning": reasoning,
            "data_quality": "roster_unconfirmed" if lineup_flag == "roster" else "ok",
        })

    # --- dedupe to ONE entry per player (best game) ----------------------
    best_by_player = {}
    for c in candidates:
        key = c["player_key"]
        if key not in best_by_player or c["score"] > best_by_player[key]["score"]:
            best_by_player[key] = c
    deduped = sorted(best_by_player.values(), key=lambda c: c["score"], reverse=True)

    strongest = [c for c in deduped if c["score"] >= config.HR_PROP_MIN_SCORE]
    return strongest[: config.HR_PROP_MAX_PER_DAY]


def _score_categories(batter, pitcher, hr_park_factor, weather, hr_count):
    """Returns (cats, reasoning). Each cats[name] is a 0-100 sub-score for that
    category; the caller applies the category weights."""
    reasoning = []
    cats = {}

    # === 1. CONTACT QUALITY (Statcast core) ==============================
    c = 50.0
    if batter.barrel_pct is not None:
        if batter.barrel_pct >= 15:
            c += 22; reasoning.append(f"[Contact] ELITE {batter.barrel_pct:.1f}% barrel rate (15%+).")
        elif batter.barrel_pct >= 12:
            c += 15; reasoning.append(f"[Contact] Strong {batter.barrel_pct:.1f}% barrel rate (12%+).")
        elif batter.barrel_pct < 6:
            c -= 18; reasoning.append(f"[Contact] Weak {batter.barrel_pct:.1f}% barrel rate (under 6%).")
        else:
            reasoning.append(f"[Contact] Average {batter.barrel_pct:.1f}% barrel rate.")
    if batter.avg_exit_velo is not None:
        if batter.avg_exit_velo >= 92:
            c += 10; reasoning.append(f"[Contact] High avg exit velo {batter.avg_exit_velo:.1f} mph (92+).")
        elif batter.avg_exit_velo < 87:
            c -= 8; reasoning.append(f"[Contact] Low avg exit velo {batter.avg_exit_velo:.1f} mph.")
    if batter.max_exit_velo is not None and batter.max_exit_velo >= 112:
        c += 6; reasoning.append(f"[Contact] Big raw-power ceiling: {batter.max_exit_velo:.1f} mph max EV.")
    if batter.hard_hit_pct is not None and batter.hard_hit_pct >= 45:
        c += 6; reasoning.append(f"[Contact] Elite {batter.hard_hit_pct:.1f}% hard-hit rate.")
    if batter.xwoba is not None:
        if batter.xwoba >= 0.370:
            c += 8; reasoning.append(f"[Contact] Elite xwOBA {batter.xwoba:.3f} (true-talent contact).")
        elif batter.xwoba < 0.300:
            c -= 8; reasoning.append(f"[Contact] Low xwOBA {batter.xwoba:.3f}.")
    if batter.recent_barrel_trend and batter.recent_barrel_trend > 2:
        c += 8; reasoning.append(f"[Contact] HOT: barrel% +{batter.recent_barrel_trend:.1f} pts over last 15 days.")
    cats["contact_quality"] = _clamp(c)

    # === 2. PARK + WEATHER ================================================
    c = 50.0
    if hr_park_factor >= 110:
        c += 20; reasoning.append(f"[Park] Big HR park (factor {hr_park_factor}).")
    elif hr_park_factor >= 105:
        c += 12; reasoning.append(f"[Park] Hitter-friendly park (factor {hr_park_factor}).")
    elif hr_park_factor <= 92:
        c -= 18; reasoning.append(f"[Park] Pitcher's park (factor {hr_park_factor}).")
    else:
        reasoning.append(f"[Park] Neutral park (factor {hr_park_factor}).")
    if weather.get("available"):
        temp = weather.get("temp_f")
        if temp is not None:
            if temp >= 85:
                c += 10; reasoning.append(f"[Weather] Hot: {temp:.0f}\u00b0F -- ball carries.")
            elif temp >= 75:
                c += 5; reasoning.append(f"[Weather] Warm: {temp:.0f}\u00b0F.")
            elif temp <= 55:
                c -= 10; reasoning.append(f"[Weather] Cold: {temp:.0f}\u00b0F -- ball dies.")
        eff = weather.get("wind_effect")
        if eff == "out":
            c += 14; reasoning.append(f"[Weather] {weather.get('summary')} -- HR BOOST.")
        elif eff == "in":
            c -= 14; reasoning.append(f"[Weather] {weather.get('summary')} -- HR SUPPRESSOR.")
        elif weather.get("summary"):
            reasoning.append(f"[Weather] {weather.get('summary')}.")
    else:
        reasoning.append("[Weather] No live reading -- park factor only.")
    cats["park_weather"] = _clamp(c)

    # === 3. MATCHUP (opposing SP HR vulnerability) =======================
    c = 50.0
    if pitcher and pitcher.hr_per_9 is not None:
        if pitcher.hr_per_9 >= 1.5:
            c += 22; reasoning.append(f"[Matchup] {pitcher.name} is very HR-prone: {pitcher.hr_per_9:.2f} HR/9.")
        elif pitcher.hr_per_9 >= 1.2:
            c += 12; reasoning.append(f"[Matchup] {pitcher.name} allows plenty of HRs: {pitcher.hr_per_9:.2f} HR/9.")
        elif pitcher.hr_per_9 < 0.9:
            c -= 18; reasoning.append(f"[Matchup] {pitcher.name} stingy on HRs: {pitcher.hr_per_9:.2f} HR/9.")
        else:
            reasoning.append(f"[Matchup] {pitcher.name} average HR rate: {pitcher.hr_per_9:.2f} HR/9.")
    if pitcher and pitcher.barrel_pct_allowed is not None:
        if pitcher.barrel_pct_allowed >= 9:
            c += 14; reasoning.append(f"[Matchup] {pitcher.name} allows a high {pitcher.barrel_pct_allowed:.1f}% barrel rate.")
        elif pitcher.barrel_pct_allowed < 5:
            c -= 10; reasoning.append(f"[Matchup] {pitcher.name} suppresses barrels ({pitcher.barrel_pct_allowed:.1f}%).")
    if pitcher and pitcher.hard_hit_pct_allowed is not None and pitcher.hard_hit_pct_allowed >= 42:
        c += 6; reasoning.append(f"[Matchup] {pitcher.name} allows hard contact ({pitcher.hard_hit_pct_allowed:.1f}% hard-hit).")
    cats["matchup"] = _clamp(c)

    # === 4. PITCHER CONTEXT (overall SP quality) =========================
    c = 50.0
    fip_or_era = None
    if pitcher:
        fip_or_era = pitcher.fip if pitcher.fip is not None else pitcher.era
    if fip_or_era is not None:
        if fip_or_era >= 5.0:
            c += 20; reasoning.append(f"[Pitcher] Weak starter (FIP/ERA {fip_or_era:.2f}) -- exploitable.")
        elif fip_or_era >= 4.3:
            c += 10; reasoning.append(f"[Pitcher] Below-average starter (FIP/ERA {fip_or_era:.2f}).")
        elif fip_or_era <= 3.3:
            c -= 20; reasoning.append(f"[Pitcher] Strong starter (FIP/ERA {fip_or_era:.2f}) -- tough spot.")
        else:
            reasoning.append(f"[Pitcher] Average starter (FIP/ERA {fip_or_era:.2f}).")
    if pitcher and pitcher.k_pct is not None and pitcher.k_pct >= 26:
        c -= 8; reasoning.append(f"[Pitcher] High strikeout rate ({pitcher.k_pct:.0f}%) -- fewer balls in play.")
    cats["pitcher_context"] = _clamp(c)

    # === 5. CONFIRMATION (volume, pull, lineup) ==========================
    c = 50.0
    if hr_count >= 30:
        c += 26; reasoning.append(f"[Confirm] Elite season power: {hr_count} HR.")
    elif hr_count >= 20:
        c += 16; reasoning.append(f"[Confirm] Strong season power: {hr_count} HR.")
    else:
        c += 6; reasoning.append(f"[Confirm] {hr_count} HR this season (in the top-{config.HR_PROP_TOP_N_POOL} pool).")
    if batter.pull_pct is not None and batter.pull_pct >= 40:
        c += 10; reasoning.append(f"[Confirm] Pull-heavy ({batter.pull_pct:.0f}%) -- most HRs are pulled.")
    cats["confirmation"] = _clamp(c)

    return cats, reasoning


def _clamp(v):
    return max(0.0, min(100.0, v))

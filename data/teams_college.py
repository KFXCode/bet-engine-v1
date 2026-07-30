"""
data/teams_college.py
======================
Shared team-name normalizer for the college sports (NCAA Football, NCAA
Basketball). Unlike the pro leagues there are hundreds of schools and no
stable short-abbreviation table worth hardcoding, so instead of a fixed dict
this canonicalizes a full team name into one consistent string used as BOTH
the odds-matching key and the display label.

ESPN's scoreboard and The Odds API both spell college teams with their full
"School Mascot" name (e.g. "Alabama Crimson Tide"), so canonicalizing both
sides the same way lets them match without a per-team map. Any team that
still doesn't line up simply falls back to simulated odds for that one game
(logged), exactly like a missed pro match.
"""

import re

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s&]")


def normalize_college_team(raw):
    """Canonical, human-readable team name: trimmed, single-spaced, with
    stray punctuation removed. Used as the matching key AND the label, so
    both data feeds land on the same string."""
    if not raw:
        return raw
    name = _PUNCT.sub("", str(raw))
    name = _WS.sub(" ", name).strip()
    return name


def college_key(raw):
    """Lowercased matching key -- what the odds provider compares on, so
    case/spacing differences between feeds never cause a miss."""
    return normalize_college_team(raw).lower()

"""
data/prop_performance.py
=========================
Long-memory bench list for prop picks: players this system has repeatedly
picked and who have repeatedly FAILED to deliver.

WHY THIS EXISTS (Aug 29, 2026): the rotation fade only looked back 3-7 days,
so a name could always cycle back in once its cooldown expired -- forever.
The ledger showed exactly that: Ben Rice 14 picks / 1 hit, Kazuma Okamoto
13 / 0, Pete Alonso 12 / 0. Those three alone were 29% of every HR pick ever
made and went a combined 1-for-39, while 47 different players had been picked
in total. Short-term rotation cannot fix that, because the problem isn't
"picked too recently" -- it's "the model keeps liking a bat the results have
already disproven."

THE RULE: once a player has enough graded picks to judge (MIN_SAMPLE), if his
hit rate is far below what a HR prop should return, he is benched for a long
stretch (BENCH_DAYS) instead of a few days. A hard zero-for-N rule catches the
Okamoto/Alonso case immediately.

This is deliberately CONSERVATIVE about small samples -- going 0-for-2 is
normal variance for a home run bet and is NOT evidence of anything. It only
acts once the sample is big enough that continuing to pick the player is a
model failure rather than bad luck.

Reads the DB directly (like data/recent_form.py) so callers don't have to
thread a connection through. Never raises -- returns an empty set on any
problem, so a bad read can't stop the daily board from being built.
"""

import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta

import config

logger = logging.getLogger("prop_performance")

# Minimum graded picks before results mean anything at all.
MIN_SAMPLE = 4

# Bench a player whose hit rate is below this once MIN_SAMPLE is reached.
# A decent anytime-HR bet should cash roughly 12-20% of the time; 10% is the
# floor where continuing to pick someone is indefensible.
MIN_HIT_RATE = 0.10

# Hard rule: this many graded picks with ZERO hits benches you regardless of
# rate math. Catches the 13-for-0 and 12-for-0 cases on the spot.
ZERO_FOR_N = 4

# How long a chronic misser stays benched. Long enough to actually clear the
# board, not the 3-7 days that let them cycle straight back in.
BENCH_DAYS = 45

# Once benched, they can return after BENCH_DAYS -- but only if the model
# rates them highly again, and the counter starts over from their next pick.


def _norm(name):
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def get_chronic_missers(kind="hr_prop", as_of=None):
    """Normalized names to bench for chronic failure.

    Looks only at picks graded within the BENCH_DAYS window, so the bench
    expires on its own and a player who genuinely turns a corner can come
    back rather than being blacklisted for the season."""
    as_of = as_of or datetime.now().date()
    cutoff = (as_of - timedelta(days=BENCH_DAYS)).strftime("%Y-%m-%d")

    try:
        conn = sqlite3.connect(str(config.DB_PATH))
        rows = conn.execute(
            "SELECT side_or_player, status FROM recommendations "
            "WHERE kind=? AND status IN ('won','lost') AND date >= ?",
            (kind, cutoff),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("Chronic-miss read failed (%s) -- no performance bench this run.", exc)
        return set()

    tally = {}
    display = {}
    for name, status in rows:
        key = _norm(name)
        if not key:
            continue
        display.setdefault(key, name)
        rec = tally.setdefault(key, {"n": 0, "w": 0})
        rec["n"] += 1
        if status == "won":
            rec["w"] += 1

    benched = set()
    for key, rec in tally.items():
        n, w = rec["n"], rec["w"]
        if n < MIN_SAMPLE:
            continue
        if w == 0 and n >= ZERO_FOR_N:
            benched.add(key)
            logger.info("PERF-BENCH: %s is %d-for-%d over the last %d days -- benched.",
                        display[key], w, n, BENCH_DAYS)
            continue
        if (w / n) < MIN_HIT_RATE:
            benched.add(key)
            logger.info("PERF-BENCH: %s hit %d of %d (%.0f%%) -- below the %.0f%% floor, benched.",
                        display[key], w, n, 100 * w / n, 100 * MIN_HIT_RATE)

    if benched:
        logger.info("PERF-BENCH: %d player(s) benched for chronic misses: %s",
                    len(benched), ", ".join(sorted(display[k] for k in benched)))
    return benched

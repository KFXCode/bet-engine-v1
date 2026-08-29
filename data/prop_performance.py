"""
data/prop_performance.py
=========================
Long-memory bench list for prop picks: players this system has repeatedly
picked who have repeatedly FAILED to deliver.

WHY (Aug 29, 2026): short rotation only looked back 3-7 days, so a name could
always cycle back once its cooldown expired -- forever. The ledger showed
Ben Rice 14 picks / 1 hit, Kazuma Okamoto 13 / 0, Pete Alonso 12 / 0. Those
three were 29% of every HR pick ever made and went a combined 1-for-39, out of
47 different players used. That is a model failure, not variance.

TIGHTENED: the first version needed 4 graded picks and forgot anything older
than 45 days, which let a chronic misser quietly reset and return. Now:
  - 0-for-3 is enough to bench (ZERO_FOR_N = 3).
  - Memory runs SEASON-LONG, so a bad history doesn't age out mid-season.
  - Whether a price was ever recorded is irrelevant -- a loss is a loss. The
    result is the only thing that counts here.

Still conservative where it should be: 0-for-2 is ordinary variance on a home
run bet and is NOT evidence. The bench only fires once continuing to pick
someone is a model failure rather than bad luck.

Reads the DB directly (like data/recent_form.py) so callers don't thread a
connection through. Never raises -- returns an empty set on any problem, so a
bad read can't stop the daily board being built.
"""

import logging
import re
import sqlite3
import unicodedata
from datetime import datetime

import config

logger = logging.getLogger("prop_performance")

# Graded picks needed before results mean anything.
MIN_SAMPLE = 3

# Bench anyone below this hit rate once MIN_SAMPLE is reached. A decent
# anytime-HR bet should cash 12-20%; below 10% is indefensible to keep taking.
MIN_HIT_RATE = 0.10

# Hard rule: this many graded picks with ZERO hits benches you outright.
ZERO_FOR_N = 3

# Season-long memory. The old 45-day window let chronic missers reset and
# come straight back, which is exactly the behaviour being fixed.
SEASON_START_MONTH_DAY = (3, 1)


def _season_start(as_of):
    m, d = SEASON_START_MONTH_DAY
    year = as_of.year if (as_of.month, as_of.day) >= (m, d) else as_of.year - 1
    return f"{year:04d}-{m:02d}-{d:02d}"


def _norm(name):
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def get_chronic_missers(kind="hr_prop", as_of=None):
    """Normalized names to bench for chronic failure, season to date."""
    as_of = as_of or datetime.now().date()
    cutoff = _season_start(as_of)

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
            logger.info("PERF-BENCH: %s is 0-for-%d this season -- benched.", display[key], n)
            continue
        if (w / n) < MIN_HIT_RATE:
            benched.add(key)
            logger.info("PERF-BENCH: %s hit %d of %d (%.0f%%) -- below the %.0f%% floor, benched.",
                        display[key], w, n, 100 * w / n, 100 * MIN_HIT_RATE)

    if benched:
        logger.info("PERF-BENCH: %d player(s) benched season-to-date: %s",
                    len(benched), ", ".join(sorted(display[k] for k in benched)))
    return benched

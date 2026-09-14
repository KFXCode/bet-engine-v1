"""
auto_gate.py
=============
Decides WHEN the automatic (cloud) run should publish, so one frequent cron
schedule produces "update ahead of today's first game" behaviour whether that
game is at noon or 8pm -- without hard-coding either time.

Used by run_daily.py's `--auto` flag. A plain `python run_daily.py` ignores
all of this and runs immediately.

It anchors on the EARLIEST game across every in-season sport, not baseball --
run_daily passes the full merged slate, so an NFL 1pm kickoff correctly drives
the publish even when the first MLB game isn't until 7pm.

THE GRACE WINDOW (Sep 14, 2026) -- why publishing kept landing late.
The gate published when `now >= target`, where target = first game minus 60
minutes. With HOURLY cron checks that quietly fails for any game that doesn't
start exactly on the hour:

    Game 1:05pm -> target 12:05pm
    check 12:00  ->  12:00 < 12:05  ->  SKIP
    check 13:00  ->  13:00 > 12:05  ->  PUBLISH, five minutes before kickoff

The intent was "an hour of warning"; the delivery was five minutes, and
nothing logged an error because the gate did exactly what it was told. Any
start time between :01 and :59 past the hour hit this.

So the window now OPENS one full check-interval before the target. The gate
fires at the first check inside that window, which guarantees publishing at
least AUTO_RUN_LEAD_MINUTES before the first game rather than at most. Same
example: the window opens 11:05, the 12:00 check publishes, 65 minutes of
warning. Publishing slightly early costs nothing -- odds are cached, and the
board locks on first publish and only refines before the game.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

MARKER_PATH = config.DATA_STORE_DIR / "last_published.txt"

# How far apart the scheduled checks are (.github/workflows/daily.yml runs
# hourly). The grace window matches it, so a start time anywhere inside the
# hour still gets the full intended lead.
CHECK_INTERVAL_MINUTES = int(getattr(config, "AUTO_CHECK_INTERVAL_MINUTES", 60))


def already_published_today(date_str):
    if not MARKER_PATH.exists():
        return False
    return MARKER_PATH.read_text().strip() == date_str


def mark_published(date_str):
    MARKER_PATH.write_text(date_str)


def _local_now():
    return datetime.now(ZoneInfo(config.TIMEZONE))


def _parse_game_time_utc(iso_str):
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    return datetime.fromisoformat(iso_str)


def earliest_game_local(run_date, games):
    """Kickoff/first pitch of the day's FIRST game across all sports, in local
    time, or None when nothing on the slate carries a start time."""
    tz = ZoneInfo(config.TIMEZONE)
    starts = []
    for g in games:
        if not g.game_time_utc:
            continue
        try:
            starts.append(_parse_game_time_utc(g.game_time_utc))
        except Exception:
            continue
    if not starts:
        return None
    return min(starts).astimezone(tz)


def compute_target_publish_time(run_date, games):
    """Earliest game today minus the configured lead, in config.TIMEZONE.
    Falls back to the fixed DAILY_RUN_HOUR/MINUTE when there's no schedule to
    anchor to (off day, or every game missing its time)."""
    tz = ZoneInfo(config.TIMEZONE)
    fallback = datetime(run_date.year, run_date.month, run_date.day,
                         config.DAILY_RUN_HOUR, config.DAILY_RUN_MINUTE, tzinfo=tz)

    earliest = earliest_game_local(run_date, games)
    if earliest is None:
        return fallback
    return earliest - timedelta(minutes=config.AUTO_RUN_LEAD_MINUTES)


def should_run_now(run_date, date_str, games):
    """Returns (should_run: bool, reason: str)."""
    if already_published_today(date_str):
        return False, f"Already published today's report ({date_str})."

    target = compute_target_publish_time(run_date, games)
    # Open the window a full check-interval early -- see the module docstring.
    window_open = target - timedelta(minutes=CHECK_INTERVAL_MINUTES)
    now = _local_now()

    earliest = earliest_game_local(run_date, games)
    first_game_str = earliest.strftime("%-I:%M %p %Z") if earliest else "n/a"
    window_str = window_open.strftime("%-I:%M %p %Z")

    if now >= window_open:
        if earliest:
            lead = int((earliest - now).total_seconds() // 60)
            return True, (f"Publishing now -- first game {first_game_str}, "
                          f"{lead} min of lead time.")
        return True, f"Publishing now -- no game times available, using the {window_str} fallback."

    return False, (f"Not yet -- first game {first_game_str}, publish window opens "
                   f"{window_str}. Next scheduled check will re-evaluate.")

"""
output/publish_whop.py
=======================
Posts the day's picks straight into your Whop community as a forum post.

WHY: the picks were on GitHub Pages, which is PUBLIC -- anyone holding the URL
could read them whether they paid or not. Posting into Whop puts the content
behind the membership wall, so access ends when a subscription does.

ENDPOINT (fixed Sep 3, 2026): the correct path is `/forum_posts` with an
UNDERSCORE, on base https://api.whop.com/api/v1. This file originally used
`/forum-posts` with a hyphen, which 404s -- the run reported success, the log
line said the post failed, and nothing ever appeared in the feed. Verified
against the published OpenAPI spec: POST /forum_posts, body requires
experience_id, optional content (Markdown), title, is_mention.

The API key must carry the `forum:post:create` permission, and experience_id
must be the FORUM experience you want to post in (the Picks Feed app), not the
whop or product id.

MLB IS MONEYLINE-ONLY (Sep 3, 2026). HR props are retired, so nothing here
renders or tallies them. NFL anytime-TD props and NCAAF totals are unaffected.

FORMATTING NOTES (the whole point of this file): a forum post is read on a
phone, in a feed, next to everything else in someone's day. So the layout is
built to be SCANNED, not studied:
  - The bet itself is the first thing on the line, in bold, with its price.
  - The single number that matters (edge / model %) sits right after it.
  - Reasoning is capped at two lines per pick. The engine generates six or
    more; dumping all of them turns the post into a wall nobody finishes.
  - Sections only appear when they have content -- no "none today" filler.
  - Parlays come last, because they're optional add-ons to straight plays.

Two repo secrets:
    WHOP_API_KEY        account API key with forum:post:create
    WHOP_EXPERIENCE_ID  the forum experience to post into (exp_xxxxx)

Missing either one makes this a no-op. Posting never raises: a Whop outage
must not break the daily run.
"""

import logging
import os

import requests

logger = logging.getLogger("publish_whop")

API_URL = "https://api.whop.com/api/v1/forum_posts"
TIMEOUT = 20

# Two lines of "why" per pick. Enough to justify the bet, short enough to read.
MAX_REASONS = 2

SPORT_ICON = {
    "MLB": "⚾", "NFL": "🏈", "NCAAF": "🏈", "NBA": "🏀",
    "NCAAB": "🏀", "NHL": "🏒", "WNBA": "🏀",
}


def _american(v):
    return f"{v:+d}" if isinstance(v, int) else "n/a"


def _reasons(items):
    """Trim the engine's full reasoning list to the few that read well, and
    drop the internal bookkeeping lines that mean nothing to a member."""
    out = []
    for r in (items or []):
        text = str(r).strip()
        if not text:
            continue
        low = text.lower()
        if low.startswith(("final ", "base ", "starting score")):
            continue
        if "score:" in low and "/100" in low:
            continue
        out.append(text)
        if len(out) >= MAX_REASONS:
            break
    return out


def _pick_block(headline, reasons, sub=None):
    lines = [headline]
    if sub:
        lines.append(f"  _{sub}_")
    for r in reasons:
        lines.append(f"  ↳ {r}")
    lines.append("")
    return lines


def _moneyline_section(plays, sport):
    rows = sorted([p for p in plays if getattr(p, "sport", None) == sport],
                  key=lambda p: p.edge_pct, reverse=True)
    if not rows:
        return []
    icon = SPORT_ICON.get(sport, "•")
    out = [f"### {icon} {sport} — Moneyline", ""]
    for i, p in enumerate(rows, 1):
        tag = " · **TOP PLAY**" if i == 1 else ""
        out.extend(_pick_block(
            f"**{p.team} ML {_american(p.odds_american)}** — {p.edge_pct * 100:.1f}% edge{tag}",
            _reasons(p.reasoning)))
    return out


def _td_section(props):
    if not props:
        return []
    out = ["### 🏈 NFL — Anytime Touchdown", ""]
    for i, c in enumerate(props, 1):
        tag = " · **TOP PROP**" if i == 1 else ""
        out.extend(_pick_block(
            f"**{c['player_name']} anytime TD {_american(c.get('odds_american'))}** — "
            f"{c['model_prob'] * 100:.0f}% model{tag}",
            _reasons(c.get("reasoning")),
            sub=f"{c['position']} · {c['team']} vs {c['opponent']}"))
    return out


def _totals_section(totals):
    if not totals:
        return []
    out = ["### 📊 Totals", ""]
    for c in totals:
        out.extend(_pick_block(
            f"**{c['side'].title()} {c['line']:.1f} — {c['matchup']}** — "
            f"{c['edge_pct'] * 100:.1f}% edge",
            [f"Projected {c['projected']:.1f} vs a {c['line']:.1f} line."]))
    return out


def _parlay_section(parlay, title, note):
    if not parlay or not parlay.get("legs"):
        return []
    out = [f"### {title} — {_american(parlay.get('combined_odds_american'))}", ""]
    for leg in parlay["legs"]:
        out.append(f"- {leg.get('label')}")
    out.extend(["", f"_{note}_", ""])
    return out


def build_markdown(report):
    """Turn a DailyReport into the Markdown body of the Whop post."""
    plays = report.plays or []
    td_props = getattr(report, "td_props", []) or []
    totals = getattr(report, "totals", []) or []
    has_picks = bool(plays or td_props or totals)

    lines = []

    if not has_picks:
        lines.append("**No qualifying plays today.**")
        lines.append("")
        lines.append("Nothing on the slate cleared a real edge. Standing down is the system "
                     "working as designed — no filler bets.")
        lines.append("")
    else:
        total = len(plays) + len(td_props) + len(totals)
        lines.append(f"**{total} play{'s' if total != 1 else ''} today** · "
                     f"{report.slate_size} game{'s' if report.slate_size != 1 else ''} analyzed · "
                     f"flat 1 unit each")
        lines.append("")

        for sport in (report.active_sports or []):
            lines.extend(_moneyline_section(plays, sport))
        lines.extend(_td_section(td_props))
        lines.extend(_totals_section(totals))

        lines.extend(_parlay_section(
            getattr(report, "double_parlay", {}), "💵 Double Your Money",
            "The two safest plays on the board, combined for roughly a 2x return."))
        lines.extend(_parlay_section(
            getattr(report, "top_parlay", {}), "🎯 Top Parlay",
            "Optional. Every leg is already a straight play above — parlay only if you want the swing."))

    bank = report.bankroll_summary or {}
    if bank.get("wins") or bank.get("losses"):
        lines.append("---")
        lines.append(f"📈 **Moneyline record** — `{bank.get('wins', 0)}-{bank.get('losses', 0)}`")
        lines.append("")

    cel = report.celestial or {}
    lines.append(f"_{cel.get('phase', '')} in {cel.get('sign', '')} · "
                 f"numerology {(report.numerology or {}).get('number', '')}_")
    lines.append("")
    lines.append("_For entertainment and informational purposes only. Sports betting involves "
                 "risk; past performance does not guarantee future results. You are responsible "
                 "for your own wagering decisions._")

    return "\n".join(lines)


def publish_to_whop(report):
    """Post today's picks into the Whop forum. Returns a status dict and never
    raises -- a Whop failure must not break the daily run."""
    api_key = os.getenv("WHOP_API_KEY", "").strip()
    experience_id = os.getenv("WHOP_EXPERIENCE_ID", "").strip()

    if not api_key or not experience_id:
        logger.info("Whop publishing skipped (WHOP_API_KEY / WHOP_EXPERIENCE_ID not set).")
        return {"published": False, "reason": "not_configured"}

    body = {
        "experience_id": experience_id,
        "title": f"Daily Picks — {report.date}",
        "content": build_markdown(report),
        "is_mention": True,   # notify members the board is live
    }

    logger.info("Whop: posting to %s (experience %s).", API_URL, experience_id)
    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=body, timeout=TIMEOUT)
        if resp.status_code >= 400:
            # Print the API's own message -- it names the exact problem
            # (bad experience id, missing forum:post:create scope, etc.).
            logger.error("Whop post FAILED (HTTP %s): %s", resp.status_code, resp.text[:500])
            if resp.status_code == 401:
                logger.error("  -> 401 means the WHOP_API_KEY secret is wrong or revoked.")
            elif resp.status_code == 403:
                logger.error("  -> 403 means the key lacks the 'forum:post:create' permission.")
            elif resp.status_code == 404:
                logger.error("  -> 404 means WHOP_EXPERIENCE_ID isn't a forum experience you own.")
            return {"published": False, "reason": f"http_{resp.status_code}"}
        post_id = (resp.json() or {}).get("id")
        logger.info("Whop post SUCCEEDED -- picks are live in the forum (post %s).", post_id)
        return {"published": True, "post_id": post_id}
    except Exception as exc:
        logger.error("Whop post error: %s", exc)
        return {"published": False, "reason": str(exc)}

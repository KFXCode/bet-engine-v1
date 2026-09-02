"""
output/publish_whop.py
=======================
Posts the day's picks straight into your Whop community as a forum post.

WHY THIS EXISTS: the picks were published to GitHub Pages, which is PUBLIC
hosting -- anyone holding the URL could read them whether they paid or not,
and a screenshotted link kept working forever. Rotating the URL weekly only
shrinks that window; it never closes it. Posting into Whop removes the link
from the equation entirely: the content lives behind Whop's own membership
wall, so when a subscription lapses, access ends with it. There is no URL left
to leak.

Auth: a COMPANY API KEY from your Whop dashboard (Settings -> API keys), sent
as `Authorization: Bearer <key>`. Set two repo secrets:

    WHOP_API_KEY        biz-scoped API key
    WHOP_EXPERIENCE_ID  the forum experience to post into (exp_xxxxx)

If either is missing this module quietly does nothing, so the pipeline keeps
working exactly as before until you're ready to switch over.

Safety: posting is best-effort and never raises. A Whop outage must not break
the daily run -- picks are still written to disk and (optionally) Pages.
"""

import logging
import os

import requests

logger = logging.getLogger("publish_whop")

API_URL = "https://api.whop.com/api/v1/forum-posts"
TIMEOUT = 20


def _american(v):
    return f"{v:+d}" if isinstance(v, int) else "n/a"


def _fmt_plays(plays, sport):
    rows = [p for p in plays if getattr(p, "sport", None) == sport]
    if not rows:
        return []
    rows.sort(key=lambda p: p.edge_pct, reverse=True)
    out = [f"**{sport} — Moneyline**", ""]
    for i, p in enumerate(rows, 1):
        conf = max(1, min(10, round(p.edge_pct * 100) + 2))
        star = " ⭐" if i == 1 else ""
        out.append(f"{i}. **{p.team} ML {_american(p.odds_american)}**{star} — "
                   f"{p.edge_pct * 100:.1f}% edge · confidence {conf}/10")
        for reason in (p.reasoning or [])[:3]:
            out.append(f"   - {reason}")
        out.append("")
    return out


def _fmt_props(props, heading, line_fn):
    if not props:
        return []
    out = [f"**{heading}**", ""]
    for i, c in enumerate(props, 1):
        star = " ⭐" if i == 1 else ""
        out.append(f"{i}. {line_fn(c)}{star}")
        for reason in (c.get("reasoning") or [])[:3]:
            out.append(f"   - {reason}")
        out.append("")
    return out


def _fmt_parlay(parlay, heading):
    if not parlay or not parlay.get("legs"):
        return []
    out = [f"**{heading}** — combined {_american(parlay.get('combined_odds_american'))}", ""]
    for i, leg in enumerate(parlay["legs"], 1):
        out.append(f"{i}. {leg.get('label')}")
    out.append("")
    return out


def build_markdown(report):
    """Turn a DailyReport into the Markdown body of the Whop post."""
    lines = []

    cel = report.celestial or {}
    num = report.numerology or {}
    lines.append(f"*{report.slate_size} game(s) on the slate · Moon: {cel.get('phase', '?')} "
                 f"in {cel.get('sign', '?')} · Numerology {num.get('number', '?')}*")
    lines.append("")

    if report.data_warnings:
        lines.append("> **Heads up**")
        for w in report.data_warnings[:4]:
            lines.append(f"> - {w}")
        lines.append("")

    for sport in (report.active_sports or []):
        lines.extend(_fmt_plays(report.plays or [], sport))

    lines.extend(_fmt_props(
        getattr(report, "td_props", []) or [], "NFL — Anytime TD",
        lambda c: (f"**{c['player_name']} ({c['position']}, {c['team']}) anytime TD "
                   f"{_american(c.get('odds_american'))}** — {c['model_prob'] * 100:.0f}% model")))

    lines.extend(_fmt_props(
        getattr(report, "totals", []) or [], "Totals",
        lambda c: (f"**{c['side'].title()} {c['line']:.1f} — {c['matchup']}** "
                   f"({c['edge_pct'] * 100:.1f}% edge, projected {c['projected']:.1f})")))

    lines.extend(_fmt_parlay(getattr(report, "double_parlay", {}), "💵 Double Your Money"))
    lines.extend(_fmt_parlay(getattr(report, "top_parlay", {}), "🎯 Top Parlay"))

    bank = report.bankroll_summary or {}
    if bank:
        lines.append("---")
        lines.append(f"**Record to date** — Moneyline {bank.get('wins', 0)}-{bank.get('losses', 0)}"
                     f" · Props {bank.get('hr_wins', 0)}-{bank.get('hr_losses', 0)}")
        lines.append("")

    lines.append("*Flat 1 unit per play. For entertainment and informational purposes only. "
                 "Sports betting involves risk; past performance does not guarantee future "
                 "results. You are responsible for your own wagering decisions.*")

    if not (report.plays or getattr(report, "td_props", []) or getattr(report, "totals", [])):
        lines.insert(0, "**No qualifying plays today.** Nothing cleared a real edge — "
                        "standing down is the system working as designed.\n")

    return "\n".join(lines)


def publish_to_whop(report):
    """Post today's picks into the Whop forum. Returns a small status dict and
    never raises -- a Whop failure must not break the daily run."""
    api_key = os.getenv("WHOP_API_KEY", "").strip()
    experience_id = os.getenv("WHOP_EXPERIENCE_ID", "").strip()

    if not api_key or not experience_id:
        logger.info("Whop publishing skipped (WHOP_API_KEY / WHOP_EXPERIENCE_ID not set).")
        return {"published": False, "reason": "not_configured"}

    body = {
        "experience_id": experience_id,
        "title": f"Daily Picks — {report.date}",
        "content": build_markdown(report),
        "is_mention": True,   # notify members that the board is live
    }

    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=body, timeout=TIMEOUT)
        if resp.status_code >= 400:
            logger.error("Whop post failed (%s): %s", resp.status_code, resp.text[:300])
            return {"published": False, "reason": f"http_{resp.status_code}"}
        post_id = (resp.json() or {}).get("id")
        logger.info("Published picks to Whop forum (post %s).", post_id)
        return {"published": True, "post_id": post_id}
    except Exception as exc:
        logger.error("Whop post error: %s", exc)
        return {"published": False, "reason": str(exc)}

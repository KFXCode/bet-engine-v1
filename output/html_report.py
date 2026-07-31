"""
output/html_report.py
=======================
Renders the dark sportsbook-style, mobile/PWA-ready HTML daily report.
Writes report_<date>.html (archive) and latest.html (always overwritten).
"""

import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

import config

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
PWA_ASSETS = ["manifest.json", "icon-180.png", "icon-192.png", "icon-512.png"]

_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


def render_daily_report(report):
    template = _env.get_template("daily_report.html")
    generated_at = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %-d, %Y - %-I:%M %p %Z")
    html = template.render(
        date=report.date,
        generated_at=generated_at,
        slate_size=report.slate_size,
        celestial=report.celestial,
        numerology=report.numerology,
        data_warnings=report.data_warnings,
        plays=report.plays,
        fade_teams=report.fade_teams,
        parlay=report.parlay,
        hr_props=report.hr_props,
        dropped_notes=report.dropped_notes,
        bankroll=report.bankroll_summary,
        results_recap=report.results_recap,
        history=report.history,
        daily_parlay=report.daily_parlay,
        sport_parlays=report.sport_parlays,
        top_parlay=report.top_parlay,
        double_parlay=report.double_parlay,
        active_sports=report.active_sports,
        unit_size=config.FLAT_STAKE_UNITS,
    )
    _ensure_pwa_assets()

    out_path = config.REPORTS_DIR / f"report_{report.date}.html"
    out_path.write_text(html, encoding="utf-8")

    latest_path = config.REPORTS_DIR / "latest.html"
    latest_path.write_text(html, encoding="utf-8")

    return out_path, html


def _ensure_pwa_assets():
    for name in PWA_ASSETS:
        src = config.BASE_DIR / name
        dest = config.REPORTS_DIR / name
        if src.exists() and not dest.exists():
            shutil.copy(src, dest)

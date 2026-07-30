"""
data/weather.py
================
Free game-time weather per ballpark via Open-Meteo (no API key, no signup).
Feeds the HR model's Park+Weather category: warm air + wind blowing OUT to
the outfield = HR boost; cold air + wind blowing IN = HR suppressor.

Never raises -- any network/parse problem returns a neutral result so the
daily run always completes. Domes/closed roofs are forced neutral upstream
(see engine/hr_props.py) since weather can't affect a climate-controlled park.
"""

import logging
import requests

from data.ballparks import ballpark_for

logger = logging.getLogger(__name__)

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def get_park_weather(home_team, game_time_utc=None):
    """Returns a dict:
        {available, temp_f, wind_mph, wind_effect: 'out'|'in'|'neutral',
         summary}
    wind_effect compares the wind's FROM-direction to the park's center-field
    bearing: wind coming FROM behind home plate blows OUT (HR boost); wind
    coming FROM the outfield blows IN (suppressor)."""
    park = ballpark_for(home_team)
    neutral = {"available": False, "temp_f": None, "wind_mph": None,
               "wind_effect": "neutral", "summary": "weather unavailable"}
    if not park:
        return neutral
    if park.get("dome"):
        return {"available": True, "temp_f": 72.0, "wind_mph": 0.0,
                "wind_effect": "neutral", "summary": "dome/closed roof -- climate controlled"}

    try:
        resp = requests.get(OPEN_METEO, params={
            "latitude": park["lat"], "longitude": park["lon"],
            "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "forecast_days": 2, "timezone": "UTC",
        }, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.debug("weather fetch failed for %s: %s", home_team, exc)
        return neutral

    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    winds = hourly.get("wind_speed_10m", [])
    dirs = hourly.get("wind_direction_10m", [])
    if not times or not temps:
        return neutral

    idx = _closest_hour_index(times, game_time_utc)
    temp_f = temps[idx] if idx < len(temps) else None
    wind_mph = winds[idx] if idx < len(winds) else None
    wind_from = dirs[idx] if idx < len(dirs) else None

    wind_effect = _wind_effect(wind_from, wind_mph, park["cf_azimuth_deg"])
    summary = _summary(temp_f, wind_mph, wind_effect)
    return {"available": True, "temp_f": temp_f, "wind_mph": wind_mph,
            "wind_effect": wind_effect, "summary": summary}


def _closest_hour_index(times, game_time_utc):
    if not game_time_utc:
        return min(19, len(times) - 1)  # default ~7pm-ish local first day
    target = game_time_utc.replace("Z", "")[:13]  # 'YYYY-MM-DDTHH'
    for i, t in enumerate(times):
        if t[:13] >= target:
            return i
    return len(times) - 1


def _wind_effect(wind_from_deg, wind_mph, cf_azimuth_deg):
    """wind_from_deg is the compass direction the wind blows FROM. If it comes
    from roughly behind home plate (opposite the CF bearing), it pushes balls
    OUT toward the outfield. If it comes from the outfield (near the CF
    bearing), it pushes them IN. Light wind (<6 mph) is neutral."""
    if wind_from_deg is None or wind_mph is None or wind_mph < 6:
        return "neutral"
    blowing_toward = (wind_from_deg + 180) % 360           # direction wind travels toward
    diff_out = _angle_diff(blowing_toward, cf_azimuth_deg)  # small => blowing toward CF (out)
    diff_in = _angle_diff(wind_from_deg, cf_azimuth_deg)    # small => coming from CF (in)
    if diff_out <= 55:
        return "out"
    if diff_in <= 55:
        return "in"
    return "neutral"


def _angle_diff(a, b):
    d = abs((a - b) % 360)
    return min(d, 360 - d)


def _summary(temp_f, wind_mph, wind_effect):
    parts = []
    if temp_f is not None:
        parts.append(f"{temp_f:.0f}\u00b0F")
    if wind_mph is not None:
        if wind_effect == "out":
            parts.append(f"wind {wind_mph:.0f} mph blowing OUT")
        elif wind_effect == "in":
            parts.append(f"wind {wind_mph:.0f} mph blowing IN")
        else:
            parts.append(f"wind {wind_mph:.0f} mph (neutral)")
    return ", ".join(parts) if parts else "no reading"

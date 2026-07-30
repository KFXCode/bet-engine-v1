"""
data/ballparks.py
==================
Per-team home ballpark data for the HR weather layer:
  - lat/lon           -> feed the free Open-Meteo weather API (no key)
  - dome              -> True if roofed/climate-controlled; weather is
                         treated as neutral (no temp/wind effect on HRs)
  - cf_azimuth_deg    -> compass bearing (0=N, 90=E, 180=S, 270=W) from home
                         plate toward center field. Used to decide whether the
                         day's wind is blowing OUT toward the outfield (HR
                         boost) or IN toward the plate (HR suppressor):
                         compare wind-FROM direction to this bearing.

Keyed by the same team abbreviations as data/teams.py. Orientation values are
the generally-accepted field bearings for each park; small errors don't matter
because the wind effect is bucketed (out / neutral / in), not continuous.
"""

BALLPARKS = {
    "ARI": {"lat": 33.4455, "lon": -112.0667, "dome": True,  "cf_azimuth_deg": 0},
    "ATL": {"lat": 33.8907, "lon": -84.4677,  "dome": False, "cf_azimuth_deg": 25},
    "BAL": {"lat": 39.2839, "lon": -76.6217,  "dome": False, "cf_azimuth_deg": 20},
    "BOS": {"lat": 42.3467, "lon": -71.0972,  "dome": False, "cf_azimuth_deg": 45},
    "CHC": {"lat": 41.9484, "lon": -87.6553,  "dome": False, "cf_azimuth_deg": 30},
    "CWS": {"lat": 41.8300, "lon": -87.6339,  "dome": False, "cf_azimuth_deg": 5},
    "CIN": {"lat": 39.0975, "lon": -84.5069,  "dome": False, "cf_azimuth_deg": 20},
    "CLE": {"lat": 41.4962, "lon": -81.6852,  "dome": False, "cf_azimuth_deg": 0},
    "COL": {"lat": 39.7559, "lon": -104.9942, "dome": False, "cf_azimuth_deg": 0},
    "DET": {"lat": 42.3390, "lon": -83.0485,  "dome": False, "cf_azimuth_deg": 25},
    "HOU": {"lat": 29.7573, "lon": -95.3555,  "dome": True,  "cf_azimuth_deg": 345},
    "KC":  {"lat": 39.0517, "lon": -94.4803,  "dome": False, "cf_azimuth_deg": 0},
    "LAA": {"lat": 33.8003, "lon": -117.8827, "dome": False, "cf_azimuth_deg": 40},
    "LAD": {"lat": 34.0739, "lon": -118.2400, "dome": False, "cf_azimuth_deg": 25},
    "MIA": {"lat": 25.7781, "lon": -80.2197,  "dome": True,  "cf_azimuth_deg": 40},
    "MIL": {"lat": 43.0280, "lon": -87.9712,  "dome": True,  "cf_azimuth_deg": 0},
    "MIN": {"lat": 44.9817, "lon": -93.2777,  "dome": False, "cf_azimuth_deg": 0},
    "NYM": {"lat": 40.7571, "lon": -73.8458,  "dome": False, "cf_azimuth_deg": 25},
    "NYY": {"lat": 40.8296, "lon": -73.9262,  "dome": False, "cf_azimuth_deg": 0},
    "OAK": {"lat": 37.7516, "lon": -122.2005, "dome": False, "cf_azimuth_deg": 60},
    "PHI": {"lat": 39.9061, "lon": -75.1665,  "dome": False, "cf_azimuth_deg": 0},
    "PIT": {"lat": 40.4469, "lon": -80.0057,  "dome": False, "cf_azimuth_deg": 60},
    "SD":  {"lat": 32.7073, "lon": -117.1566, "dome": False, "cf_azimuth_deg": 0},
    "SF":  {"lat": 37.7786, "lon": -122.3893, "dome": False, "cf_azimuth_deg": 60},
    "SEA": {"lat": 47.5914, "lon": -122.3325, "dome": True,  "cf_azimuth_deg": 0},
    "STL": {"lat": 38.6226, "lon": -90.1928,  "dome": False, "cf_azimuth_deg": 0},
    "TB":  {"lat": 27.7683, "lon": -82.6534,  "dome": True,  "cf_azimuth_deg": 0},
    "TEX": {"lat": 32.7473, "lon": -97.0847,  "dome": True,  "cf_azimuth_deg": 0},
    "TOR": {"lat": 43.6414, "lon": -79.3894,  "dome": True,  "cf_azimuth_deg": 0},
    "WSH": {"lat": 38.8730, "lon": -77.0074,  "dome": False, "cf_azimuth_deg": 30},
}


def ballpark_for(team_abbr):
    return BALLPARKS.get(team_abbr)

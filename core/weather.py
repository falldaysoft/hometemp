"""Outdoor weather from Open-Meteo (free, no key; https://open-meteo.com)."""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .models import WEATHER_FIELDS, HomeSettings, WeatherHour, WeatherNow

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
SNAPSHOT_HOURS = 6  # keep a forecast snapshot every 6 hours (00, 06, 12, 18 UTC)
CURRENT_FIELDS = ["temperature_2m", "relative_humidity_2m", "apparent_temperature", "precipitation", "snowfall",
                  "weather_code", "cloud_cover", "pressure_msl", "wind_speed_10m", "wind_direction_10m",
                  "wind_gusts_10m"]


def get_json(url, params):
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=20) as r:
        return json.loads(r.read())


def geocode(query):
    if not query.strip():
        return []
    data = get_json(GEOCODE_URL, {"name": query, "count": 6, "language": "en", "format": "json"})
    return [{
        "label": ", ".join(p for p in (r.get("name"), r.get("admin1"), r.get("country")) if p),
        "latitude": r["latitude"], "longitude": r["longitude"], "timezone": r.get("timezone", ""),
    } for r in data.get("results", [])]


def fetch(settings=None, past_days=2, now=None):
    """Record current conditions, upsert actual hours up to now, and snapshot the forecast if due."""
    settings = settings or HomeSettings.load()
    if not settings.has_location:
        return {"skipped": "no location set"}
    now = now or datetime.now(timezone.utc)
    data = get_json(FORECAST_URL, {
        "latitude": settings.latitude, "longitude": settings.longitude,
        "hourly": ",".join(WEATHER_FIELDS), "current": ",".join(CURRENT_FIELDS),
        "past_days": past_days, "forecast_days": 7, "timezone": "GMT", "timeformat": "unixtime",
    })
    return store(data, now)


def store(data, now):
    stats = {"actual": 0, "forecast": 0, "current": 0}
    cur = data.get("current") or {}
    if "time" in cur:
        ts = datetime.fromtimestamp(cur["time"], timezone.utc)
        values = {k: v for k, v in cur.items() if k not in ("time", "interval")}
        WeatherNow.objects.update_or_create(ts=ts, defaults={"temperature": values.get("temperature_2m"),
                                                             "data": values})
        stats["current"] = 1

    hourly = data.get("hourly") or {}
    times = hourly.get("time", [])
    issued = now.replace(minute=0, second=0, microsecond=0)
    issued -= timedelta(hours=issued.hour % SNAPSHOT_HOURS)
    snapshot_due = not WeatherHour.objects.filter(issued=issued).exists()
    forecast_rows = []
    for i, t in enumerate(times):
        ts = datetime.fromtimestamp(t, timezone.utc)
        values = {k: hourly[k][i] for k in WEATHER_FIELDS if k in hourly}
        temp = values.get("temperature_2m")
        if ts <= now:
            WeatherHour.objects.update_or_create(ts=ts, issued=None, defaults={"temperature": temp, "data": values})
            stats["actual"] += 1
        elif snapshot_due:
            forecast_rows.append(WeatherHour(ts=ts, issued=issued, temperature=temp, data=values))
    WeatherHour.objects.bulk_create(forecast_rows, ignore_conflicts=True)
    stats["forecast"] = len(forecast_rows)
    return stats

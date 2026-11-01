"""Data assembly for the dashboard and accessory sheets."""
import statistics
from collections import defaultdict
from datetime import timedelta

from django.db.models import OuterRef, Subquery
from django.utils import timezone

from .models import Accessory, Characteristic, Reading, Zone

STALE_AFTER = timedelta(minutes=15)
DEBOUNCE = timedelta(seconds=60)

LABELS = {
    "currentTemperature": "Temperature",
    "targetTemperature": "Target temperature",
    "currentHeaterCoolerState": "Running",
    "targetHeaterCoolerState": "Mode",
    "currentHeatingCooling": "Running",
    "targetHeatingCooling": "Mode",
    "heatingThreshold": "Heat to",
    "coolingThreshold": "Cool to",
    "currentRelativeHumidity": "Humidity",
    "targetRelativeHumidity": "Target humidity",
    "carbonDioxideLevel": "CO₂",
    "airQuality": "Air quality",
    "pm25Density": "PM2.5",
    "vocDensity": "VOC",
    "batteryLevel": "Battery",
    "statusLowBattery": "Low battery",
    "active": "Power",
    "temperatureUnits": "Display units",
    "currentFanState": "Fan",
}
ORDER = list(LABELS)

ENUMS = {
    "currentHeaterCoolerState": {0: "Off", 1: "Idle", 2: "Heating", 3: "Cooling"},
    "targetHeaterCoolerState": {0: "Auto", 1: "Heat", 2: "Cool"},
    "currentHeatingCooling": {0: "Off", 1: "Heating", 2: "Cooling"},
    "targetHeatingCooling": {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto"},
    "airQuality": {0: "Unknown", 1: "Excellent", 2: "Good", 3: "Fair", 4: "Inferior", 5: "Poor"},
    "statusLowBattery": {0: "No", 1: "Yes"},
    "active": {0: "Off", 1: "On"},
    "temperatureUnits": {0: "Celsius", 1: "Fahrenheit"},
    "currentFanState": {0: "Inactive", 1: "Idle", 2: "Blowing"},
}
UNITS = {"celsius": "°", "percentage": "%"}
KIND_UNITS = {"carbonDioxideLevel": " ppm", "pm25Density": " µg/m³", "vocDensity": " µg/m³",
              "currentRelativeHumidity": "%", "batteryLevel": "%"}

RUN_STATES = {"currentHeaterCoolerState": {2: "heating", 3: "cooling"},
              "currentHeatingCooling": {1: "heating", 2: "cooling"}}


def fmt_value(kind, value, unit=""):
    if value is None:
        return "—"
    if kind in ENUMS:
        return ENUMS[kind].get(int(value), f"Unknown")
    suffix = KIND_UNITS.get(kind) or UNITS.get(unit, "")
    if suffix == "°" or kind in ("heatingThreshold", "coolingThreshold", "targetTemperature"):
        return f"{value:.1f}°"
    return f"{value:.0f}{suffix}" if float(value).is_integer() or suffix in ("%", " ppm") else f"{value:.1f}{suffix}"


def latest_values(accessory_ids=None):
    """{accessory_id: {kind: (value, ts)}} from the newest reading of each characteristic."""
    newest = Reading.objects.filter(characteristic=OuterRef("pk")).order_by("-ts")
    qs = Characteristic.objects.annotate(v=Subquery(newest.values("value")[:1]), t=Subquery(newest.values("ts")[:1]))
    if accessory_ids is not None:
        qs = qs.filter(accessory_id__in=accessory_ids)
    out = defaultdict(dict)
    for r in qs.values("accessory_id", "kind", "v", "t"):
        if r["v"] is not None:
            out[r["accessory_id"]][r["kind"]] = (r["v"], r["t"])
    return out


def series(characteristic_ids, since, bucket=timedelta(minutes=10), offset_by_char=None):
    """{characteristic_id: [(ts, value)]} averaged into buckets."""
    rows = Reading.objects.filter(characteristic_id__in=characteristic_ids, ts__gte=since).order_by("ts")
    buckets = defaultdict(lambda: defaultdict(list))
    for cid, ts, v in rows.values_list("characteristic_id", "ts", "value"):
        key = since + bucket * ((ts - since) // bucket)
        buckets[cid][key].append(v + (offset_by_char or {}).get(cid, 0))
    return {cid: [(k + bucket / 2, sum(vs) / len(vs)) for k, vs in sorted(b.items())] for cid, b in buckets.items()}


def spark_path(points, since, until, w=100.0, h=28.0, lo=None, hi=None):
    if len(points) < 2:
        return ""
    vals = [v for _, v in points]
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi - lo < 1.0:
        mid = (hi + lo) / 2
        lo, hi = mid - 0.5, mid + 0.5
    span = (until - since).total_seconds()
    pts = [(w * (t - since).total_seconds() / span, h - h * (v - lo) / (hi - lo)) for t, v in points]
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def run_intervals(state_readings, kind, until):
    """[(start, end, mode)] from state readings, dropping blips shorter than DEBOUNCE."""
    mapping = RUN_STATES.get(kind, {})
    out, cur = [], None
    for ts, v in state_readings:
        mode = mapping.get(int(v))
        if cur and cur[2] != mode:
            out.append((cur[0], ts, cur[2]))
            cur = None
        if mode and cur is None:
            cur = (ts, None, mode)
    if cur:
        out.append((cur[0], until, cur[2]))
    return [i for i in out if i[1] - i[0] >= DEBOUNCE]


class Tile:
    """Everything a dashboard tile or sheet header needs for one accessory."""

    def __init__(self, acc, vals, now):
        self.acc = acc
        self.vals = vals
        get = lambda k: vals.get(k, (None, None))[0]
        self.get = get
        t = get("currentTemperature")
        self.temp = None if t is None else t + acc.calibration_offset
        self.humidity = get("currentRelativeHumidity")
        self.co2 = get("carbonDioxideLevel")
        self.battery = get("batteryLevel")
        self.low_battery = get("statusLowBattery") == 1
        self.seen = max((t for _, t in vals.values()), default=None)
        self.stale = self.seen is None or now - self.seen > STALE_AFTER or not acc.reachable
        self.is_thermostat = bool({"currentHeaterCoolerState", "currentHeatingCooling"} & set(vals))
        self.state = "off"
        self.mode = None
        self.heat_to = self.cool_to = None
        if self.is_thermostat:
            kind = "currentHeaterCoolerState" if "currentHeaterCoolerState" in vals else "currentHeatingCooling"
            self.state = RUN_STATES[kind].get(int(get(kind) or 0), "idle")
            mk = "targetHeaterCoolerState" if kind == "currentHeaterCoolerState" else "targetHeatingCooling"
            self.mode = ENUMS[mk].get(int(get(mk) or 0))
            if get("active") == 0:
                self.mode, self.state = "Off", "off"
            self.heat_to = get("heatingThreshold") if get("heatingThreshold") is not None else get("targetTemperature")
            self.cool_to = get("coolingThreshold")
        self.spark = ""
        self.delta = None

    @property
    def icon(self):
        if self.is_thermostat:
            return "thermostat"
        if self.acc.role == Accessory.Role.APPLIANCE:
            return "fridge"
        if self.acc.role == Accessory.Role.OUTDOOR:
            return "outdoor"
        if self.co2 is not None:
            return "air"
        return "sensor"

    @property
    def active(self):
        return self.state in ("heating", "cooling")

    @property
    def room_name(self):
        return self.acc.room.name if self.acc.room else "No room"


def dashboard(now=None):
    now = now or timezone.now()
    since = now - timedelta(hours=24)
    accessories = list(Accessory.objects.select_related("homekit_room__zone", "room_override__zone"))
    values = latest_values()
    tiles = [Tile(a, values.get(a.id, {}), now) for a in accessories]
    tiles = [t for t in tiles if t.temp is not None or t.is_thermostat]
    hidden = sorted((t for t in tiles if t.acc.role == Accessory.Role.IGNORE), key=lambda t: t.acc.name)
    tiles = [t for t in tiles if t.acc.role != Accessory.Role.IGNORE]

    temp_chars = dict(Characteristic.objects.filter(kind="currentTemperature", accessory__in=[t.acc for t in tiles])
                      .values_list("accessory_id", "id"))
    offsets = {temp_chars[t.acc.id]: t.acc.calibration_offset for t in tiles if t.acc.id in temp_chars}
    hist = series(list(temp_chars.values()), since, offset_by_char=offsets)
    # Until there's a full day of data, stretch the sparkline over what we have.
    first = min((pts[0][0] for pts in hist.values() if pts), default=since)
    for t in tiles:
        t.spark = spark_path(hist.get(temp_chars.get(t.acc.id), []), max(since, first), now)

    zones = []
    for zone in Zone.objects.all():
        members = [t for t in tiles if t.acc.zone == zone and t.acc.role == Accessory.Role.AMBIENT]
        thermo = next((t for t in members if zone.thermostat_id == t.acc.id), None) or next(
            (t for t in members if t.is_thermostat), None)
        rooms = sorted((t for t in members if t is not thermo), key=lambda t: (t.room_name, t.acc.name))
        for t in rooms:
            if thermo and t.temp is not None and thermo.temp is not None:
                t.delta = t.temp - thermo.temp
        temps = [t.temp for t in members if t.temp is not None and not t.stale]
        zones.append({"zone": zone, "thermostat": thermo, "rooms": rooms,
                      "range": (min(temps), max(temps)) if temps else None})

    appliances = sorted((t for t in tiles if t.acc.role == Accessory.Role.APPLIANCE), key=lambda t: t.acc.name)
    latest = Reading.objects.order_by("-ts").values_list("ts", flat=True).first()
    running = {t.state for z in zones if z["thermostat"] for t in [z["thermostat"]] if t.active}
    return {
        "zones": zones,
        "appliances": appliances,
        "appliance_range": (min(t.temp for t in appliances), max(t.temp for t in appliances)) if appliances else None,
        "outdoor_sensors": [t for t in tiles if t.acc.role == Accessory.Role.OUTDOOR],
        "review": [t for t in tiles if t.acc.role == Accessory.Role.UNCLASSIFIED],
        "hidden": hidden,
        "latest_reading": latest,
        "collector_stale": latest is None or now - latest > STALE_AFTER,
        "outdoor": outdoor(now),
        "today_cost": _today_cost(now),
        "glow": "heating" if "heating" in running else ("cooling" if "cooling" in running else "none"),
        "now": now,
    }


def accessory_detail(acc, now=None, hours=24):
    now = now or timezone.now()
    since = now - timedelta(hours=hours)
    vals = latest_values([acc.id]).get(acc.id, {})
    tile = Tile(acc, vals, now)
    chars = list(acc.characteristics.all())
    by_kind = {c.kind: c for c in chars}

    # Per-characteristic stats from change events (heartbeats only repeat cached values).
    events = defaultdict(list)
    for cid, ts in (Reading.objects.filter(characteristic__in=chars, ts__gte=since, source=Reading.Source.EVENT)
                    .order_by("ts").values_list("characteristic_id", "ts")):
        events[cid].append(ts)
    rows = []
    for c in sorted(chars, key=lambda c: (ORDER.index(c.kind) if c.kind in ORDER else 99, c.kind)):
        v, ts = vals.get(c.kind, (None, None))
        ev = events.get(c.id, [])
        gaps = [(b - a).total_seconds() for a, b in zip(ev, ev[1:])]
        rows.append({
            "label": LABELS.get(c.kind) or c.description or c.kind, "kind": c.kind, "value": fmt_value(c.kind, v, c.unit),
            "raw": v, "ts": ts, "events": len(ev), "typical": statistics.median(gaps) if gaps else None,
            "last_event": ev[-1] if ev else None,
        })

    chart = None
    if "currentTemperature" in by_kind:
        bucket = timedelta(minutes=5) if hours <= 24 else timedelta(hours=1)
        cid = by_kind["currentTemperature"].id
        temp = series([cid], since, bucket, {cid: acc.calibration_offset}).get(cid, [])
        setpoint = []
        if "heatingThreshold" in by_kind:
            setpoint = list(Reading.objects.filter(characteristic=by_kind["heatingThreshold"], ts__gte=since)
                            .order_by("ts").values_list("ts", "value"))
        from .models import HvacInterval
        bands = [(max(r.start, since), r.end, r.mode)
                 for r in HvacInterval.objects.filter(thermostat=acc, end__gte=since)]
        first = Reading.objects.filter(characteristic__accessory=acc).order_by("ts").values_list("ts", flat=True).first()
        chart = build_chart(temp, setpoint, bands, max(since, first) if first else since, now)
    return {"tile": tile, "rows": rows, "chart": chart, "hours": hours}


def build_chart(temp, setpoint, bands, since, until, w=640, h=180):
    vals = [v for _, v in temp] + [v for _, v in setpoint]
    if len(temp) < 2:
        return None
    lo, hi = min(vals) - 0.5, max(vals) + 0.5
    span = (until - since).total_seconds()
    x = lambda t: w * max(0, (t - since).total_seconds()) / span
    y = lambda v: h - h * (v - lo) / (hi - lo)
    line = "M" + " L".join(f"{x(t):.1f},{y(v):.1f}" for t, v in temp)
    step = ""
    if setpoint:
        pts = []
        for i, (t, v) in enumerate(setpoint):
            if i:
                pts.append(f"{x(t):.1f},{y(setpoint[i - 1][1]):.1f}")
            pts.append(f"{x(t):.1f},{y(v):.1f}")
        pts.append(f"{w:.1f},{y(setpoint[-1][1]):.1f}")
        step = "M" + " L".join(pts)
    ticks = []
    t = since.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    stride = max(1, round(span / 3600 / 6))
    while t < until:
        if timezone.localtime(t).hour % stride == 0:
            ticks.append({"x": x(t), "label": timezone.localtime(t).strftime("%-I %p").lower()})
        t += timedelta(hours=1)
    run_seconds = sum((b - a).total_seconds() for a, b, _ in bands)
    return {
        "w": w, "h": h, "line": line, "step": step, "ticks": ticks,
        "bands": [{"x": x(a), "w": max(1.5, x(b) - x(a)), "mode": m} for a, b, m in bands],
        "hi": hi - 0.5, "lo": lo + 0.5, "y_hi": y(hi - 0.5), "y_lo": y(lo + 0.5),
        "run_minutes": round(run_seconds / 60), "cycles": len(bands),
    }


WMO = {0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
       51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
       71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers",
       82: "Heavy showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
       96: "Thunderstorm, hail", 99: "Thunderstorm, hail"}

WEATHER_LABELS = [
    ("temperature_2m", "Temperature", "°"), ("apparent_temperature", "Feels like", "°"),
    ("relative_humidity_2m", "Humidity", "%"), ("dew_point_2m", "Dew point", "°"),
    ("wind_speed_10m", "Wind", " km/h"), ("wind_gusts_10m", "Gusts", " km/h"),
    ("wind_direction_10m", "Wind from", "°"), ("cloud_cover", "Cloud cover", "%"),
    ("shortwave_radiation", "Sunlight", " W/m²"), ("precipitation", "Precipitation", " mm"),
    ("snowfall", "Snowfall", " cm"), ("snow_depth", "Snow on the ground", " m"),
    ("pressure_msl", "Pressure", " hPa"),
]


def outdoor(now=None):
    """Current outdoor conditions plus today's range and a 24 h sparkline, or None without a location."""
    from .models import HomeSettings, WeatherHour, WeatherNow
    settings = HomeSettings.load()
    if not settings.has_location:
        return None
    now = now or timezone.now()
    cur = WeatherNow.objects.order_by("-ts").first()
    since = now - timedelta(hours=24)
    actual = list(WeatherHour.objects.filter(issued=None, ts__gte=since, ts__lte=now).order_by("ts")
                  .values_list("ts", "temperature"))
    day_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    issued = WeatherHour.objects.exclude(issued=None).order_by("-issued").values_list("issued", flat=True).first()
    today = [t for _, t in WeatherHour.objects.filter(issued=None, ts__gte=day_start, ts__lte=now)
             .values_list("ts", "temperature") if t is not None]
    today += [t for t in WeatherHour.objects.filter(issued=issued, ts__gt=now, ts__lt=day_end)
              .values_list("temperature", flat=True) if t is not None]
    return {
        "place": settings.place, "now": cur, "temp": cur.temperature if cur else None,
        "summary": WMO.get((cur.data or {}).get("weather_code")) if cur else None,
        "feels": (cur.data or {}).get("apparent_temperature") if cur else None,
        "wind": (cur.data or {}).get("wind_speed_10m") if cur else None,
        "high": max(today) if today else None, "low": min(today) if today else None,
        "spark": spark_path([(t, v) for t, v in actual if v is not None], since, now),
        "stale": cur is None or now - cur.ts > timedelta(minutes=45),
    }


def outdoor_detail(hours=24, now=None):
    from .models import WeatherHour, WeatherNow
    now = now or timezone.now()
    since = now - timedelta(hours=hours)
    ahead = now + timedelta(hours=24 if hours == 24 else 72)
    actual = [(t, v) for t, v in WeatherHour.objects.filter(issued=None, ts__gte=since, ts__lte=now)
              .order_by("ts").values_list("ts", "temperature") if v is not None]
    issued = WeatherHour.objects.exclude(issued=None).order_by("-issued").values_list("issued", flat=True).first()
    forecast = [(t, v) for t, v in WeatherHour.objects.filter(issued=issued, ts__gt=now, ts__lte=ahead)
                .order_by("ts").values_list("ts", "temperature") if v is not None]
    if actual and forecast:
        forecast = [actual[-1]] + forecast
    chart = None
    if len(actual) + len(forecast) >= 2:
        vals = [v for _, v in actual + forecast]
        lo, hi = min(vals) - 1, max(vals) + 1
        w, h = 640, 180
        span = (ahead - since).total_seconds()
        x = lambda t: w * (t - since).total_seconds() / span
        y = lambda v: h - h * (v - lo) / (hi - lo)
        path = lambda pts: ("M" + " L".join(f"{x(t):.1f},{y(v):.1f}" for t, v in pts)) if len(pts) > 1 else ""
        ticks, t = [], since.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        stride = 6 if hours == 24 else 24
        while t < ahead:
            lt = timezone.localtime(t)
            if lt.hour % stride == 0:
                ticks.append({"x": x(t), "label": lt.strftime("%-I %p").lower() if stride < 24 else lt.strftime("%a")})
            t += timedelta(hours=1)
        zero = y(0) if lo < 0 < hi else None
        chart = {"w": w, "h": h, "actual": path(actual), "forecast": path(forecast), "now_x": x(now),
                 "ticks": ticks, "hi": hi - 1, "lo": lo + 1, "y_hi": y(hi - 1), "y_lo": y(lo + 1), "zero": zero}
    cur = WeatherNow.objects.order_by("-ts").first()
    rows = []
    if cur:
        for key, label, unit in WEATHER_LABELS:
            v = (cur.data or {}).get(key)
            if v is None:
                continue
            if key == "wind_direction_10m":
                points = "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split()
                value = f"{points[round(v / 22.5) % 16]} ({v:.0f}°)"
            else:
                value = f"{v:.1f}{unit}" if unit == "°" else f"{v:g}{unit}"
            rows.append({"label": label, "value": value})
        code = (cur.data or {}).get("weather_code")
        if code is not None:
            rows.insert(0, {"label": "Conditions", "value": f"{WMO.get(code, 'Unknown')} ({code})"})
    return {"o": outdoor(now), "chart": chart, "rows": rows, "hours": hours, "issued": issued,
            "counts": {"actual": WeatherHour.objects.filter(issued=None).count(),
                       "snapshots": WeatherHour.objects.exclude(issued=None).values("issued").distinct().count(),
                       "current": WeatherNow.objects.count()}}


def _today_cost(now):
    from .cost import daily
    d = daily(1, now)
    day = d["days"][0]
    if not day["zones"]:
        return {"cost": 0.0, "heat_min": 0.0, "cool_min": 0.0, "estimated": d["estimated"], "any": False}
    return {"cost": day["total"], "heat_min": sum(z["heat_min"] for z in day["zones"].values()),
            "cool_min": sum(z["cool_min"] for z in day["zones"].values()), "estimated": d["estimated"], "any": True}


SERIES_COLORS = ["#30d158", "#bf5af2", "#ffd60a", "#ff375f", "#ac8e68", "#5e5ce6", "#ff9f0a"]


def timeline(zone=None, hours=24, now=None, w=900, h=260):
    """One chart for a zone (or the whole house): every room sensor, the thermostat, its heat setpoint,
    furnace/AC runs as bands, and outdoor temperature on its own right-hand axis."""
    from .models import HvacInterval, WeatherHour
    now = now or timezone.now()
    since = now - timedelta(hours=hours)
    bucket = timedelta(minutes=5) if hours <= 24 else timedelta(minutes=30)

    accs = Accessory.objects.filter(role=Accessory.Role.AMBIENT).select_related(
        "homekit_room__zone", "room_override__zone")
    accs = [a for a in accs if zone is None or a.zone == zone]
    temp_chars = {c.accessory_id: c for c in Characteristic.objects.filter(kind="currentTemperature", accessory__in=accs)}
    offsets = {c.id: next(a.calibration_offset for a in accs if a.id == c.accessory_id) for c in temp_chars.values()}
    data = series([c.id for c in temp_chars.values()], since, bucket, offsets)

    thermo_ids = {a.id for a in accs if a.characteristics.filter(
        kind__in=["currentHeaterCoolerState", "currentHeatingCooling"]).exists()}
    lines = []
    for i, a in enumerate(sorted(accs, key=lambda a: (a.id not in thermo_ids, a.zone.order if a.zone else 9, a.name))):
        c = temp_chars.get(a.id)
        pts = data.get(c.id, []) if c else []
        if len(pts) < 2:
            continue
        lines.append({"name": a.name if a.id in thermo_ids else (a.room.name if a.room else a.name),
                      "sub": a.zone.name if zone is None and a.zone else "",
                      "pts": pts, "thermostat": a.id in thermo_ids,
                      "color": "#ffffff" if a.id in thermo_ids else SERIES_COLORS[len(lines) % len(SERIES_COLORS)],
                      "last": pts[-1][1]})

    setpoints = []
    for c in Characteristic.objects.filter(kind="heatingThreshold", accessory_id__in=thermo_ids):
        prior = Reading.objects.filter(characteristic=c, ts__lt=since).order_by("-ts").values_list("ts", "value").first()
        pts = ([(since, prior[1])] if prior else []) + list(
            Reading.objects.filter(characteristic=c, ts__gte=since).order_by("ts").values_list("ts", "value"))
        if pts:
            setpoints.append(pts)
    runs = HvacInterval.objects.filter(thermostat_id__in=thermo_ids, end__gte=since)
    outdoor_pts = [(t, v) for t, v in WeatherHour.objects.filter(issued=None, ts__gte=since, ts__lte=now)
                   .order_by("ts").values_list("ts", "temperature") if v is not None]

    first = min([l["pts"][0][0] for l in lines] + [p[0][0] for p in setpoints if p], default=since)
    start = max(since, first) if lines else since
    vals = [v for l in lines for _, v in l["pts"]] + [v for p in setpoints for _, v in p]
    if not vals:
        return None
    lo, hi = min(vals) - 0.5, max(vals) + 0.5
    span = (now - start).total_seconds() or 1
    x = lambda t: w * max(0.0, (t - start).total_seconds()) / span
    y = lambda v: h - h * (v - lo) / (hi - lo)
    path = lambda pts: "M" + " L".join(f"{x(t):.1f},{y(v):.1f}" for t, v in pts)
    for l in lines:
        l["d"] = path(l["pts"])
        l["end_y"] = y(l["last"])
    steps = []
    for pts in setpoints:
        seg = []
        for i, (t, v) in enumerate(pts):
            if i:
                seg.append(f"{x(t):.1f},{y(pts[i - 1][1]):.1f}")
            seg.append(f"{x(t):.1f},{y(v):.1f}")
        seg.append(f"{w:.1f},{y(pts[-1][1]):.1f}")
        steps.append("M" + " L".join(seg))

    outdoor_d = outdoor_axis = None
    if len(outdoor_pts) >= 2:
        ovals = [v for _, v in outdoor_pts]
        olo, ohi = min(ovals) - 1, max(ovals) + 1
        oy = lambda v: h - h * (v - olo) / (ohi - olo)
        outdoor_d = "M" + " L".join(f"{x(t):.1f},{oy(v):.1f}" for t, v in outdoor_pts if t >= start)
        outdoor_axis = {"hi": ohi - 1, "lo": olo + 1, "y_hi": oy(ohi - 1), "y_lo": oy(olo + 1)}

    ticks, t = [], start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    stride = max(1, round(span / 3600 / 8))
    stride = next(s for s in (1, 2, 3, 4, 6, 12, 24, 48) if s >= stride)
    while t < now:
        lt = timezone.localtime(t)
        if lt.hour % stride == 0 or stride >= 24 and lt.hour == 0:
            ticks.append({"x": x(t), "label": lt.strftime("%a") if stride >= 24 else lt.strftime("%-I %p").lower()})
        t += timedelta(hours=1)
    grid = []
    step = 1 if hi - lo <= 6 else 2 if hi - lo <= 12 else 5
    g = int(lo) + 1
    while g < hi:
        if g % step == 0:
            grid.append({"y": y(g), "label": g})
        g += 1
    run_min = sum((min(r.end, now) - max(r.start, start)).total_seconds() / 60 for r in runs)
    return {
        "w": w, "h": h, "lines": lines, "steps": steps, "grid": grid, "ticks": ticks,
        "bands": [{"x": x(max(r.start, start)), "w": max(1.5, x(min(r.end, now)) - x(max(r.start, start))),
                   "mode": r.mode, "uncertain": r.uncertain} for r in runs],
        "outdoor": outdoor_d, "outdoor_axis": outdoor_axis, "run_min": run_min, "cycles": runs.count(),
        "start": start,
    }

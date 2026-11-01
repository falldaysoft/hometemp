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
        "outdoor": [t for t in tiles if t.acc.role == Accessory.Role.OUTDOOR],
        "review": [t for t in tiles if t.acc.role == Accessory.Role.UNCLASSIFIED],
        "hidden": hidden,
        "latest_reading": latest,
        "collector_stale": latest is None or now - latest > STALE_AFTER,
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
        bands = []
        for k in ("currentHeaterCoolerState", "currentHeatingCooling"):
            if k in by_kind:
                states = list(Reading.objects.filter(characteristic=by_kind[k], ts__gte=since)
                              .order_by("ts").values_list("ts", "value"))
                bands = run_intervals(states, k, now)
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

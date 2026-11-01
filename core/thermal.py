"""How each zone warms up and loses heat, and when to start heating to be comfortable on time.

Everything is computed from stored readings, so estimates sharpen as data accumulates:
- warm-ups: a heat setpoint step of ≥ 1 °C, timed until the thermostat is within 0.5 °C of the target
- heat loss: furnace-off stretches fitted to Newton's law of cooling, dT/dt = -k (T_in - T_out)
- pre-heat: predicted temperature at the next scheduled start, then the warm-up time needed to reach comfort
"""
import math
import statistics
from datetime import timedelta

from django.utils import timezone

from .models import Accessory, Characteristic, HvacInterval, Reading, WeatherHour

STEP_MERGE = timedelta(minutes=2)  # setpoint changes this close together are one adjustment
MIN_STEP = 1.0
REACHED = 0.5  # "warm" = within this of the target
SETTLE = timedelta(minutes=20)  # ignore the coast just after the furnace stops (residual heat in the ducts)
MIN_COAST = timedelta(hours=2)
BUCKET = timedelta(minutes=15)


def zone_devices(zone):
    """(thermostat accessory or None, [room ambient accessories]) for a zone."""
    accs = [a for a in Accessory.objects.filter(role=Accessory.Role.AMBIENT).select_related(
        "homekit_room__zone", "room_override__zone") if a.zone == zone]
    states = set(Characteristic.objects.filter(
        accessory__in=accs, kind__in=["currentHeaterCoolerState", "currentHeatingCooling"]).values_list(
        "accessory_id", flat=True))
    thermo = next((a for a in accs if a.id == zone.thermostat_id), None) or next(
        (a for a in accs if a.id in states), None)
    return thermo, [a for a in accs if a != thermo]


def _char(acc, kind):
    return acc.characteristics.filter(kind=kind).first() if acc else None


def _readings(ch, start=None, end=None, offset=0.0):
    if ch is None:
        return []
    qs = Reading.objects.filter(characteristic=ch).order_by("ts")
    if start:
        qs = qs.filter(ts__gte=start)
    if end:
        qs = qs.filter(ts__lte=end)
    return [(t, v + offset) for t, v in qs.values_list("ts", "value")]


def _value_at(ch, ts, offset=0.0):
    v = Reading.objects.filter(characteristic=ch, ts__lte=ts).order_by("-ts").values_list("value", flat=True).first()
    return None if v is None else v + offset


def setpoint_changes(thermo, since=None):
    """Effective heat-setpoint changes [(ts, from, to)], with fiddling (changes within 2 min) merged."""
    pts = _readings(_char(thermo, "heatingThreshold"))
    merged = []
    for t, v in pts:
        if merged and t - merged[-1][2] <= STEP_MERGE:
            merged[-1] = (merged[-1][0], v, t)  # keep when the adjustment began, its final value, last touch
        else:
            merged.append((t, v, t))
    out, prev = [], None
    for t, v, _ in merged:
        if prev is not None and v != prev:
            out.append((t, prev, v))
        prev = v
    return [c for c in out if since is None or c[0] >= since]


def outdoor_at(ts):
    rows = list(WeatherHour.objects.filter(issued=None, ts__gte=ts - timedelta(hours=1), ts__lte=ts + timedelta(hours=1))
                .order_by("ts").values_list("ts", "temperature"))
    rows = [(t, v) for t, v in rows if v is not None]
    if not rows:
        return None
    before = [r for r in rows if r[0] <= ts]
    after = [r for r in rows if r[0] > ts]
    if before and after:
        (t0, v0), (t1, v1) = before[-1], after[0]
        f = (ts - t0).total_seconds() / (t1 - t0).total_seconds()
        return v0 + f * (v1 - v0)
    return (before or after)[-1 if before else 0][1]


def warmups(zone, since=None, now=None):
    now = now or timezone.now()
    thermo, rooms = zone_devices(zone)
    if thermo is None:
        return []
    temp = _char(thermo, "currentTemperature")
    changes = setpoint_changes(thermo, since)
    out = []
    for i, (t0, frm, to) in enumerate(changes):
        if to - frm < MIN_STEP:
            continue
        start_temp = _value_at(temp, t0, thermo.calibration_offset)
        if start_temp is None or start_temp >= to - REACHED:
            continue
        window_end = min(changes[i + 1][0] if i + 1 < len(changes) else now, t0 + timedelta(hours=6))
        reached = next(((t, v) for t, v in _readings(temp, t0, window_end, thermo.calibration_offset)
                        if v >= to - REACHED), None)
        end = reached[0] if reached else window_end
        minutes = (end - t0).total_seconds() / 60
        furnace = sum((min(r.end, end) - max(r.start, t0)).total_seconds() / 60
                      for r in HvacInterval.objects.filter(thermostat=thermo, start__lt=end, end__gt=t0))
        room_rows = []
        for a in rooms:
            ch = _char(a, "currentTemperature")
            r0, r1 = _value_at(ch, t0, a.calibration_offset), _value_at(ch, end, a.calibration_offset)
            if r0 is not None and r1 is not None:
                room_rows.append({"name": a.room.name if a.room else a.name, "start": r0, "end": r1, "rise": r1 - r0})
        rise = (reached[1] if reached else (_value_at(temp, end, thermo.calibration_offset) or start_temp)) - start_temp
        out.append({
            "start": t0, "from_setpoint": frm, "target": to, "start_temp": start_temp, "reached_at": end if reached else None,
            "minutes": minutes, "furnace_min": furnace, "rise": rise,
            "rate": rise / (minutes / 60) if reached and minutes > 0 else None,
            "outdoor": outdoor_at(t0), "rooms": room_rows, "complete": reached is not None,
        })
    return out


def _indoor_series(zone, start, end):
    """Bucketed indoor temperature for a zone: room sensors if any (finer resolution), else the thermostat."""
    thermo, rooms = zone_devices(zone)
    sources = [(a, _char(a, "currentTemperature")) for a in (rooms or ([thermo] if thermo else []))]
    buckets = {}
    for a, ch in sources:
        for t, v in _readings(ch, start, end, a.calibration_offset):
            key = start + BUCKET * ((t - start) // BUCKET)
            buckets.setdefault(key, []).append(v)
    return [(k + BUCKET / 2, sum(v) / len(v)) for k, v in sorted(buckets.items())]


def coasts(zone, since=None, now=None):
    """Furnace-off stretches with the fitted heat-loss constant k (per hour) and time constant 1/k (hours)."""
    now = now or timezone.now()
    thermo, _ = zone_devices(zone)
    if thermo is None:
        return []
    since = since or Reading.objects.filter(characteristic__accessory=thermo).order_by("ts").values_list(
        "ts", flat=True).first()
    if since is None:
        return []
    runs = list(HvacInterval.objects.filter(thermostat=thermo, end__gte=since).order_by("start"))
    gaps, cursor = [], since
    for r in runs:
        if r.start > cursor:
            gaps.append((cursor, r.start))
        cursor = max(cursor, r.end)
    if now > cursor:
        gaps.append((cursor, now))
    # A setpoint raise starts a warm-up even before the furnace reports running; end the coast there.
    raises = [c[0] for c in setpoint_changes(thermo, since) if c[2] > c[1]]
    out = []
    for a, b in gaps:
        a = a + SETTLE if a != since else a
        b = min([b] + [t for t in raises if a < t < b])
        if b - a < MIN_COAST:
            continue
        pts = _indoor_series(zone, a, b)
        if len(pts) < 6:
            continue
        sxy = sxx = 0.0
        outs = []
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            o = outdoor_at(t0 + (t1 - t0) / 2)
            if o is None:
                continue
            outs.append(o)
            x = (v0 + v1) / 2 - o
            y = -(v1 - v0) / ((t1 - t0).total_seconds() / 3600)
            sxy += x * y
            sxx += x * x
        hours = (pts[-1][0] - pts[0][0]).total_seconds() / 3600
        drop = pts[0][1] - pts[-1][1]
        k = sxy / sxx if sxx and outs else None
        out.append({"start": pts[0][0], "end": pts[-1][0], "hours": hours, "t_start": pts[0][1], "t_end": pts[-1][1],
                    "drop": drop, "rate": drop / hours if hours else None,
                    "outdoor": sum(outs) / len(outs) if outs else None,
                    "k": k if k and k > 0 else None, "tau": 1 / k if k and k > 0 else None})
    return out


def model(zone, now=None):
    """Summary used for predictions: warm-up rate (°C/h, optionally vs outdoor temp) and heat-loss constant k."""
    ws = [w for w in warmups(zone, now=now) if w["complete"] and w["rate"]]
    cs = [c for c in coasts(zone, now=now) if c["k"] and c["hours"] >= 2 and c["drop"] > 0.2]
    rate_fit = None
    pairs = [(w["outdoor"], w["rate"]) for w in ws if w["outdoor"] is not None]
    if len(pairs) >= 4 and max(p[0] for p in pairs) - min(p[0] for p in pairs) >= 5:
        mx = sum(p[0] for p in pairs) / len(pairs)
        my = sum(p[1] for p in pairs) / len(pairs)
        b = sum((x - mx) * (y - my) for x, y in pairs) / sum((x - mx) ** 2 for x, _ in pairs)
        rate_fit = (my - b * mx, b)  # rate = a + b * outdoor
    return {
        "warmups": len(ws), "coasts": len(cs),
        "rate": statistics.median([w["rate"] for w in ws]) if ws else None, "rate_fit": rate_fit,
        "k": statistics.median([c["k"] for c in cs]) if cs else None,
    }


def warm_rate(m, outdoor):
    if m["rate_fit"] and outdoor is not None:
        a, b = m["rate_fit"]
        return max(0.3, a + b * outdoor)
    return m["rate"]


def predict_coast(t_in, start, end, k, floor=None):
    """Indoor temperature at `end`, cooling from `t_in` at `start` toward hourly outdoor temps (forecast where
    needed), never below the setback `floor` (the furnace holds it there)."""
    t, temp = start, t_in
    issued = WeatherHour.objects.exclude(issued=None).order_by("-issued").values_list("issued", flat=True).first()
    while t < end:
        step_end = min(end, t + timedelta(hours=1))
        o = outdoor_at(t)
        if o is None:
            o = WeatherHour.objects.filter(issued=issued, ts__lte=t + timedelta(minutes=30)).order_by("-ts") \
                .values_list("temperature", flat=True).first()
        if o is None:
            return None
        h = (step_end - t).total_seconds() / 3600
        temp = o + (temp - o) * math.exp(-k * h)
        if floor is not None:
            temp = max(temp, floor)
        t = step_end
    return temp


def preheat(zone, now=None):
    """Advice for the next scheduled start: predicted temperature then, and when heating should begin."""
    from .occupancy import now_state, periods
    now = now or timezone.now()
    thermo, _ = zone_devices(zone)
    if thermo is None or getattr(zone, "schedule", None) is None:
        return None
    upcoming = [p for p in (periods(zone, now, now + timedelta(days=7)) or []) if p["kind"] in ("work", "other")]
    nxt = next((p for p in upcoming if p["start"] > now), None)
    if nxt is None:
        return None
    m = model(zone, now)
    target = next((w["target"] for w in reversed(warmups(zone, now=now))), 20.0)
    setback = _value_at(_char(thermo, "heatingThreshold"), now)
    current = _value_at(_char(thermo, "currentTemperature"), now, thermo.calibration_offset)
    if current is None:
        return None
    predicted = predict_coast(current, now, nxt["start"], m["k"], floor=setback) if m["k"] else None
    start_temp = predicted if predicted is not None else (setback if setback is not None else current)
    rate = warm_rate(m, outdoor_at(nxt["start"]) or _forecast_at(nxt["start"]))
    need = max(0.0, target - REACHED - start_temp)
    minutes = need / rate * 60 if rate else None
    begin = nxt["start"] - timedelta(minutes=minutes) if minutes is not None else None
    if begin is not None:
        begin = begin.replace(second=0, microsecond=0) - timedelta(minutes=begin.minute % 5)
    observed = [w for w in warmups(zone, now=now) if w["complete"]]
    typical_start = None
    if observed:
        mins = sorted(timezone.localtime(w["start"]).hour * 60 + timezone.localtime(w["start"]).minute for w in observed)
        typical_start = mins[len(mins) // 2]
    return {
        "next": nxt, "target": target, "setback": setback, "current": current, "predicted": predicted,
        "start_temp": start_temp, "rate": rate, "minutes": minutes, "begin": begin, "model": m,
        "outdoor": outdoor_at(nxt["start"]) or _forecast_at(nxt["start"]),
        "typical_start": f"{typical_start // 60 % 12 or 12}:{typical_start % 60:02d} {'a.m.' if typical_start < 720 else 'p.m.'}"
        if typical_start is not None else None,
        "typical_minutes": statistics.median([w["minutes"] for w in observed]) if observed else None,
    }


def _forecast_at(ts):
    issued = WeatherHour.objects.exclude(issued=None).order_by("-issued").values_list("issued", flat=True).first()
    return WeatherHour.objects.filter(issued=issued, ts__lte=ts + timedelta(minutes=30)).order_by("-ts") \
        .values_list("temperature", flat=True).first()


def eta(zone, from_temp, to_temp, outdoor):
    """Minutes to warm from → to at a given outdoor temperature, or hours to cool if to < from (furnace off)."""
    m = model(zone)
    if to_temp >= from_temp:
        rate = warm_rate(m, outdoor)
        return {"kind": "warm", "minutes": (to_temp - from_temp) / rate * 60 if rate else None, "model": m}
    if not m["k"] or outdoor is None or to_temp <= outdoor:
        return {"kind": "cool", "minutes": None, "model": m}
    hours = math.log((from_temp - outdoor) / (to_temp - outdoor)) / m["k"]
    return {"kind": "cool", "minutes": hours * 60, "model": m}

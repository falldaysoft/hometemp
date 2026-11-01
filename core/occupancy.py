"""When each zone is in use: schedule + stat holidays + exceptions, checked against what the sensors saw."""
from datetime import datetime, time, timedelta

import holidays as holidays_lib
from django.utils import timezone

from .models import Accessory, Characteristic, HomeSettings, HvacInterval, OccupancyException, Reading

COMFORT_SETPOINT = 18.0  # heat setpoint at or above this means "someone wants it warm"
CO2_RISE = 150  # ppm above the early-morning baseline that says people are in the room

KIND_LABELS = {"work": "Work hours", "other": "Other use", "idle": "Idle"}


def holiday_names(start_date, end_date, settings=None):
    settings = settings or HomeSettings.load()
    if not settings.holiday_country:
        return {}
    years = range(start_date.year, end_date.year + 1)
    try:
        h = holidays_lib.country_holidays(settings.holiday_country, subdiv=settings.holiday_subdivision or None,
                                          years=years)
    except (NotImplementedError, KeyError):
        return {}
    return {d: n for d, n in h.items() if start_date <= d <= end_date}


def _local(d, t):
    return timezone.make_aware(datetime.combine(d, t))


def periods(zone, since, until, settings=None):
    """[{start, end, kind: work|other|idle, label, source}] covering [since, until], or None when the zone has
    no schedule (it's simply always in use)."""
    schedule = getattr(zone, "schedule", None)
    if schedule is None:
        return None
    d0 = timezone.localtime(since).date() - timedelta(days=1)
    d1 = timezone.localtime(until).date() + timedelta(days=1)
    hols = holiday_names(d0, d1, settings) if schedule.skip_holidays else {}

    spans = []  # base timeline from the schedule
    d = d0
    while d <= d1:
        if d.weekday() in schedule.days and d not in hols:
            spans.append({"start": _local(d, schedule.start), "end": _local(d, schedule.end), "kind": "work",
                          "label": "Work hours", "source": "schedule"})
        elif d.weekday() in schedule.days:
            spans.append({"start": _local(d, schedule.start), "end": _local(d, schedule.end), "kind": "idle",
                          "label": hols[d], "source": "holiday"})
        d += timedelta(days=1)

    # Fill the gaps with idle so the timeline is continuous.
    timeline, cursor = [], _local(d0, time(0))
    for s in spans:
        if s["start"] > cursor:
            timeline.append({"start": cursor, "end": s["start"], "kind": "idle", "label": "Idle", "source": "schedule"})
        timeline.append(s)
        cursor = s["end"]
    end_all = _local(d1 + timedelta(days=1), time(0))
    if cursor < end_all:
        timeline.append({"start": cursor, "end": end_all, "kind": "idle", "label": "Idle", "source": "schedule"})

    # Exceptions win over the schedule wherever they overlap.
    for ex in OccupancyException.objects.filter(zone=zone, end__gt=_local(d0, time(0)), start__lt=end_all):
        kind = {"off": "idle", "other": "other", "work": "work"}[ex.kind]
        label = ex.note or ex.get_kind_display()
        out = []
        for p in timeline:
            if p["end"] <= ex.start or p["start"] >= ex.end:
                out.append(p)
                continue
            if p["start"] < ex.start:
                out.append({**p, "end": ex.start})
            if p["end"] > ex.end:
                out.append({**p, "start": ex.end})
        out.append({"start": ex.start, "end": ex.end, "kind": kind, "label": label, "source": "exception",
                    "exception": ex})
        timeline = sorted(out, key=lambda p: p["start"])

    # Merge neighbours with the same meaning, then clip to the window.
    merged = []
    for p in timeline:
        if merged and merged[-1]["kind"] == p["kind"] and merged[-1]["label"] == p["label"] \
                and merged[-1]["end"] >= p["start"]:
            merged[-1]["end"] = max(merged[-1]["end"], p["end"])
        else:
            merged.append(dict(p))
    return [{**p, "start": max(p["start"], since), "end": min(p["end"], until)}
            for p in merged if p["end"] > since and p["start"] < until]


def now_state(zone, now=None):
    """{kind, label, until} for right now, or None if the zone has no schedule."""
    now = now or timezone.now()
    ps = periods(zone, now - timedelta(minutes=1), now + timedelta(days=14))
    if not ps:
        return None
    cur = next((p for p in ps if p["start"] <= now < p["end"]), ps[0])
    nxt = next((p for p in ps if p["start"] >= cur["end"] and p["kind"] != cur["kind"]), None)
    return {**cur, "until": cur["end"] if nxt else None, "next": nxt}


def _zone_chars(zone, kind):
    accs = [a for a in Accessory.objects.filter(role=Accessory.Role.AMBIENT).select_related(
        "homekit_room__zone", "room_override__zone") if a.zone == zone]
    return Characteristic.objects.filter(accessory__in=accs, kind=kind)


def evidence(zone, days=14, now=None):
    """Per local day: what the schedule expected vs what the sensors saw (CO₂ rise, heat setpoint, furnace)."""
    now = now or timezone.now()
    schedule = getattr(zone, "schedule", None)
    if schedule is None:
        return []
    co2 = list(_zone_chars(zone, "carbonDioxideLevel"))
    setpoints = list(_zone_chars(zone, "heatingThreshold"))
    thermo_ids = {c.accessory_id for c in setpoints}
    today = timezone.localtime(now).date()
    out = []
    for i in range(days):
        d = today - timedelta(days=i)
        day_start, day_end = _local(d, time(0)), _local(d + timedelta(days=1), time(0))
        ps = periods(zone, day_start, day_end) or []
        expected = "work" if any(p["kind"] == "work" for p in ps) else (
            "other" if any(p["kind"] == "other" for p in ps) else "idle")
        label = next((p["label"] for p in ps if p["source"] in ("holiday", "exception")), None)
        win_start, win_end = _local(d, schedule.start), min(_local(d, schedule.end), now)
        if win_end <= day_start or d.weekday() not in schedule.days and expected == "idle":
            win_start, win_end = _local(d, time(8)), min(_local(d, time(18)), now)

        rise = None
        if co2 and win_end > win_start:
            base = list(Reading.objects.filter(characteristic__in=co2, ts__gte=day_start,
                                               ts__lt=_local(d, time(6))).values_list("value", flat=True))
            day = list(Reading.objects.filter(characteristic__in=co2, ts__gte=win_start, ts__lt=win_end)
                       .values_list("value", flat=True))
            if day:
                baseline = min(base) if base else min(day)
                rise = max(day) - baseline
        sp = None
        if setpoints and win_end > win_start:
            vals = list(Reading.objects.filter(characteristic__in=setpoints, ts__gte=win_start, ts__lt=win_end)
                        .values_list("value", flat=True))
            prior = Reading.objects.filter(characteristic__in=setpoints, ts__lt=win_start).order_by("-ts") \
                .values_list("value", flat=True).first()
            vals += [prior] if prior is not None else []
            sp = max(vals) if vals else None
        furnace = sum(
            (min(r.end, win_end) - max(r.start, win_start)).total_seconds() / 60
            for r in HvacInterval.objects.filter(thermostat_id__in=thermo_ids, start__lt=win_end, end__gt=win_start)
        ) if win_end > win_start else 0

        signals = [s for s in (rise is not None and rise >= CO2_RISE, sp is not None and sp >= COMFORT_SETPOINT) if s]
        known = rise is not None or sp is not None
        seen = None if not known or win_end <= win_start else ("in use" if signals else "idle")
        mismatch = seen is not None and ((expected in ("work", "other")) != (seen == "in use"))
        out.append({"date": d, "expected": expected, "label": label, "co2_rise": rise, "setpoint": sp,
                    "furnace_min": furnace, "seen": seen, "mismatch": mismatch, "today": d == today})
    return out
